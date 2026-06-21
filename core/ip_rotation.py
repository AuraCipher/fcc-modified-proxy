"""VPN IP rotation service — random proxy selection with direct-IP fallback.

Thread-safe singleton accessible by all provider transports.
Every provider request picks a random proxy from the pool.
Direct (computer) IP is used **only** when all VPN proxies have failed.
"""

from __future__ import annotations

import random
from typing import ClassVar

from loguru import logger

from config.ip_rotation import IpRotationSettings


class IpRotationService:
    """Manages VPN proxy pool with random rotation and direct IP fallback.

    Usage
    -----
    Initialize once at startup::

        IpRotationService.init_instance(settings)

    Consume anywhere::

        service = IpRotationService.get_instance()
        if service and service.is_enabled:
            pool = service.get_pool_for_attempt(attempt)
            proxy = random.choice(pool) if pool else None

    If no proxies are configured ``is_enabled`` returns ``False`` and callers
    fall back to their existing behaviour (per-provider proxy or direct IP).
    """

    _instance: ClassVar[IpRotationService | None] = None

    # ------------------------------------------------------------------
    # Singleton lifecycle
    # ------------------------------------------------------------------

    @classmethod
    def init_instance(cls, settings: IpRotationSettings) -> IpRotationService:
        """Create/replace the global singleton from configuration."""
        instance = cls(settings)
        cls._instance = instance
        if instance.is_enabled:
            logger.info(
                "IP_ROTATION: Enabled with {} proxies, "
                "max_attempts={}, fallback_to_direct={}",
                instance.proxy_count,
                instance.max_attempts,
                instance.fallback_to_direct,
            )
        else:
            logger.debug("IP_ROTATION: No VPN proxies configured — rotation disabled")
        return instance

    @classmethod
    def get_instance(cls) -> IpRotationService | None:
        """Return the global singleton, or ``None`` if not initialised."""
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Tear down the singleton (testing / shutdown)."""
        cls._instance = None

    # ------------------------------------------------------------------
    # Instance
    # ------------------------------------------------------------------

    def __init__(self, settings: IpRotationSettings) -> None:
        # Store as list[str | None] so get_pool_for_attempt returns match the return type
        self._proxies: list[str | None] = list(settings.proxies)
        # Auto-compute max_attempts: one per proxy + 1 for the direct-IP fallback
        self._max_attempts = (
            settings.max_attempts
            if settings.max_attempts > 0
            else max(1, len(self._proxies) + 1)
        )
        self._fallback_to_direct = settings.fallback_to_direct

    # -- read-only properties -------------------------------------------

    @property
    def is_enabled(self) -> bool:
        """Whether this service has any VPN proxies to rotate through."""
        return len(self._proxies) > 0

    @property
    def proxy_count(self) -> int:
        return len(self._proxies)

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    @property
    def fallback_to_direct(self) -> bool:
        return self._fallback_to_direct

    # -- pool selection -------------------------------------------------

    def get_pool_for_attempt(self, attempt: int) -> list[str | None]:
        """Return the proxy pool eligible for the given *attempt* (0-based).

        Rules
        -----
        * ``attempt < len(proxies)`` → only VPN proxies (random order).
        * ``attempt >= len(proxies) and fallback_to_direct`` → ``[None]``
          (direct computer IP, last resort).
        * No proxies configured → empty list (caller handles normally).
        """
        if not self._proxies:
            return []

        if attempt < len(self._proxies):
            return self._proxies

        if self._fallback_to_direct:
            return [None]

        return []

    # -- convenience for transports -------------------------------------

    def get_random_order(self) -> list[str | None]:
        """Return all proxies in random order (no repeats), with optional direct IP sentinel.

        Each caller gets their own independent shuffled copy, so concurrent
        requests are isolated.  Guarantees every proxy is tried at most once
        before moving to the direct-IP fallback.
        """
        proxies: list[str | None] = list(self._proxies)
        random.shuffle(proxies)
        if self._fallback_to_direct:
            proxies.append(None)
        return proxies

    def get_random_proxy(self, attempt: int) -> str | None:
        """Pick a random proxy for *attempt*, or ``None`` for direct IP.

        Returns ``None`` both for "direct IP" and "no proxies configured".
        Callers should check ``is_enabled`` to distinguish.
        """
        pool = self.get_pool_for_attempt(attempt)
        if not pool:
            return None
        return random.choice(pool)

    def label_for(self, proxy: str | None) -> str:
        """Human-readable label for log messages."""
        if proxy is None:
            return "DIRECT COMPUTER IP (no proxy)"
        # Mask credentials in logs
        if "@" in proxy:
            return proxy.split("@", 1)[1]
        return proxy
