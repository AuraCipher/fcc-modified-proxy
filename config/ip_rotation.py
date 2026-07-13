"""VPN IP rotation configuration — loaded from env and/or JSON config file."""

from __future__ import annotations

import json
import os
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field, field_validator

from config.paths import config_dir_path

IP_ROTATION_CONFIG_FILENAME = "ip_rotation.json"


def _resolve_config_path() -> Path:
    """Return the user-level JSON config path (~/.fcc/ip_rotation.json)."""
    return config_dir_path() / IP_ROTATION_CONFIG_FILENAME


def _load_proxies_from_json(config_path: Path) -> list[str]:
    """Load proxy entries from a JSON file, or return [] if missing/invalid."""
    if not config_path.exists():
        return []
    try:
        raw = config_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        proxies = data.get("proxies", [])
        if not isinstance(proxies, list):
            logger.warning("IP_ROTATION: JSON config has non-list 'proxies' field")
            return []
        logger.debug(
            "IP_ROTATION: Loaded {} proxies from {}", len(proxies), config_path
        )
        return [str(p).strip() for p in proxies if p and str(p).strip()]
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("IP_ROTATION: Failed to read {}: {}", config_path, exc)
        return []


def _parse_proxies_env(raw: str) -> list[str]:
    """Parse semicolon-separated proxy URLs from an env var."""
    return [p.strip() for p in raw.split(";") if p.strip()]


def resolve_ip_rotation_proxies() -> list[str]:
    """Resolve the final list of proxy URLs from JSON config and/or env."""

    # 1. Try JSON config file first (supports larger lists)
    json_path = _resolve_config_path()
    proxies = _load_proxies_from_json(json_path)

    # 2. Fall back to env var (semicolon-separated)
    env_raw = os.environ.get("IP_ROTATION_PROXIES", "").strip()
    if not proxies and env_raw:
        proxies = _parse_proxies_env(env_raw)
        logger.info(
            "IP_ROTATION: Loaded {} proxies from IP_ROTATION_PROXIES env", len(proxies)
        )

    return proxies


class IpRotationSettings(BaseModel):
    """Configuration for VPN IP rotation across all providers."""

    proxies: list[str] = Field(
        default_factory=list,
        description="VPN proxy URLs in http://user:pass@host:port format",
    )
    max_attempts: int = Field(
        default=0,
        ge=0,
        description="Max rotation attempts (0 = auto = one per proxy + 1 for direct IP)",
    )
    fallback_to_direct: bool = Field(
        default=True,
        description="Whether to try direct computer IP after all VPN proxies fail",
    )

    # --- Proxy Pool Settings (used by core.proxy_pool.ProxyPool) ---
    proxy_connect_timeout: float = Field(
        default=5.0,
        ge=1.0,
        description="Connect timeout (seconds) for proxy connections",
    )
    proxy_read_timeout: float = Field(
        default=20.0,
        ge=5.0,
        description="Read timeout (seconds) for proxy connections",
    )
    cooldown_default_hours: float = Field(
        default=15.0,
        ge=0.0,
        description="Default cooldown hours when a proxy is rate-limited",
    )
    cooldown_by_provider: dict[str, float] = Field(
        default_factory=lambda: {"opencode": 15.0, "zen": 15.0},
        description="Per-provider cooldown overrides (lowercase id -> hours)",
    )
    max_failures_before_dead: int = Field(
        default=3,
        ge=1,
        description="Consecutive failures before marking a proxy dead",
    )
    health_check_interval_minutes: float = Field(
        default=5.0,
        ge=1.0,
        description="Interval (minutes) for background proxy health checks",
    )
    max_response_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Max average response time (ms) before auto-culling a proxy (0 = disabled)",
    )

    @field_validator("proxies", mode="before")
    @classmethod
    def strip_empty_proxies(cls, v: list[str]) -> list[str]:
        if not isinstance(v, list):
            return v
        return [p for p in v if p and p.strip()]

    model_config = {"extra": "forbid"}
