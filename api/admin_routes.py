"""Local admin UI routes and APIs."""

from __future__ import annotations

import inspect
import ipaddress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from config.settings import Settings
from config.settings import get_settings as get_cached_settings
from core.token_tracking import get_token_tracker
from providers.registry import ProviderRegistry

from .admin_config import (
    FIELD_BY_KEY,
    load_config_response,
    provider_config_status,
    validate_updates,
    write_managed_env,
)
from .admin_urls import local_admin_url

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent / "admin_static"
LOCAL_PROVIDER_PATHS = {
    "lmstudio": "/models",
    "llamacpp": "/models",
    "ollama": "/api/tags",
}


class AdminConfigPayload(BaseModel):
    """Partial config update submitted by the admin UI."""

    values: dict[str, Any] = Field(default_factory=dict)


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _origin_is_local(origin: str | None) -> bool:
    if not origin:
        return True
    parsed = urlsplit(origin)
    return _is_loopback_host(parsed.hostname)


def require_loopback_admin(request: Request) -> None:
    """Allow admin access only from the local machine."""

    client_host = request.client.host if request.client else None
    if not _is_loopback_host(client_host):
        raise HTTPException(status_code=403, detail="Admin UI is local-only")

    origin = request.headers.get("origin")
    if not _origin_is_local(origin):
        raise HTTPException(status_code=403, detail="Admin UI is local-only")


def _asset_response(filename: str) -> FileResponse:
    path = STATIC_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Admin asset not found")
    return FileResponse(path)


@router.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    require_loopback_admin(request)
    return _asset_response("index.html")


@router.get("/admin/assets/{filename}", include_in_schema=False)
async def admin_asset(filename: str, request: Request):
    require_loopback_admin(request)
    if filename not in {"admin.css", "admin.js"}:
        raise HTTPException(status_code=404, detail="Admin asset not found")
    return _asset_response(filename)


@router.get("/admin/api/config")
async def get_admin_config(request: Request):
    require_loopback_admin(request)
    return load_config_response()


@router.post("/admin/api/config/validate")
async def validate_admin_config(payload: AdminConfigPayload, request: Request):
    require_loopback_admin(request)
    return validate_updates(_filtered_values(payload.values))


@router.post("/admin/api/config/apply")
async def apply_admin_config(
    payload: AdminConfigPayload,
    request: Request,
    background_tasks: BackgroundTasks,
):
    require_loopback_admin(request)
    result = write_managed_env(_filtered_values(payload.values))
    if not result["applied"]:
        return result

    get_cached_settings.cache_clear()
    restart = _restart_metadata(result["pending_fields"], request)
    result["restart"] = restart
    if restart["required"] and restart["automatic"]:
        callback = request.app.state.admin_restart_callback
        background_tasks.add_task(_invoke_admin_restart_callback, callback)
        request.app.state.admin_pending_fields = []
        return result

    old_registry = getattr(request.app.state, "provider_registry", None)
    if isinstance(old_registry, ProviderRegistry):
        await old_registry.cleanup()
    request.app.state.provider_registry = ProviderRegistry()
    request.app.state.admin_pending_fields = result["pending_fields"]
    return result


@router.get("/admin/api/status")
async def admin_status(request: Request):
    require_loopback_admin(request)
    settings = get_cached_settings()
    registry = getattr(request.app.state, "provider_registry", None)
    cached_models: dict[str, list[str]] = {}
    if isinstance(registry, ProviderRegistry):
        cached_models = {
            provider_id: sorted(model_ids)
            for provider_id, model_ids in registry.cached_model_ids().items()
        }
    return {
        "status": "running",
        "host": settings.host,
        "port": settings.port,
        "model": settings.model,
        "provider": settings.provider_type,
        "pending_fields": getattr(request.app.state, "admin_pending_fields", []),
        "provider_status": provider_config_status(),
        "cached_models": cached_models,
    }


