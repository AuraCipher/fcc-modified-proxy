"""NVIDIA NIM provider implementation."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import openai
from loguru import logger
from openai import AsyncOpenAI

from config.nim import NimSettings
from core.proxy_pool import set_request_proxy
from providers.base import ProviderConfig
from providers.defaults import NVIDIA_NIM_DEFAULT_BASE
from providers.openai_compat import OpenAIChatTransport
from providers.rate_limit import retryable_upstream_status

from .key_pool import NimApiKeyPool
from .request import (
    body_without_nim_tool_argument_aliases,
    build_request_body,
    clone_body_without_chat_template,
    clone_body_without_reasoning_budget,
    clone_body_without_reasoning_content,
    nim_tool_argument_aliases_from_body,
)


def _build_nim_openai_client(
    config: ProviderConfig,
    *,
    base_url: str,
    api_key: str,
) -> AsyncOpenAI:
    http_client = None
    if config.proxy:
        http_client = httpx.AsyncClient(
            proxy=config.proxy,
            timeout=httpx.Timeout(
                config.http_read_timeout,
                connect=config.http_connect_timeout,
                read=config.http_read_timeout,
                write=config.http_write_timeout,
            ),
        )
    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        max_retries=0,
        timeout=httpx.Timeout(
            config.http_read_timeout,
            connect=config.http_connect_timeout,
            read=config.http_read_timeout,
            write=config.http_write_timeout,
        ),
        http_client=http_client,
    )


class NvidiaNimProvider(OpenAIChatTransport):
    """NVIDIA NIM provider using official OpenAI client."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        nim_settings: NimSettings,
        api_keys: tuple[str, ...] | None = None,
        rpm_per_key: int = 35,
        key_window_sec: float = 60.0,
        key_cooldown_sec: float = 65.0,
        key_switch_delay_sec: float = 5.0,
    ):
        keys = api_keys if api_keys else ((config.api_key,) if config.api_key else ())
        if not keys:
            raise ValueError("At least one NVIDIA NIM API key is required")

        primary_config = config.model_copy(update={"api_key": keys[0]})
        super().__init__(
            primary_config,
            provider_name="NIM",
            base_url=config.base_url or NVIDIA_NIM_DEFAULT_BASE,
            api_key=keys[0],
        )
        self._nim_settings = nim_settings
        self._base_url = (config.base_url or NVIDIA_NIM_DEFAULT_BASE).rstrip("/")
        self._nim_config = config
        self._api_key = keys[0]
        self._key_pool = NimApiKeyPool(
            keys,
            rpm_per_key=rpm_per_key,
            window_seconds=key_window_sec,
            cooldown_seconds=key_cooldown_sec,
            switch_delay_seconds=key_switch_delay_sec,
            client_factory=lambda api_key: _build_nim_openai_client(
                config,
                base_url=self._base_url,
                api_key=api_key,
            ),
        )
        self._key_pool.bind_primary_client(self._client)
        self._use_pooled_keys = len(keys) > 1
        if self._use_pooled_keys:
            logger.info("NIM multi-API rotation enabled ({} keys)", len(keys))

    async def cleanup(self) -> None:
        await self._key_pool.close()
        await super().cleanup()

    async def list_model_ids(self) -> frozenset[str]:
        """Return model ids; multi-key mode uses the pool (respects quarantine)."""
        from providers.model_listing import extract_openai_model_ids

        if self._use_pooled_keys:
            payload = await self._key_pool.execute_with_retry(
                lambda client: client.models.list()
            )
            return extract_openai_model_ids(payload, provider_name=self._provider_name)

        payload = await self._client.models.list()
        return extract_openai_model_ids(payload, provider_name=self._provider_name)

    def _build_request_body(
        self, request: Any, thinking_enabled: bool | None = None
    ) -> dict:
        """Internal helper for tests and shared building."""
        return build_request_body(
            request,
            self._nim_settings,
            thinking_enabled=self._is_thinking_enabled(request, thinking_enabled),
        )

    def _prepare_create_body(self, body: dict[str, Any]) -> dict[str, Any]:
        """Strip private request metadata before calling NVIDIA NIM."""
        return body_without_nim_tool_argument_aliases(body)

    def _tool_argument_aliases(self, body: dict[str, Any]) -> dict[str, dict[str, str]]:
        """Return NIM tool argument aliases captured while building this request."""
        return nim_tool_argument_aliases_from_body(body)

    async def _create_stream(self, body: dict) -> tuple[Any, dict]:
        """Create a streaming chat completion with smart proxy + key rotation.

        Uses the SQLite-backed ``ProxyPool`` to pick **one random healthy proxy**
        per request.  If the proxy fails or no proxy is available, falls back
        to the existing key-pool (or direct) path — **no sequential proxy loop**.
        """
        from core.proxy_pool import ProxyPool

        pool = ProxyPool.get_instance()
        proxy_url = pool.get_available_proxy(self._provider_name.lower())

        if proxy_url is not None:
            return await self._nim_create_stream_via_proxy(body, proxy_url, pool)

        # No healthy proxy → use existing key-pool / direct path
        if self._use_pooled_keys:
            return await self._nim_create_stream_pool(body)
        return await self._nim_create_stream_direct(body)

    async def _nim_create_stream_via_proxy(
        self, body: dict, proxy_url: str, pool: ProxyPool
    ) -> tuple[Any, dict]:
        """Try via *proxy_url* with the primary API key; fall back on failure."""
        import time

        proxy_label = proxy_url.split("@", 1)[1] if "@" in proxy_url else proxy_url
        start = time.monotonic()

        proxy_connect_timeout = getattr(pool._settings, "proxy_connect_timeout", 5.0)
        http_client = httpx.AsyncClient(
            proxy=proxy_url,
            timeout=httpx.Timeout(
                self._config.http_read_timeout,
                connect=proxy_connect_timeout,
                read=self._config.http_read_timeout,
                write=self._config.http_write_timeout,
            ),
        )
        proxied_client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            max_retries=0,
            timeout=httpx.Timeout(
                self._config.http_read_timeout,
                connect=proxy_connect_timeout,
                read=self._config.http_read_timeout,
                write=self._config.http_write_timeout,
            ),
            http_client=http_client,
        )

        create_body = self._prepare_create_body(body)

        try:
            stream = await self._global_rate_limiter.execute_with_retry(
                proxied_client.chat.completions.create,
                **create_body,
                stream=True,
            )
            elapsed_ms = (time.monotonic() - start) * 1000
            pool.report_success(proxy_url, round(elapsed_ms, 1))
            set_request_proxy(f"via {proxy_label}")
            logger.debug(
                "PROXY_POOL: {} request OK via {} ({}ms)",
                self._provider_name,
                proxy_label,
                round(elapsed_ms, 1),
            )
            return stream, body
        except Exception as exc:
            # Detect rate-limit (429) → set cooldown instead of marking dead
            if getattr(exc, "status_code", None) == 429:
                logger.warning(
                    "PROXY_POOL: {} rate-limited (429) via {}, falling back to {} path",
                    self._provider_name,
                    proxy_label,
                    "key pool" if self._use_pooled_keys else "direct",
                )
                pool.report_rate_limit(proxy_url)
            else:
                logger.warning(
                    "PROXY_POOL: {} {} via {}, falling back to {} path",
                    self._provider_name,
                    type(exc).__name__,
                    proxy_label,
                    "key pool" if self._use_pooled_keys else "direct",
                )
                pool.report_failure(proxy_url, type(exc).__name__)
            await http_client.aclose()
            # Fall back to existing pool/direct logic
            if self._use_pooled_keys:
                return await self._nim_create_stream_pool(body)
            return await self._nim_create_stream_direct(body)

    async def _nim_create_stream_pool(self, body: dict) -> tuple[Any, dict]:
        """Multi-key path: rotate through API keys (no IP rotation)."""
        set_request_proxy("direct")
        create_body = self._prepare_create_body(body)

        async def _create_with_client(client: AsyncOpenAI) -> Any:
            return await client.chat.completions.create(**create_body, stream=True)

        async def _run_with_pool(
            runner: Callable[[AsyncOpenAI], Any],
        ) -> Any:
            async def _on_retryable(lease: Any, exc: BaseException) -> None:
                status = retryable_upstream_status(exc)
                if status is not None:
                    await lease.mark_rate_limited(exc=exc)

            return await self._key_pool.execute_with_retry(
                runner,
                on_retryable_error=_on_retryable,
            )

        try:

            async def _create() -> Any:
                return await _run_with_pool(_create_with_client)

            stream = await self._global_rate_limiter.execute_with_retry(
                _create,
                proactive=False,
            )
            return stream, body
        except Exception as error:
            retry_body = self._get_retry_request_body(error, body)
            if retry_body is None:
                raise

            create_retry_body = self._prepare_create_body(retry_body)

            async def _create_retry_with_client(client: AsyncOpenAI) -> Any:
                return await client.chat.completions.create(
                    **create_retry_body, stream=True
                )

            async def _create_retry() -> Any:
                return await _run_with_pool(_create_retry_with_client)

            stream = await self._global_rate_limiter.execute_with_retry(
                _create_retry,
                proactive=False,
            )
            return stream, retry_body

    async def _nim_create_stream_direct(self, body: dict) -> tuple[Any, dict]:
        """Original single-key path (no API key pool, no IP rotation)."""
        set_request_proxy("direct")
        try:
            create_body = self._prepare_create_body(body)
            stream = await self._global_rate_limiter.execute_with_retry(
                self._client.chat.completions.create,
                **create_body,
                stream=True,
            )
            return stream, body
        except Exception as error:
            retry_body = self._get_retry_request_body(error, body)
            if retry_body is None:
                raise

            create_retry_body = self._prepare_create_body(retry_body)
            stream = await self._global_rate_limiter.execute_with_retry(
                self._client.chat.completions.create,
                **create_retry_body,
                stream=True,
            )
            return stream, retry_body

    def _get_retry_request_body(self, error: Exception, body: dict) -> dict | None:
        """Retry once with a downgraded body when NIM rejects a known field."""
        status_code = getattr(error, "status_code", None)
        if not isinstance(error, openai.BadRequestError) and status_code != 400:
            return None

        error_text = str(error)
        error_body = getattr(error, "body", None)
        if error_body is not None:
            error_text = f"{error_text} {json.dumps(error_body, default=str)}"
        error_text = error_text.lower()

        if "reasoning_budget" in error_text:
            retry_body = clone_body_without_reasoning_budget(body)
            if retry_body is None:
                return None
            logger.warning(
                "NIM_STREAM: retrying without reasoning_budget after 400 error"
            )
            return retry_body

        if "chat_template" in error_text:
            retry_body = clone_body_without_chat_template(body)
            if retry_body is None:
                return None
            logger.warning("NIM_STREAM: retrying without chat_template after 400 error")
            return retry_body

        if "reasoning_content" in error_text:
            retry_body = clone_body_without_reasoning_content(body)
            if retry_body is None:
                return None
            logger.warning(
                "NIM_STREAM: retrying without reasoning_content after 400 error"
            )
            return retry_body

        return None
