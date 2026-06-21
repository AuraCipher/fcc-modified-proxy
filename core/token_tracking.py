"""Token usage tracking and analytics with persistent storage.

Tracks token consumption hierarchically:
- Total across all providers
- Per provider total
- Per model within each provider
- Input/output tokens separately
- Persists to SQLite database for durability
"""

import sqlite3
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
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
                self.total_tokens // self.request_count if self.request_count > 0 else 0
            ),
            "last_updated": self.last_updated.isoformat(),
        }


def _get_db_path() -> Path:
    """Get the path to the token tracking database."""
    from config.paths import managed_env_path

    # Store in same location as managed env (typically project root)
    base_path = managed_env_path().parent
    db_path = base_path / "token_tracking.db"
    return db_path


class TokenTracker:
    """Centralized token usage tracker with provider/model hierarchy and persistence."""

    _instance: ClassVar[TokenTracker | None] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self):
        """Initialize the token tracker with persistent storage."""
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
        # Database path
        self._db_path = _get_db_path()

        # Initialize database
        self._init_db()
        # Load existing data from database
        self._load_from_db()

    def _init_db(self) -> None:
        """Initialize SQLite database schema."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS token_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        provider_id TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        input_tokens INTEGER DEFAULT 0,
                        output_tokens INTEGER DEFAULT 0,
                        request_count INTEGER DEFAULT 0,
                        recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(provider_id, model_id, recorded_at)
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_provider_model
                    ON token_usage(provider_id, model_id)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_recorded_at
                    ON token_usage(recorded_at)
                """)
                conn.commit()
            logger.debug(f"Token tracking database initialized at {self._db_path}")
        except Exception as e:
            logger.error(f"Failed to initialize token tracking database: {e}")

    def _load_from_db(self) -> None:
        """Load token statistics from database into memory."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.execute("""
                    SELECT provider_id, model_id,
                           SUM(input_tokens), SUM(output_tokens),
                           SUM(request_count)
                    FROM token_usage
                    GROUP BY provider_id, model_id
                """)

                for (
                    provider_id,
                    model_id,
                    input_tokens,
                    output_tokens,
                    request_count,
                ) in cursor:
                    if input_tokens is None:
                        continue
                    self._tokens_by_provider[provider_id][model_id].add(
                        input_tokens or 0,
                        output_tokens or 0,
                        request_count or 0,
                    )
                    self._total_tokens.add(
                        input_tokens or 0,
                        output_tokens or 0,
                        request_count or 0,
                    )

            logger.info("Token tracking data loaded from persistent storage")
        except Exception as e:
            logger.error(f"Failed to load token tracking data from database: {e}")

    def _save_to_db(
        self, provider_id: str, model_id: str, input_tokens: int, output_tokens: int
    ) -> None:
        """Save token usage to database (asynchronous to avoid blocking)."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                # Record at hourly granularity to avoid too many entries
                now = datetime.utcnow()
                hour_bucket = now.replace(minute=0, second=0, microsecond=0)

                conn.execute(
                    """
                    INSERT INTO token_usage
                    (provider_id, model_id, input_tokens, output_tokens, request_count, recorded_at)
                    VALUES (?, ?, ?, ?, 1, ?)
                    ON CONFLICT(provider_id, model_id, recorded_at)
                    DO UPDATE SET
                        input_tokens = input_tokens + ?,
                        output_tokens = output_tokens + ?,
                        request_count = request_count + 1
                """,
                    (
                        provider_id,
                        model_id,
                        input_tokens,
                        output_tokens,
                        hour_bucket,
                        input_tokens,
                        output_tokens,
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"Failed to save to database: {e}")

    @classmethod
    def get_instance(cls) -> TokenTracker:
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

        # Save to database asynchronously
        threading.Thread(
            target=self._save_to_db,
            args=(provider_id, model_id, input_tokens, output_tokens),
            daemon=True,
        ).start()

    def get_history(self, hours: int = 24) -> dict:
        """Get token usage history for the last N hours.

        Args:
            hours: Number of hours to retrieve (default 24)

        Returns:
            Dict mapping hour timestamp to TokenStats
        """
        with self._data_lock:
            cutoff = datetime.utcnow() - timedelta(hours=hours)

            # From database
            try:
                with sqlite3.connect(self._db_path) as conn:
                    cursor = conn.execute(
                        """
                        SELECT recorded_at,
                               SUM(input_tokens), SUM(output_tokens),
                               SUM(request_count)
                        FROM token_usage
                        WHERE recorded_at >= ?
                        GROUP BY recorded_at
                        ORDER BY recorded_at DESC
                    """,
                        (cutoff,),
                    )

                    history = {}
                    for (
                        recorded_at,
                        input_tokens,
                        output_tokens,
                        request_count,
                    ) in cursor:
                        stats = TokenStats()
                        stats.input_tokens = input_tokens or 0
                        stats.output_tokens = output_tokens or 0
                        stats.total_tokens = (input_tokens or 0) + (output_tokens or 0)
                        stats.request_count = request_count or 0
                        history[recorded_at] = stats

                    return history
            except Exception as e:
                logger.error(f"Failed to query history from database: {e}")
                return {}

    def cleanup_old_data(self, days: int = 30) -> int:
        """Clean up token records older than specified days.

        Args:
            days: Number of days to retain (default 30)

        Returns:
            Number of records deleted
        """
        try:
            cutoff = datetime.utcnow() - timedelta(days=days)
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.execute(
                    """
                    DELETE FROM token_usage
                    WHERE recorded_at < ?
                """,
                    (cutoff,),
                )
                conn.commit()
                deleted = cursor.rowcount
                if deleted > 0:
                    logger.info(f"Cleaned up {deleted} old token records")
                return deleted
        except Exception as e:
            logger.error(f"Failed to cleanup old data: {e}")
            return 0

    def export_to_json(self) -> dict:
        """Export all token data to JSON-serializable format.

        Returns:
            Dict with all token data, ready for JSON serialization
        """
        with self._data_lock:
            return {
                "global_total": self._total_tokens.to_dict(),
                "by_provider": {
                    provider: {
                        "by_model": {
                            model_id: stats.to_dict()
                            for model_id, stats in models.items()
                        }
                    }
                    for provider, models in self._tokens_by_provider.items()
                },
                "exported_at": datetime.utcnow().isoformat(),
            }

    def import_from_json(self, data: dict) -> None:
        """Import token data from JSON format.

        Args:
            data: Dict with token data (from export_to_json)
        """
        try:
            with self._data_lock:
                # Load into memory structures
                for provider_id, provider_data in data.get("by_provider", {}).items():
                    for model_id, model_stats_dict in provider_data.get(
                        "by_model", {}
                    ).items():
                        stats = TokenStats(
                            input_tokens=model_stats_dict.get("input_tokens", 0),
                            output_tokens=model_stats_dict.get("output_tokens", 0),
                            total_tokens=model_stats_dict.get("total_tokens", 0),
                            request_count=model_stats_dict.get("request_count", 0),
                        )
                        self._tokens_by_provider[provider_id][model_id] = stats

                # Update global total
                global_data = data.get("global_total", {})
                self._total_tokens = TokenStats(
                    input_tokens=global_data.get("input_tokens", 0),
                    output_tokens=global_data.get("output_tokens", 0),
                    total_tokens=global_data.get("total_tokens", 0),
                    request_count=global_data.get("request_count", 0),
                )

                logger.info("Token data imported successfully")
        except Exception as e:
            logger.error(f"Failed to import token data: {e}")
            raise

    def backup_to_file(self, filepath: str) -> None:
        """Backup token data to a JSON file.

        Args:
            filepath: Path where to save the backup file
        """
        import json

        try:
            export_data = self.export_to_json()
            with open(filepath, "w") as f:
                json.dump(export_data, f, indent=2)
            logger.info(f"Token data backed up to {filepath}")
        except Exception as e:
            logger.error(f"Failed to backup token data: {e}")
            raise

    def restore_from_file(self, filepath: str) -> None:
        """Restore token data from a JSON backup file.

        Args:
            filepath: Path to the backup file to restore
        """
        import json

        try:
            with open(filepath) as f:
                data = json.load(f)
            self.import_from_json(data)
            logger.info(f"Token data restored from {filepath}")
        except Exception as e:
            logger.error(f"Failed to restore token data from file: {e}")
            raise

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

    def get_hierarchy_since(
        self, cutoff: datetime | None = None
    ) -> dict[str, dict[str, TokenStats]]:
        """Get provider/model hierarchy filtered by time period from the database.

        Queries the SQLite ``token_usage`` table for records ``>= cutoff``,
        aggregates by provider and model, and returns the same format as
        :meth:`get_all_providers`.

        Args:
            cutoff: Only include records at or after this timestamp.
                    ``None`` means all time (falls back to in-memory data).

        Returns:
            Same shape as :meth:`get_all_providers`:
            ``{provider_id: {model_id: TokenStats, ...}, ...}``
        """
        if cutoff is None:
            return self.get_all_providers()

        try:
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.execute(
                    """
                    SELECT provider_id, model_id,
                           SUM(input_tokens), SUM(output_tokens),
                           SUM(request_count)
                    FROM token_usage
                    WHERE recorded_at >= ?
                    GROUP BY provider_id, model_id
                    ORDER BY provider_id, model_id
                    """,
                    (cutoff,),
                )

                hierarchy: dict[str, dict[str, TokenStats]] = {}
                for provider_id, model_id, input_t, output_t, req_count in cursor:
                    stats = TokenStats()
                    stats.input_tokens = input_t or 0
                    stats.output_tokens = output_t or 0
                    stats.total_tokens = (input_t or 0) + (output_t or 0)
                    stats.request_count = req_count or 0
                    hierarchy.setdefault(provider_id, {})[model_id] = stats

                return hierarchy
        except Exception as e:
            logger.error(f"Failed to query time-filtered hierarchy: {e}")
            return {}

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

    def get_time_window_stats(self, hours_back: int = 24) -> dict[str, dict]:
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