@router.get("/admin/api/providers/local-status")
async def local_provider_status(request: Request):
    require_loopback_admin(request)
    config = load_config_response()
    values = {field["key"]: field["value"] for field in config["fields"]}
    checks = []
    for provider_id, path in LOCAL_PROVIDER_PATHS.items():
        base_url = _local_provider_url(provider_id, values)
        checks.append(await _check_local_provider(provider_id, base_url, path))
    return {"providers": checks}


@router.post("/admin/api/providers/{provider_id}/test")
async def test_provider(provider_id: str, request: Request):
    require_loopback_admin(request)
    settings = get_cached_settings()
    registry = getattr(request.app.state, "provider_registry", None)
    if not isinstance(registry, ProviderRegistry):
        registry = ProviderRegistry()
        request.app.state.provider_registry = registry
    try:
        provider = registry.get(provider_id, settings)
        infos = await provider.list_model_infos()
    except Exception as exc:
        return {
            "provider_id": provider_id,
            "ok": False,
            "error_type": type(exc).__name__,
        }
    registry.cache_model_infos(provider_id, infos)
    return {
        "provider_id": provider_id,
        "ok": True,
        "models": sorted(info.model_id for info in infos),
    }


@router.post("/admin/api/models/refresh")
async def refresh_models(request: Request):
    require_loopback_admin(request)
    settings = get_cached_settings()
    registry = getattr(request.app.state, "provider_registry", None)
    if not isinstance(registry, ProviderRegistry):
        registry = ProviderRegistry()
        request.app.state.provider_registry = registry
    await registry.refresh_model_list_cache(settings)
    return {
        "cached_models": {
            provider_id: sorted(model_ids)
            for provider_id, model_ids in registry.cached_model_ids().items()
        }
    }


def _filtered_values(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key in FIELD_BY_KEY}


async def _invoke_admin_restart_callback(callback: Any) -> None:
    result = callback()
    if inspect.isawaitable(result):
        await result


def _restart_metadata(fields: list[str], request: Request) -> dict[str, Any]:
    callback = getattr(request.app.state, "admin_restart_callback", None)
    automatic = bool(fields and callable(callback))
    return {
        "required": bool(fields),
        "automatic": automatic,
        "admin_url": _next_admin_url() if automatic else None,
        "fields": fields,
    }


def _next_admin_url() -> str:
    fields = {
        field["key"]: field["value"] for field in load_config_response()["fields"]
    }
    settings = Settings.model_construct(
        host=fields.get("HOST") or "0.0.0.0",
        port=int(fields.get("PORT") or 8082),
    )
    return local_admin_url(settings)


def _local_provider_url(provider_id: str, values: dict[str, str]) -> str:
    if provider_id == "lmstudio":
        return values.get("LM_STUDIO_BASE_URL", "")
    if provider_id == "llamacpp":
        return values.get("LLAMACPP_BASE_URL", "")
    if provider_id == "ollama":
        return values.get("OLLAMA_BASE_URL", "")
    return ""


async def _check_local_provider(
    provider_id: str, base_url: str, path: str
) -> dict[str, Any]:
    clean_url = base_url.strip().rstrip("/")
    if not clean_url:
        return {
            "provider_id": provider_id,
            "status": "missing_url",
            "label": "Missing URL",
            "base_url": base_url,
        }

    url = f"{clean_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            response = await client.get(url)
        ok = 200 <= response.status_code < 300
        return {
            "provider_id": provider_id,
            "status": "reachable" if ok else "offline",
            "label": "Reachable" if ok else "Offline",
            "base_url": base_url,
            "status_code": response.status_code,
        }
    except Exception as exc:
        return {
            "provider_id": provider_id,
            "status": "offline",
            "label": "Offline",
            "base_url": base_url,
            "error_type": type(exc).__name__,
        }


# =============================================================================
# Token Tracking API Endpoints
# =============================================================================


@router.get("/admin/api/tokens")
async def get_token_stats():
    """Get comprehensive token usage report with provider/model breakdown."""
    tracker = get_token_tracker()
    return tracker.get_report()


@router.get("/admin/api/tokens/total")
async def get_total_tokens():
    """Get global token statistics."""
    tracker = get_token_tracker()
    total = tracker.get_total_tokens()
    return total.to_dict()


