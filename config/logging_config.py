"""Loguru configuration — terminal only, no file logs.

Stdlib logging is intercepted and funneled to loguru for consistent formatting.
"""

import logging
import sys
import threading
from typing import Any

from loguru import logger

_configured = False

# Loguru ``logger.bind()`` key used by structured TRACE payloads; ``core/trace.py``
# uses the identical string constant ``TRACE_PAYLOAD_BINDING``.
_TRACE_PAYLOAD_BINDING = "trace_payload"


def _nim_console_log_filter(record: Any) -> bool:
    """Mirror NIM pool status lines and IP rotation logs to the terminal."""
    if record["extra"].get(_TRACE_PAYLOAD_BINDING) is not None:
        return False
    # Show all IP_ROTATION-prefixed logs on the terminal regardless of module
    message = str(record.get("message", ""))
    if message.startswith("IP_ROTATION:"):
        return True
    if message.startswith("PROXY_POOL:"):
        return True
    return str(record["name"]).startswith("providers.nvidia_nim")


class InterceptHandler(logging.Handler):
    """Redirect stdlib logging to loguru."""

    def __init__(self) -> None:
        super().__init__()
        self._local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._local, "active", False):
            # Avoid deadlock when nested stdlib records fire during a loguru emit.
            return
        self._local.active = True
        try:
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno

            frame, depth = logging.currentframe(), 2
            while frame is not None and frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1

            logger.opt(depth=depth, exception=record.exc_info).log(
                level, record.getMessage()
            )
        finally:
            self._local.active = False


def configure_logging(
    *,
    force: bool = False,
    verbose_third_party: bool = False,
) -> None:
    """Configure loguru with terminal output only (no file logging).

    Idempotent: skips if already configured (e.g. hot reload).
    Use force=True to reconfigure (e.g. in tests).

    When ``verbose_third_party`` is false, noisy HTTP and Telegram loggers are capped
    at WARNING unless explicitly configured otherwise.
    """
    global _configured
    if _configured and not force:
        return
    _configured = True

    # Remove default loguru handler (writes to stderr)
    logger.remove()

    # Terminal: one-line NIM API rotation status (pool, client, request helpers)
    logger.add(
        sys.stderr,
        level="INFO",
        format="{message}",
        filter=_nim_console_log_filter,
        enqueue=True,
    )

    # Intercept stdlib logging: route all root logger output to loguru
    intercept = InterceptHandler()
    logging.root.handlers = [intercept]
    logging.root.setLevel(logging.DEBUG)

    third_party = (
        "httpx",
        "httpcore",
        "httpcore.http11",
        "httpcore.connection",
        "telegram",
        "telegram.ext",
    )
    for name in third_party:
        logging.getLogger(name).setLevel(
            logging.WARNING if not verbose_third_party else logging.NOTSET
        )
