"""Tests for deferred uvicorn access logging with proxy labels."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

from api.runtime import (
    _install_proxy_access_filter,
    deferred_access_logging_enabled,
    log_deferred_access,
)
from core.proxy_pool import (
    ProxyAccessLogFormatter,
    _SuppressUvicornAccessLogFilter,
    bind_request_proxy_store,
    clear_request_proxy,
    reset_request_proxy_store,
    set_request_proxy,
)


class _PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


def test_proxy_access_log_formatter_appends_via_suffix() -> None:
    formatter = ProxyAccessLogFormatter(_PlainFormatter())
    set_request_proxy("via 203.0.113.10:8080")
    try:
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='127.0.0.1:1234 - "POST /v1/messages HTTP/1.1" 200',
            args=(),
            exc_info=None,
        )
        assert (
            formatter.format(record)
            == '127.0.0.1:1234 - "POST /v1/messages HTTP/1.1" 200 via 203.0.113.10:8080'
        )
    finally:
        clear_request_proxy()


def test_proxy_access_log_formatter_leaves_line_unchanged_without_proxy() -> None:
    formatter = ProxyAccessLogFormatter(_PlainFormatter())
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='127.0.0.1:1234 - "GET /health HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )
    assert formatter.format(record) == '127.0.0.1:1234 - "GET /health HTTP/1.1" 200'


def test_suppress_uvicorn_access_log_filter_blocks_early_records() -> None:
    filt = _SuppressUvicornAccessLogFilter()
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="early access log",
        args=(),
        exc_info=None,
    )
    assert filt.filter(record) is False


def test_suppress_uvicorn_access_log_filter_allows_deferred_records() -> None:
    filt = _SuppressUvicornAccessLogFilter()
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="deferred access log",
        args=(),
        exc_info=None,
    )
    record.deferred_access = True
    assert filt.filter(record) is True


def test_log_deferred_access_uses_uvicorn_access_args_and_proxy_suffix() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    handler = logging.Handler()
    handler.setFormatter(ProxyAccessLogFormatter(_PlainFormatter()))
    access_logger.handlers.clear()
    access_logger.filters.clear()
    access_logger.addHandler(handler)
    access_logger.setLevel(logging.INFO)

    emitted: list[str] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            emitted.append(handler.formatter.format(record))

    capture = _CaptureHandler()
    capture.setFormatter(ProxyAccessLogFormatter(_PlainFormatter()))
    access_logger.handlers.clear()
    access_logger.addHandler(capture)

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/v1/messages",
        "query_string": b"beta=true",
        "headers": [],
        "client": ("127.0.0.1", 39374),
        "server": ("127.0.0.1", 8082),
        "scheme": "http",
        "state": {},
    }
    request = Request(scope)
    request.state.proxy_access_label = {"label": "via 203.0.113.10:8080"}
    response = MagicMock(status_code=200)

    log_deferred_access(request, response)

    assert len(emitted) == 1
    assert emitted[0] == (
        '127.0.0.1:39374 - "POST /v1/messages?beta=true HTTP/1.1" 200 '
        "via 203.0.113.10:8080"
    )


def test_install_proxy_access_filter_wraps_handlers_and_adds_suppress_filter() -> None:
    import api.runtime as runtime_mod

    runtime_mod._deferred_access_logging_active = False
    access_logger = logging.getLogger("uvicorn.test_proxy_access")
    handler = logging.StreamHandler()
    handler.setFormatter(_PlainFormatter())
    access_logger.handlers.clear()
    access_logger.filters.clear()
    access_logger.addHandler(handler)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "api.runtime.logging.getLogger",
            lambda name: (
                access_logger if name == "uvicorn.access" else logging.getLogger(name)
            ),
        )
        _install_proxy_access_filter()

    assert isinstance(handler.formatter, ProxyAccessLogFormatter)
    assert any(
        isinstance(f, _SuppressUvicornAccessLogFilter) for f in access_logger.filters
    )
    assert deferred_access_logging_enabled() is True


def test_set_request_proxy_updates_bound_request_store() -> None:
    store = {"label": ""}
    token = bind_request_proxy_store(store)
    try:
        set_request_proxy("via 203.0.113.10:8080")
        assert store["label"] == "via 203.0.113.10:8080"
    finally:
        reset_request_proxy_store(token)
        clear_request_proxy()


def test_streaming_request_logs_proxy_label_in_access_log() -> None:
    import api.runtime as runtime_mod
    from collections.abc import AsyncIterator
    from unittest.mock import AsyncMock, patch

    from fastapi.testclient import TestClient

    from api.app import create_app
    from providers.nvidia_nim import NvidiaNimProvider

    runtime_mod._deferred_access_logging_active = False
    access_logger = logging.getLogger("uvicorn.access")
    emitted: list[str] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            emitted.append(ProxyAccessLogFormatter(_PlainFormatter()).format(record))

    access_logger.handlers.clear()
    access_logger.filters.clear()
    access_logger.addHandler(_CaptureHandler())
    _install_proxy_access_filter()

    mock_provider = MagicMock(spec=NvidiaNimProvider)

    async def stream_with_proxy(
        *_args: object, **_kwargs: object
    ) -> AsyncIterator[str]:
        set_request_proxy("via 203.0.113.10:8080")
        yield "event: message_start\ndata: {}\n\n"

    mock_provider.stream_response = stream_with_proxy

    with (
        patch("api.dependencies.resolve_provider", return_value=mock_provider),
        patch(
            "providers.registry.ProviderRegistry.validate_configured_models",
            new_callable=AsyncMock,
        ),
        patch("providers.registry.ProviderRegistry.start_model_list_refresh"),
        TestClient(create_app()) as client,
    ):
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-3-sonnet",
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 100,
                "stream": True,
            },
        )
        assert response.status_code == 200
        _ = b"".join(response.iter_bytes())

    assert any("via 203.0.113.10:8080" in line for line in emitted)
