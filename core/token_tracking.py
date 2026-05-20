"""Token usage tracking and analytics.

Tracks token consumption hierarchically:
- Total across all providers
- Per provider total
- Per model within each provider
- Input/output tokens separately
"""

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import ClassVar

from loguru import logger


@dataclass
class TokenStats:
    """Token statistics for a single entity (provider or model)."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    request_count: int = 0
    last_updated: datetime = field(default_factory=datetime.utcnow)

    def add(self, input_tokens: int, output_tokens: int, count: int = 1) -> None:
        """Add tokens to this stat."""
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.total_tokens += input_tokens + output_tokens
        self.request_count += count
        self.last_updated = datetime.utcnow()

    def to_dict(self) -> dict:
        """Convert to dictionary representation."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "request_count": self.request_count,
            "avg_tokens_per_request": (
                self.total_tokens // self.request_count
                if self.request_count > 0
                else 0
            ),
            "last_updated": self.last_updated.isoformat(),
        }


class TokenTracker:
    """Centralized token usage tracker with provider/model hierarchy."""

    _instance: ClassVar["TokenTracker | None"] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self):
        """Initialize the token tracker."""
        # Structure: {provider_id: {model_id: TokenStats}}
        self._tokens_by_provider: dict[str, dict[str, TokenStats]] = defaultdict(
            lambda: defaultdict(TokenStats)
        )
        # Global total
        self._total_tokens = TokenStats()
        # Lock for thread safety
        self._data_lock = threading.Lock()
        # Time window stats (for analytics)
        self._time_windows: dict[str, TokenStats] = defaultdict(TokenStats)

    @classmethod
    def get_instance(cls) -> "TokenTracker":
        """Get singleton instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance (for testing)."""
        with cls._lock:
            cls._instance = None

    def add_tokens(
        self,
        provider_id: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Record token usage for a request.

        Args:
            provider_id: Provider identifier (e.g., "nvidia_nim")
            model_id: Model identifier (e.g., "z-ai/glm4.7")
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
        """
        with self._data_lock:
            # Add to provider/model breakdown
            self._tokens_by_provider[provider_id][model_id].add(
                input_tokens, output_tokens
            )

            # Add to global total
            self._total_tokens.add(input_tokens, output_tokens)

            # Add to time-window stats (for trending)
            now = datetime.utcnow()
            window_key = now.strftime("%Y-%m-%d %H:00")  # Hourly buckets
            self._time_windows[window_key].add(input_tokens, output_tokens)

            logger.debug(
                "Token usage recorded: provider={} model={} "
                "input={} output={} total={}",
                provider_id,
                model_id,
                input_tokens,
                output_tokens,
                input_tokens + output_tokens,
            )

    def get_total_tokens(self) -> TokenStats:
        """Get global token statistics."""
        with self._data_lock:
            return self._total_tokens

    def get_provider_total(self, provider_id: str) -> TokenStats | None:
        """Get total tokens for a specific provider."""
        with self._data_lock:
            if provider_id not in self._tokens_by_provider:
                return None

            # Calculate total by summing all models in provider
            total = TokenStats()
            for model_stats in self._tokens_by_provider[provider_id].values():
                total.add(
                    model_stats.input_tokens,
                    model_stats.output_tokens,
                    model_stats.request_count,
                )
            return total

    def get_provider_models(self, provider_id: str) -> dict[str, TokenStats] | None:
        """Get token stats for all models within a provider."""
        with self._data_lock:
            if provider_id not in self._tokens_by_provider:
                return None
            return dict(self._tokens_by_provider[provider_id])

    def get_all_providers(self) -> dict[str, dict[str, TokenStats]]:
        """Get complete hierarchical view: provider -> model -> stats."""
        with self._data_lock:
            # Deep copy to avoid external modifications
            return {
                provider: dict(models)
                for provider, models in self._tokens_by_provider.items()
            }

    def get_report(self) -> dict:
        """Get comprehensive token usage report.

        Returns:
            Dict with full hierarchy and statistics:
            {
                "total": TokenStats dict,
                "by_provider": {
                    "provider_id": {
                        "total": TokenStats dict,
                        "by_model": {
                            "model_id": TokenStats dict,
                            ...
                        }
                    },
                    ...
                },
                "timestamp": ISO datetime string
            }
        """
        with self._data_lock:
            report = {
                "total": self._total_tokens.to_dict(),
                "by_provider": {},
                "timestamp": datetime.utcnow().isoformat(),
            }

            for provider_id, models in self._tokens_by_provider.items():
                # Calculate provider total
                provider_total = TokenStats()
                for model_stats in models.values():
                    provider_total.add(
                        model_stats.input_tokens,
                        model_stats.output_tokens,
                        model_stats.request_count,
                    )

                # Build provider entry
                report["by_provider"][provider_id] = {
                    "total": provider_total.to_dict(),
                    "by_model": {
                        model_id: stats.to_dict() for model_id, stats in models.items()
                    },
                }

            return report

    def get_model_across_providers(self, model_name: str) -> dict[str, TokenStats]:
        """Get usage of a specific model across all providers that have it.

        Useful for comparing same model (e.g., "gpt-4") across providers.
        """
        with self._data_lock:
            result = {}
            for provider_id, models in self._tokens_by_provider.items():
                for model_id, stats in models.items():
                    # Extract base model name (e.g., "gpt-4" from "openrouter/gpt-4")
                    if model_name.lower() in model_id.lower():
                        full_key = f"{provider_id}/{model_id}"
                        result[full_key] = stats
            return result

    def get_time_window_stats(
        self, hours_back: int = 24
    ) -> dict[str, dict]:
        """Get token usage by hour for the last N hours.

        Returns dict with hourly breakdown for trending analysis.
        """
        with self._data_lock:
            cutoff = datetime.utcnow() - timedelta(hours=hours_back)
            result = {}
            for window_key, stats in self._time_windows.items():
                try:
                    window_time = datetime.strptime(window_key, "%Y-%m-%d %H")
                    if window_time >= cutoff:
                        result[window_key] = stats.to_dict()
                except ValueError:
                    pass
            return result

    def clear(self) -> None:
        """Clear all tracked tokens (for testing or reset)."""
        with self._data_lock:
            self._tokens_by_provider.clear()
            self._total_tokens = TokenStats()
            self._time_windows.clear()
            logger.info("Token tracker cleared")


# Global instance access
def get_token_tracker() -> TokenTracker:
    """Get the global token tracker instance."""
    return TokenTracker.get_instance()