@router.get("/admin/api/tokens/provider/{provider_id}")
async def get_provider_tokens(provider_id: str):
    """Get token statistics for a specific provider."""
    tracker = get_token_tracker()
    provider_stats = tracker.get_provider_total(provider_id)
    if provider_stats is None:
        raise HTTPException(status_code=404, detail=f"Provider {provider_id} not found")
    return provider_stats.to_dict()


@router.get("/admin/api/tokens/provider/{provider_id}/models")
async def get_provider_model_tokens(provider_id: str):
    """Get token statistics broken down by model within a provider."""
    tracker = get_token_tracker()
    models = tracker.get_provider_models(provider_id)
    if models is None:
        raise HTTPException(status_code=404, detail=f"Provider {provider_id} not found")
    
    return {
        "provider_id": provider_id,
        "models": {model_id: stats.to_dict() for model_id, stats in models.items()},
    }


@router.get("/admin/api/tokens/hierarchy")
async def get_tokens_hierarchy():
    """Get complete hierarchical view: All Providers -> Models -> Stats."""
    tracker = get_token_tracker()
    all_providers = tracker.get_all_providers()
    
    hierarchy = {}
    for provider_id, models in all_providers.items():
        # Calculate provider total
        provider_input = sum(m.input_tokens for m in models.values())
        provider_output = sum(m.output_tokens for m in models.values())
        provider_total = provider_input + provider_output
        provider_requests = sum(m.request_count for m in models.values())
        
        hierarchy[provider_id] = {
            "total": {
                "input_tokens": provider_input,
                "output_tokens": provider_output,
                "total_tokens": provider_total,
                "request_count": provider_requests,
            },
            "models": {
                model_id: stats.to_dict() for model_id, stats in models.items()
            },
        }
    
    return {
        "global_total": tracker.get_total_tokens().to_dict(),
        "by_provider": hierarchy,
    }


@router.get("/admin/api/tokens/model/{model_name}")
async def get_model_across_providers(model_name: str):
    """Get usage of a specific model across all providers."""
    tracker = get_token_tracker()
    usage = tracker.get_model_across_providers(model_name)
    if not usage:
        raise HTTPException(
            status_code=404, detail=f"Model {model_name} not found in any provider"
        )
    
    return {
        "model_name": model_name,
        "providers": {key: stats.to_dict() for key, stats in usage.items()},
    }


@router.get("/admin/api/tokens/history")
async def get_token_history(hours_back: int = 24):
    """Get token usage history by hour for trending."""
    tracker = get_token_tracker()
    history = tracker.get_time_window_stats(hours_back=hours_back)
    return {
        "period_hours": hours_back,
        "hourly": history,
    }


@router.post("/admin/api/tokens/reset")
async def reset_token_tracker():
    """Reset all token tracking (for testing/debugging)."""
    tracker = get_token_tracker()
    tracker.clear()
    return {"status": "cleared", "message": "Token tracker has been reset"}


@router.get("/admin/api/tokens/storage")
async def get_token_storage_info():
    """Get information about persistent token storage (database)."""
    from pathlib import Path
    import os
    
    tracker = get_token_tracker()
    db_path = tracker._db_path
    
    info = {
        "database_location": str(db_path),
        "database_exists": db_path.exists(),
    }
    
    if db_path.exists():
        # Get file size
        size_bytes = os.path.getsize(db_path)
        
        # Get record count
        try:
            import sqlite3
            with sqlite3.connect(db_path) as conn:
                cursor = conn.execute("SELECT COUNT(*) FROM token_usage")
                record_count = cursor.fetchone()[0]
                
                cursor = conn.execute(
                    "SELECT MIN(recorded_at), MAX(recorded_at) FROM token_usage"
                )
                min_date, max_date = cursor.fetchone()
                
                info.update({
                    "size_bytes": size_bytes,
                    "size_mb": round(size_bytes / (1024 * 1024), 2),
                    "record_count": record_count,
                    "earliest_record": min_date,
                    "latest_record": max_date,
                    "retention_days": 30,
                    "persistence": "enabled",
                })
        except Exception as e:
            info["error"] = str(e)
    
    return info

