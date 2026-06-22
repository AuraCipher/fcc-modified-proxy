"""Smart proxy pool backed by SQLite with cooldown tracking and health state.

Replaces the in-memory ``IpRotationService`` sequential-retry approach with a
persistent pool that picks **one random working proxy** per request.  If no
healthy proxy is available, callers fall back to direct computer IP immediately.

Design
------
- ``proxy_pool`` table in the same SQLite DB as token tracking
  (``~/.fcc/token_tracking.db``).
- Proxies are seeded from ``~/.fcc/ip_rotation.json`` at startup.
- **One attempt per request** — no sequential retry loop.
- Rate-limited proxies (HTTP 429) get a configurable cooldown timer.
- Dead proxies (N consecutive failures) are marked ``is_alive = 0``.
- A background health checker periodically revives dead/cooldown proxies.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from loguru import logger

# ---------------------------------------------------------------------------
# Per-request proxy tracking (injected into uvicorn access log)
# ---------------------------------------------------------------------------

_current_proxy_label: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_proxy_label", default=""
)


def set_request_proxy(label: str) -> None:
    """Store the proxy label for the current request (called from transports)."""
    _current_proxy_label.set(label)


def get_request_proxy() -> str:
    """Return the proxy label for the current request, or ``""``."""
    return _current_proxy_label.get()


class ProxyAccessLogFormatter(logging.Formatter):
    """Wraps an existing formatter and appends the proxy label to every record."""

    def __init__(self, wrapped: logging.Formatter) -> None:
        self._wrapped = wrapped
        # Copy format info so introspection still works
        try:
            self._style = wrapped._style
        except AttributeError:
            pass
        self._fmt = getattr(wrapped, "_fmt", None)

    def format(self, record: logging.LogRecord) -> str:
        proxy = get_request_proxy()
        original = self._wrapped.format(record)
        if proxy:
            return f"{original.rstrip()} ({proxy})"
        return original

    def formatTime(self, record, datefmt=None):
        return self._wrapped.formatTime(record, datefmt)

    def formatException(self, ei):
        return self._wrapped.formatException(ei)

    def formatStack(self, stack_info):
        return self._wrapped.formatStack(stack_info)

    def usesTime(self):
        return self._wrapped.usesTime()


# ---------------------------------------------------------------------------
# Settings (could be promoted to pydantic later)
# ---------------------------------------------------------------------------


@dataclass
class ProxyPoolSettings:
    """Runtime settings for the proxy pool behaviour."""

    # Connect timeout used when sending real LLM requests via a proxy.
    proxy_connect_timeout: float = 5.0
    # Default cooldown duration in hours when provider has no specific override.
    cooldown_default_hours: float = 15.0
    # Per-provider cooldown overrides (lowercased provider id → hours).
    cooldown_by_provider: dict[str, float] = field(default_factory=dict)
    # Consecutive failures before marking a proxy as dead.
    max_failures_before_dead: int = 3
    # How often (seconds) the background health checker tests dead proxies.
    health_check_interval_s: float = 300.0  # 5 minutes


# ---------------------------------------------------------------------------
# Singleton proxy pool
# ---------------------------------------------------------------------------


class ProxyPool:
    """Persistent proxy pool with randomised selection and cooldown tracking.

    Usage
    -----
    Initialise once at startup::

        pool = ProxyPool.get_instance()
        pool.load_proxies_from_json(config_path)

    Consume in a transport::

        proxy = pool.get_available_proxy(provider_id="opencode")
        if proxy is None:
            # → use direct IP
        else:
            # → send request via proxy
            # on success: pool.report_success(proxy, response_ms)
            # on 429:     pool.report_rate_limit(proxy, cooldown_hours)
            # on error:   pool.report_failure(proxy, error_type)
    """

    _instance: ClassVar[ProxyPool | None] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()

    # ------------------------------------------------------------------
    # Singleton lifecycle
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> ProxyPool:
        """Return the global singleton, creating it if necessary."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Tear down the singleton (testing / shutdown)."""
        cls._instance = None

    # ------------------------------------------------------------------
    # Instance
    # ------------------------------------------------------------------

    def __init__(self, settings: ProxyPoolSettings | None = None) -> None:
        self._settings = settings or ProxyPoolSettings()
        self._db_path = self._resolve_db_path()

        self._init_db()
        count = self._count_proxies()
        logger.info(
            "PROXY_POOL: Initialised with {} proxies, "
            "cooldown_default={}h, max_failures={}, health_check={}s",
            count,
            self._settings.cooldown_default_hours,
            self._settings.max_failures_before_dead,
            self._settings.health_check_interval_s,
        )

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_db_path() -> Path:
        """Return the path to the shared token-tracking database."""
        from config.paths import config_dir_path

        return config_dir_path() / "token_tracking.db"

    def _get_conn(self) -> sqlite3.Connection:
        """Open a connection with WAL mode and row factory."""
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create the ``proxy_pool`` table if it doesn't exist.

        .. important::
           This table lives in the **same** database as ``token_usage``.
           Only the ``proxy_pool`` table is touched — token data is never
           modified by this class.
        """
        try:
            with self._get_conn() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS proxy_pool (
                        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                        proxy_url           TEXT    NOT NULL UNIQUE,
                        provider            TEXT    DEFAULT '*',
                        country             TEXT,
                        city                TEXT,
                        flag                TEXT,
                        is_alive            INTEGER DEFAULT 1,
                        cooldown_until      TEXT,
                        fail_count          INTEGER DEFAULT 0,
                        consecutive_failures INTEGER DEFAULT 0,
                        total_requests      INTEGER DEFAULT 0,
                        total_errors        INTEGER DEFAULT 0,
                        total_cooldowns     INTEGER DEFAULT 0,
                        avg_response_ms     REAL    DEFAULT 0.0,
                        last_success_at     TEXT,
                        last_error_at       TEXT,
                        last_error_type     TEXT,
                        last_used_at        TEXT,
                        created_at          TEXT    DEFAULT (datetime('now'))
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_proxy_pool_lookup
                    ON proxy_pool(is_alive, cooldown_until, provider)
                """)
                conn.commit()
        except Exception as exc:
            logger.error("PROXY_POOL: Failed to initialise database: {}", exc)
            raise

    def _count_proxies(self) -> int:
        """Return the number of rows currently in the pool."""
        try:
            with self._get_conn() as conn:
                row = conn.execute("SELECT COUNT(*) AS cnt FROM proxy_pool").fetchone()
                return row["cnt"] if row else 0
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Seed proxies from JSON config  (same format as ip_rotation.json)
    # ------------------------------------------------------------------

    def load_proxies_from_json(self, json_path: str | Path) -> int:
        """Load proxy entries from the JSON config file.

        Uses ``INSERT OR IGNORE`` so existing rows are never overwritten.
        Returns the number of new proxies imported.
        """
        path = Path(json_path)
        if not path.exists():
            logger.warning("PROXY_POOL: Config file not found: {}", path)
            return 0

        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            proxies: list[str] = data.get("proxies", [])
            metadata: dict[str, dict[str, str]] = data.get("proxy_metadata", {})

            if not proxies:
                logger.info("PROXY_POOL: No proxies in config: {}", path)
                return 0

            imported = 0
            with self._get_conn() as conn:
                for url in proxies:
                    url = str(url).strip()
                    if not url:
                        continue

                    # Extract host for metadata lookup
                    host = _extract_host(url)
                    meta = metadata.get(host, {})

                    conn.execute(
                        """
                        INSERT OR IGNORE INTO proxy_pool
                            (proxy_url, country, city, flag, provider)
                        VALUES (?, ?, ?, ?, '*')
                        """,
                        (url, meta.get("country"), meta.get("city"), meta.get("flag")),
                    )
                    if conn.total_changes > 0:
                        imported += 1
                conn.commit()

            logger.info(
                "PROXY_POOL: Imported {} new proxies from {} ({} total)",
                imported,
                path,
                self._count_proxies(),
            )
            return imported
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("PROXY_POOL: Failed to load {}: {}", path, exc)
            return 0

    # ------------------------------------------------------------------
    # Core pool operations
    # ------------------------------------------------------------------

    def get_available_proxy(self, provider_id: str = "*") -> str | None:
        """Return a random healthy proxy URL, or ``None`` if none available.

        ``None`` means the caller should use direct computer IP.

        Selection rules
        ---------------
        * ``is_alive = 1``
        * ``cooldown_until`` is ``NULL`` or in the past
        * ``provider`` is ``'*'`` (wildcard, matches all providers)
        * Random order, LIMIT 1
        """
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    """
                    SELECT proxy_url FROM proxy_pool
                    WHERE is_alive = 1
                      AND (cooldown_until IS NULL OR cooldown_until < datetime('now'))
                      AND (provider = '*' OR provider = ?)
                    ORDER BY RANDOM()
                    LIMIT 1
                    """,
                    (provider_id,),
                ).fetchone()

                if row is None:
                    return None

                proxy_url = row["proxy_url"]

                # Mark last_used_at
                conn.execute(
                    "UPDATE proxy_pool SET last_used_at = datetime('now') WHERE proxy_url = ?",
                    (proxy_url,),
                )
                conn.commit()
                return proxy_url
        except Exception as exc:
            logger.error("PROXY_POOL: get_available_proxy failed: {}", exc)
            return None

    def report_success(self, proxy_url: str, response_ms: float = 0.0) -> None:
        """Record a successful request through *proxy_url*."""
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    UPDATE proxy_pool SET
                        is_alive = 1,
                        fail_count = 0,
                        consecutive_failures = 0,
                        total_requests = total_requests + 1,
                        last_success_at = datetime('now'),
                        avg_response_ms = CASE
                            WHEN avg_response_ms = 0 THEN ?
                            ELSE (avg_response_ms * (total_requests - 1) + ?) / total_requests
                        END
                    WHERE proxy_url = ?
                    """,
                    (response_ms, response_ms, proxy_url),
                )
                conn.commit()
        except Exception as exc:
            logger.debug("PROXY_POOL: report_success failed: {}", exc)

    def report_rate_limit(
        self, proxy_url: str, cooldown_hours: float | None = None
    ) -> None:
        """Set *proxy_url* on cooldown after a rate-limit (HTTP 429).

        If *cooldown_hours* is ``None``, the default from settings is used.
        """
        hours = (
            cooldown_hours
            if cooldown_hours is not None
            else self._settings.cooldown_default_hours
        )
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    UPDATE proxy_pool SET
                        cooldown_until = datetime('now', '+' || ? || ' hours'),
                        total_cooldowns = total_cooldowns + 1,
                        total_errors = total_errors + 1,
                        last_error_at = datetime('now'),
                        last_error_type = 'rate_limit'
                    WHERE proxy_url = ?
                    """,
                    (str(hours), proxy_url),
                )
                conn.commit()
            logger.info(
                "PROXY_POOL: Rate-limited {} — cooldown for {}h",
                _mask_url(proxy_url),
                hours,
            )
        except Exception as exc:
            logger.debug("PROXY_POOL: report_rate_limit failed: {}", exc)

    def report_failure(self, proxy_url: str, error_type: str = "unknown") -> None:
        """Record a failed request through *proxy_url*.

        If consecutive failures reach the threshold, the proxy is marked dead.
        """
        try:
            with self._get_conn() as conn:
                cur = conn.execute(
                    """
                    UPDATE proxy_pool SET
                        fail_count = fail_count + 1,
                        consecutive_failures = consecutive_failures + 1,
                        total_errors = total_errors + 1,
                        last_error_at = datetime('now'),
                        last_error_type = ?
                    WHERE proxy_url = ?
                    RETURNING consecutive_failures
                    """,
                    (error_type, proxy_url),
                )
                row = cur.fetchone()
                conn.commit()

                if (
                    row
                    and row["consecutive_failures"]
                    >= self._settings.max_failures_before_dead
                ):
                    self._set_dead(proxy_url)
        except Exception as exc:
            logger.debug("PROXY_POOL: report_failure failed: {}", exc)

    def _set_dead(self, proxy_url: str) -> None:
        """Mark *proxy_url* as dead (is_alive = 0)."""
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE proxy_pool SET is_alive = 0 WHERE proxy_url = ?",
                    (proxy_url,),
                )
                conn.commit()
            logger.warning("PROXY_POOL: Marked {} as dead", _mask_url(proxy_url))
        except Exception as exc:
            logger.debug("PROXY_POOL: _set_dead failed: {}", exc)

    # ------------------------------------------------------------------
    # Admin helpers
    # ------------------------------------------------------------------

    def get_all_proxies(self) -> list[dict[str, Any]]:
        """Return all proxies with full status for the admin UI."""
        try:
            with self._get_conn() as conn:
                rows = conn.execute("SELECT * FROM proxy_pool ORDER BY id").fetchall()
                return [dict(row) for row in rows]
        except Exception as exc:
            logger.error("PROXY_POOL: get_all_proxies failed: {}", exc)
            return []

    def get_proxy_stats(self) -> dict[str, int]:
        """Return aggregate counts for the admin UI."""
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    """
                    SELECT
                        COUNT(*)                                                              AS total,
                        COALESCE(SUM(CASE WHEN is_alive = 1 THEN 1 ELSE 0 END), 0)           AS alive,
                        COALESCE(SUM(CASE WHEN is_alive = 0 THEN 1 ELSE 0 END), 0)           AS dead,
                        COALESCE(SUM(CASE WHEN cooldown_until > datetime('now') THEN 1 ELSE 0 END), 0) AS cooldown
                    FROM proxy_pool
                    """
                ).fetchone()
                if row:
                    return {
                        "total": row["total"],
                        "alive": row["alive"],
                        "dead": row["dead"],
                        "on_cooldown": row["cooldown"],
                    }
                return {"total": 0, "alive": 0, "dead": 0, "on_cooldown": 0}
        except Exception as exc:
            logger.error("PROXY_POOL: get_proxy_stats failed: {}", exc)
            return {"total": 0, "alive": 0, "dead": 0, "on_cooldown": 0}

    def reset_proxy(self, proxy_url: str) -> bool:
        """Reset a single proxy — alive, no cooldown, zero failures."""
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    UPDATE proxy_pool SET
                        is_alive = 1,
                        cooldown_until = NULL,
                        fail_count = 0,
                        consecutive_failures = 0
                    WHERE proxy_url = ?
                    """,
                    (proxy_url,),
                )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("PROXY_POOL: reset_proxy failed: {}", exc)
            return False

    def reset_all_cooldowns(self) -> int:
        """Reset every proxy — alive, no cooldown, zero failures.

        Returns the number of rows updated.
        """
        try:
            with self._get_conn() as conn:
                cur = conn.execute(
                    """
                    UPDATE proxy_pool SET
                        is_alive = 1,
                        cooldown_until = NULL,
                        fail_count = 0,
                        consecutive_failures = 0
                    """
                )
                conn.commit()
                return cur.rowcount
        except Exception as exc:
            logger.error("PROXY_POOL: reset_all_cooldowns failed: {}", exc)
            return 0

    # ------------------------------------------------------------------
    # Background health checker
    # ------------------------------------------------------------------

    async def health_check(self) -> int:
        """Test a few currently-dead or cooldown proxies with fast timeouts.

        Returns the number of proxies revived.
        """
        revived = 0
        candidates = self._health_check_candidates()
        if not candidates:
            return 0

        import httpx

        timeout = httpx.Timeout(
            5.0, connect=self._settings.proxy_connect_timeout, read=5.0, write=5.0
        )

        for proxy_url in candidates:
            ok, resp_ms = await _test_proxy_connectivity(proxy_url, timeout)
            if ok:
                with self._get_conn() as conn:
                    conn.execute(
                        """
                        UPDATE proxy_pool SET
                            is_alive = 1,
                            cooldown_until = NULL,
                            consecutive_failures = 0,
                            avg_response_ms = CASE
                                WHEN avg_response_ms = 0 THEN ?
                                ELSE (avg_response_ms * 0.7 + ? * 0.3)
                            END
                        WHERE proxy_url = ?
                        """,
                        (resp_ms, resp_ms, proxy_url),
                    )
                    conn.commit()
                revived += 1
                logger.info(
                    "PROXY_POOL: Health check revived {} ({}ms)",
                    _mask_url(proxy_url),
                    resp_ms,
                )
            else:
                logger.debug(
                    "PROXY_POOL: Health check still dead: {}", _mask_url(proxy_url)
                )

        if revived:
            alive = self.get_proxy_stats().get("alive", 0)
            logger.info(
                "PROXY_POOL: Health check revived {} proxies ({} alive)", revived, alive
            )

        return revived

    def _health_check_candidates(self) -> list[str]:
        """Return up to 3 proxy URLs that need testing."""
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT proxy_url FROM proxy_pool
                    WHERE is_alive = 0
                       OR cooldown_until > datetime('now')
                    ORDER BY RANDOM()
                    LIMIT 3
                    """
                ).fetchall()
                return [r["proxy_url"] for r in rows]
        except Exception:
            return []

    async def start_background_health_check(self) -> None:
        """Run the health checker loop indefinitely (call from startup)."""
        logger.info(
            "PROXY_POOL: Background health checker started (interval={}s)",
            self._settings.health_check_interval_s,
        )
        import asyncio

        while True:
            await asyncio.sleep(self._settings.health_check_interval_s)
            try:
                await self.health_check()
            except Exception as exc:
                logger.debug("PROXY_POOL: Health check run failed: {}", exc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_host(proxy_url: str) -> str:
    """Extract the hostname from a ``http://user:pass@host:port`` URL."""
    try:
        after_scheme = proxy_url.split("://", 1)[1]
        after_at = (
            after_scheme.split("@", 1)[1] if "@" in after_scheme else after_scheme
        )
        return after_at.split(":")[0]
    except Exception:
        return proxy_url


def _mask_url(proxy_url: str) -> str:
    """Return a log-safe version (host:port only, no credentials)."""
    return _extract_host(proxy_url)


async def _test_proxy_connectivity(
    proxy_url: str, timeout: httpx.Timeout
) -> tuple[bool, float]:
    """Check whether *proxy_url* can reach a known endpoint.

    Returns ``(success, response_ms)``.
    """
    import httpx

    start = time.monotonic()
    try:
        transport = httpx.AsyncHTTPTransport(proxy=proxy_url, verify=False)
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            # Try a fast endpoint — ipv4.webshare.io is quick but
            # fall back to a generic reachability test.
            resp = await client.get("https://httpbin.org/ip", timeout=timeout)
            elapsed_ms = (time.monotonic() - start) * 1000
            return resp.is_success, round(elapsed_ms, 1)
    except Exception:
        elapsed_ms = (time.monotonic() - start) * 1000
        return False, round(elapsed_ms, 1)
