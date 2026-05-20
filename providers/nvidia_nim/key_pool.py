"""Round-robin NVIDIA NIM API keys with per-key RPM limits and cooldowns."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from loguru import logger
from openai import AsyncOpenAI

from core.rate_limit import StrictSlidingWindowLimiter

T = TypeVar("T")

ClientFactory = Callable[[str], AsyncOpenAI]

_COOLDOWN_LOG_TICK_SECONDS = 5.0
_MAX_RATE_LIMIT_COOLDOWN_SECONDS = 15 * 60.0


@dataclass(slots=True)
class NimApiSlot:
    """One NVIDIA API key with its own limiter and optional cooldown."""

    index: int
    api_key: str
    limiter: StrictSlidingWindowLimiter
    cooldown_until: float = 0.0
    consecutive_rate_limits: int = 0
    _client: AsyncOpenAI | None = field(default=None, repr=False)

    def is_in_cooldown(self, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        return current < self.cooldown_until

    def cooldown_remaining(self, *, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        return max(0.0, self.cooldown_until - current)

    def start_cooldown(self, seconds: float, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        self.cooldown_until = current + seconds


@dataclass(frozen=True, slots=True)
class NimSlotLease:
    """Handle for an acquired key slot; released after the upstream call completes."""

    pool: NimApiKeyPool
    slot: NimApiSlot
    client: AsyncOpenAI

    async def mark_rate_limited(
        self, *, seconds: float | None = None, exc: BaseException | None = None
    ) -> None:
        await self.pool.mark_slot_rate_limited(
            self.slot.index, cooldown_seconds=seconds, exc=exc
        )


class NimApiKeyPool:
    """Select among multiple NVIDIA NIM API keys with RPM limits and cooldowns."""

    def __init__(
        self,
        api_keys: tuple[str, ...],
        *,
        rpm_per_key: int,
        window_seconds: float,
        cooldown_seconds: float,
        switch_delay_seconds: float,
        client_factory: ClientFactory,
    ) -> None:
        if not api_keys:
            raise ValueError("api_keys must be non-empty")
        if rpm_per_key <= 0:
            raise ValueError("rpm_per_key must be > 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be > 0")
        if switch_delay_seconds < 0:
            raise ValueError("switch_delay_seconds must be >= 0")

        self._rpm_per_key = rpm_per_key
        self._window_seconds = window_seconds
        self._cooldown_seconds = cooldown_seconds
        self._switch_delay_seconds = switch_delay_seconds
        self._client_factory = client_factory
        self._slots = tuple(
            NimApiSlot(
                index=index,
                api_key=key,
                limiter=StrictSlidingWindowLimiter(rpm_per_key, window_seconds),
            )
            for index, key in enumerate(api_keys)
        )
        self._cursor = 0
        self._lock = asyncio.Lock()
        self._active_slot_index: int | None = None
        self._last_slot_index: int | None = None
        self._cooldown_log_tasks: dict[int, asyncio.Task[None]] = {}
        self._multi_key = len(self._slots) > 1

        if self._multi_key:
            logger.info(
                "NIM key pool ready: {} APIs, {} RPM each, {:.0f}s cooldown, {:.0f}s switch delay",
                len(self._slots),
                rpm_per_key,
                cooldown_seconds,
                switch_delay_seconds,
            )

    @property
    def slot_count(self) -> int:
        return len(self._slots)

    @property
    def primary_api_key(self) -> str:
        return self._slots[0].api_key

    def client_for_index(self, index: int) -> AsyncOpenAI:
        slot = self._slots[index]
        if slot._client is None:
            slot._client = self._client_factory(slot.api_key)
        return slot._client

    def bind_primary_client(self, client: AsyncOpenAI) -> None:
        """Reuse an existing client for slot 0 (single-key tests and shared cleanup)."""
        self._slots[0]._client = client

    def _api_label(self, slot_index: int) -> str:
        return f"API {slot_index + 1}"

    def _log_pool(self, message: str) -> None:
        if self._multi_key:
            logger.info(message)

    def _retry_after_seconds(self, exc: BaseException) -> float | None:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers is None:
            return None
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if not raw:
            return None
        try:
            parsed = float(raw)
        except (TypeError, ValueError):
            return None
        if parsed <= 0:
            return None
        return parsed

    def _proactive_quarantine_seconds(self) -> float:
        """No traffic long enough that the strict local RPM window fully empties."""
        return max(float(self._cooldown_seconds), float(self._window_seconds))

    def _rate_limit_cooldown_seconds(
        self, slot_index: int, exc: BaseException | None
    ) -> float:
        slot = self._slots[slot_index]
        retry_after = self._retry_after_seconds(exc) if exc is not None else None
        exp = max(0, slot.consecutive_rate_limits - 1)
        backoff = float(self._cooldown_seconds) * (2.0**exp)
        # Full window with zero requests before this key may be used again.
        floor = float(self._window_seconds)
        desired = max(
            floor,
            float(self._cooldown_seconds),
            backoff,
            retry_after or 0.0,
        )
        return min(desired, _MAX_RATE_LIMIT_COOLDOWN_SECONDS)

    async def _begin_slot_cooldown(
        self,
        slot_index: int,
        seconds: float,
        *,
        now: float | None = None,
        clear_sliding_window: bool = False,
    ) -> None:
        slot = self._slots[slot_index]
        if clear_sliding_window:
            await slot.limiter.reset()
        slot.start_cooldown(seconds, now=now)
        remaining = math.ceil(slot.cooldown_remaining(now=now))
        self._log_pool(f"{self._api_label(slot_index)}: {remaining} seconds remaining")
        prior = self._cooldown_log_tasks.pop(slot_index, None)
        if prior is not None:
            prior.cancel()
        self._cooldown_log_tasks[slot_index] = asyncio.create_task(
            self._run_cooldown_countdown(slot_index)
        )

    async def _run_cooldown_countdown(self, slot_index: int) -> None:
        try:
            while True:
                remaining = self._slots[slot_index].cooldown_remaining()
                if remaining <= 0:
                    return
                await asyncio.sleep(min(_COOLDOWN_LOG_TICK_SECONDS, remaining))
                remaining = self._slots[slot_index].cooldown_remaining()
                if remaining <= 0:
                    return
                seconds = math.ceil(remaining)
                self._log_pool(
                    f"{self._api_label(slot_index)}: {seconds} seconds remaining"
                )
        except asyncio.CancelledError:
            raise

    async def acquire(self) -> NimSlotLease:
        """Pick the next usable key, waiting when every slot is in cooldown."""
        while True:
            switch_delay = 0.0
            slot_index: int | None = None
            sleep_seconds: float | None = None
            now = time.monotonic()

            async with self._lock:
                if self._next_wait_seconds(now) is not None:
                    sleep_seconds = self._min_cooldown_remaining(now)

            if sleep_seconds is not None:
                wait = max(sleep_seconds, 0.05)
                wait_s = math.ceil(wait)
                self._log_pool(f"All APIs cooling down, waiting {wait_s} seconds")
                await asyncio.sleep(wait)
                continue

            picked = await self._pick_slot(now)
            if picked is None:
                async with self._lock:
                    retry_wait = max(
                        self._min_cooldown_remaining(time.monotonic()), 0.05
                    )
                await asyncio.sleep(retry_wait)
                continue

            slot_index, switch_delay = picked
            async with self._lock:
                self._active_slot_index = slot_index
                self._last_slot_index = slot_index

            if switch_delay > 0:
                delay_s = math.ceil(switch_delay)
                self._log_pool(f"Started {delay_s} second delay")
                await asyncio.sleep(switch_delay)

            slot = self._slots[slot_index]
            await slot.limiter.acquire()
            self._log_pool(f"Using {self._api_label(slot_index)}")
            return NimSlotLease(
                pool=self,
                slot=slot,
                client=self.client_for_index(slot_index),
            )

    async def mark_slot_rate_limited(
        self,
        slot_index: int,
        *,
        cooldown_seconds: float | None = None,
        exc: BaseException | None = None,
    ) -> None:
        async with self._lock:
            slot = self._slots[slot_index]
            slot.consecutive_rate_limits += 1
            seconds = (
                float(cooldown_seconds)
                if cooldown_seconds is not None
                else self._rate_limit_cooldown_seconds(slot_index, exc)
            )

        await self._begin_slot_cooldown(
            slot_index,
            seconds,
            clear_sliding_window=True,
        )

        async with self._lock:
            if self._active_slot_index == slot_index:
                self._active_slot_index = None

    async def _pick_slot(self, now: float) -> tuple[int, float] | None:
        """Return (slot_index, switch_delay) or None when all slots are unavailable."""
        async with self._lock:
            slot_count = len(self._slots)
            start_index = (
                self._active_slot_index
                if self._active_slot_index is not None
                else self._cursor
            )
            order = [
                (start_index + offset) % slot_count for offset in range(slot_count)
            ]

        for index in order:
            slot = self._slots[index]
            async with self._lock:
                if slot.is_in_cooldown(now=now):
                    continue
            if await slot.limiter.is_full():
                quarantine_s = self._proactive_quarantine_seconds()
                await self._begin_slot_cooldown(
                    index,
                    quarantine_s,
                    now=now,
                    clear_sliding_window=True,
                )
                async with self._lock:
                    if self._active_slot_index == index:
                        self._active_slot_index = None
                continue
            switch_delay = 0.0
            async with self._lock:
                if (
                    self._switch_delay_seconds > 0
                    and self._last_slot_index is not None
                    and self._last_slot_index != index
                ):
                    switch_delay = self._switch_delay_seconds
            return index, switch_delay
        return None

    def _min_cooldown_remaining(self, now: float) -> float:
        return min(slot.cooldown_remaining(now=now) for slot in self._slots)

    def _next_wait_seconds(self, now: float) -> float | None:
        if any(not slot.is_in_cooldown(now=now) for slot in self._slots):
            return None
        return self._min_cooldown_remaining(now)

    async def execute_with_retry(
        self,
        fn: Callable[[AsyncOpenAI], Awaitable[T]],
        *,
        max_rotations: int | None = None,
        on_retryable_error: Callable[[NimSlotLease, BaseException], Awaitable[None]]
        | None = None,
    ) -> T:
        """Run ``fn(client)`` rotating keys on proactive exhaustion or retryable errors."""
        attempts = max_rotations if max_rotations is not None else len(self._slots)
        total_attempts = max(1, attempts)
        last_exc: BaseException | None = None

        for attempt in range(total_attempts):
            lease = await self.acquire()
            try:
                result = await fn(lease.client)
                async with self._lock:
                    self._slots[lease.slot.index].consecutive_rate_limits = 0
                return result
            except BaseException as exc:
                last_exc = exc
                if attempt >= total_attempts - 1:
                    break
                if on_retryable_error is not None:
                    await on_retryable_error(lease, exc)
                else:
                    await lease.mark_rate_limited(exc=exc)
                self._log_pool(
                    f"{self._api_label(lease.slot.index)} failed ({type(exc).__name__}), rotating"
                )

        assert last_exc is not None
        raise last_exc

    async def close(self) -> None:
        for task in self._cooldown_log_tasks.values():
            task.cancel()
        self._cooldown_log_tasks.clear()
        for slot in self._slots:
            client = slot._client
            if client is not None:
                await client.close()
                slot._client = None
