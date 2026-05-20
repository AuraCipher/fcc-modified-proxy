"""Tests for providers.nvidia_nim.key_pool."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import AsyncOpenAI

from providers.nvidia_nim.key_pool import NimApiKeyPool


def _client_factory() -> MagicMock:
    return MagicMock(side_effect=lambda key: MagicMock(api_key=key))


@pytest.mark.asyncio
async def test_pool_rotates_when_first_slot_is_full():
    pool = NimApiKeyPool(
        ("key-a", "key-b"),
        rpm_per_key=2,
        window_seconds=0.2,
        cooldown_seconds=0.3,
        switch_delay_seconds=0.0,
        client_factory=_client_factory(),
    )

    lease1 = await pool.acquire()
    lease2 = await pool.acquire()
    assert lease1.slot.api_key == "key-a"
    assert lease2.slot.api_key == "key-a"

    lease3 = await pool.acquire()
    assert lease3.slot.api_key == "key-b"


@pytest.mark.asyncio
async def test_pool_waits_when_all_slots_are_in_cooldown():
    pool = NimApiKeyPool(
        ("key-a",),
        rpm_per_key=1,
        window_seconds=0.12,
        cooldown_seconds=0.15,
        switch_delay_seconds=0.0,
        client_factory=_client_factory(),
    )

    await pool.acquire()
    start = time.monotonic()
    await pool.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.1


@pytest.mark.asyncio
async def test_pool_execute_with_retry_rotates_on_failure():
    pool = NimApiKeyPool(
        ("key-a", "key-b"),
        rpm_per_key=100,
        window_seconds=60.0,
        cooldown_seconds=0.3,
        switch_delay_seconds=0.0,
        client_factory=_client_factory(),
    )
    clients: list[AsyncOpenAI] = []

    async def flaky(client: AsyncOpenAI) -> str:
        clients.append(client)
        if len(clients) == 1:
            raise RuntimeError("upstream down")
        return "ok"

    result = await pool.execute_with_retry(flaky, max_rotations=2)
    assert result == "ok"
    assert clients[0].api_key == "key-a"
    assert clients[1].api_key == "key-b"


@pytest.mark.asyncio
async def test_pool_rejects_empty_keys():
    with pytest.raises(ValueError, match="api_keys must be non-empty"):
        NimApiKeyPool(
            (),
            rpm_per_key=35,
            window_seconds=60.0,
            cooldown_seconds=65.0,
            switch_delay_seconds=0.0,
            client_factory=_client_factory(),
        )


@pytest.mark.asyncio
async def test_pool_close_closes_cached_clients():
    client = MagicMock()
    client.close = AsyncMock()
    factory = MagicMock(return_value=client)
    pool = NimApiKeyPool(
        ("key-a", "key-b"),
        rpm_per_key=10,
        window_seconds=60.0,
        cooldown_seconds=65.0,
        switch_delay_seconds=0.0,
        client_factory=factory,
    )
    await pool.acquire()
    await pool.close()
    client.close.assert_awaited()
