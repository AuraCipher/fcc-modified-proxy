"""End-to-end flow tests for multi-key NVIDIA NIM rotation (mock clients)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.nim import NimSettings
from providers.nvidia_nim.client import NvidiaNimProvider
from providers.nvidia_nim.key_pool import NimApiKeyPool


@pytest.fixture(autouse=True)
def mock_rate_limiter():
    """Passthrough global limiter so provider flow tests only exercise the key pool."""
    with patch("providers.openai_compat.GlobalRateLimiter") as mock:
        instance = mock.get_scoped_instance.return_value
        instance.wait_if_blocked = AsyncMock(return_value=False)

        async def _passthrough(fn, *args, **kwargs):
            for key in (
                "proactive",
                "max_retries",
                "base_delay",
                "max_delay",
                "jitter",
            ):
                kwargs.pop(key, None)
            return await fn(*args, **kwargs)

        instance.execute_with_retry = AsyncMock(side_effect=_passthrough)
        yield instance


@pytest.mark.asyncio
async def test_three_key_rotation_flow_exhausts_each_key_in_order():
    """Mock flow: 2 RPM per key → stay on key until full, then rotate 1→2→3."""
    used_keys: list[str] = []

    def factory(api_key: str) -> MagicMock:
        client = MagicMock()
        client.api_key = api_key
        return client

    pool = NimApiKeyPool(
        ("key-1", "key-2", "key-3"),
        rpm_per_key=2,
        window_seconds=0.2,
        cooldown_seconds=0.15,
        switch_delay_seconds=0.0,
        client_factory=factory,
    )

    for expected in ("key-1", "key-1", "key-2", "key-2", "key-3", "key-3"):
        lease = await pool.acquire()
        used_keys.append(lease.slot.api_key)
        assert lease.slot.api_key == expected

    assert used_keys == [
        "key-1",
        "key-1",
        "key-2",
        "key-2",
        "key-3",
        "key-3",
    ]


@pytest.mark.asyncio
async def test_three_key_flow_cycles_back_after_all_cooldowns():
    """After both keys hit RPM and cool down, the pool reuses key-1."""
    pool = NimApiKeyPool(
        ("key-1", "key-2"),
        rpm_per_key=1,
        window_seconds=0.2,
        cooldown_seconds=0.12,
        switch_delay_seconds=0.0,
        client_factory=lambda api_key: MagicMock(api_key=api_key),
    )

    assert (await pool.acquire()).slot.api_key == "key-1"
    assert (await pool.acquire()).slot.api_key == "key-2"

    start = time.monotonic()
    third = await pool.acquire()
    elapsed = time.monotonic() - start

    assert third.slot.api_key == "key-1"
    assert elapsed >= 0.08


@pytest.mark.asyncio
async def test_switch_delay_applied_between_keys():
    pool = NimApiKeyPool(
        ("key-1", "key-2"),
        rpm_per_key=1,
        window_seconds=0.05,
        cooldown_seconds=0.06,
        switch_delay_seconds=0.08,
        client_factory=lambda api_key: MagicMock(api_key=api_key),
    )

    await pool.acquire()
    start = time.monotonic()
    lease = await pool.acquire()
    elapsed = time.monotonic() - start

    assert lease.slot.api_key == "key-2"
    assert elapsed >= 0.06


@pytest.mark.asyncio
async def test_provider_create_stream_flow_rotates_mock_clients(provider_config):
    """Provider-level flow: _create_stream must hit each mock client in RPM order."""
    created_with: list[str] = []

    async def _mock_stream():
        chunk = MagicMock()
        chunk.choices = []
        chunk.usage = None
        yield chunk

    def _install_client(api_key: str) -> MagicMock:
        client = MagicMock()
        client.api_key = api_key

        async def _create(**_kwargs: object) -> object:
            created_with.append(api_key)
            return _mock_stream()

        client.chat.completions.create = AsyncMock(side_effect=_create)
        return client

    with patch("providers.openai_compat.AsyncOpenAI") as mock_openai_cls:
        mock_openai_cls.side_effect = lambda **kwargs: _install_client(
            kwargs["api_key"]
        )

        provider = NvidiaNimProvider(
            provider_config,
            nim_settings=NimSettings(),
            api_keys=("nim-a", "nim-b"),
            rpm_per_key=2,
            key_window_sec=60.0,
            key_cooldown_sec=65.0,
            key_switch_delay_sec=0.0,
        )
        provider._key_pool._slots[1]._client = _install_client("nim-b")

        body = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}

        for _ in range(4):
            await provider._create_stream(body)

    assert created_with == ["nim-a", "nim-a", "nim-b", "nim-b"]
