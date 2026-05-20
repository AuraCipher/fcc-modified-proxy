"""Tests for one-line NIM key pool terminal logging."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import AsyncOpenAI

from providers.nvidia_nim.key_pool import NimApiKeyPool


def _mock_client_factory() -> Callable[[str], AsyncOpenAI]:
    def _make(_key: str) -> AsyncOpenAI:
        client = MagicMock(spec=AsyncOpenAI)
        client.close = AsyncMock()
        return cast(AsyncOpenAI, client)

    return _make


@pytest.mark.asyncio
async def test_multi_key_pool_emits_one_line_status_logs(
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.INFO)

    pool = NimApiKeyPool(
        ("key-a", "key-b"),
        rpm_per_key=1,
        window_seconds=0.2,
        cooldown_seconds=0.25,
        switch_delay_seconds=0.05,
        client_factory=_mock_client_factory(),
    )

    await pool.acquire()
    await pool.acquire()

    messages = [record.message for record in caplog.records]
    assert any(msg == "Using API 1" for msg in messages)
    assert any(
        msg.startswith("API 1:") and "seconds remaining" in msg for msg in messages
    )
    assert any(msg == "Started 1 second delay" for msg in messages)
    assert any(msg == "Using API 2" for msg in messages)

    await pool.close()


@pytest.mark.asyncio
async def test_single_key_pool_skips_rotation_logs(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)

    pool = NimApiKeyPool(
        ("only-key",),
        rpm_per_key=10,
        window_seconds=60.0,
        cooldown_seconds=65.0,
        switch_delay_seconds=5.0,
        client_factory=_mock_client_factory(),
    )

    await pool.acquire()

    messages = [record.message for record in caplog.records]
    assert not any(msg.startswith("Using API") for msg in messages)
    assert not any("key pool ready" in msg for msg in messages)

    await pool.close()
