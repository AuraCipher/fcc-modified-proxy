"""
Environment variable hot-reload manager.

Watches .env file for changes and reloads settings after active requests complete.
"""

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Callable

from loguru import logger


class SettingsReloadManager:
    """Manages hot-reload of settings when .env file changes."""

    def __init__(self):
        """Initialize the reload manager."""
        self._env_paths = self._get_env_paths()
        self._file_mtimes: dict[Path, float] = {}
        self._active_requests = 0
        self._pending_reload = False
        self._reload_lock = threading.Lock()
        self._reload_callback: Callable[[], None] | None = None
        self._watcher_thread: threading.Thread | None = None
        self._stop_watcher = threading.Event()

    def _get_env_paths(self) -> list[Path]:
        """Get all .env file paths to watch."""
        paths = []
        # Add main .env if it exists
        env_file = Path(".env")
        if env_file.is_file():
            paths.append(env_file)

        # Add managed env path if it exists
        from .paths import managed_env_path

        managed = managed_env_path()
        if managed.is_file() and managed not in paths:
            paths.append(managed)

        # Add explicit FCC_ENV_FILE if configured
        if explicit := os.environ.get("FCC_ENV_FILE"):
            explicit_path = Path(explicit)
            if explicit_path.is_file() and explicit_path not in paths:
                paths.append(explicit_path)

        return paths

    def start(self, callback: Callable[[], None]) -> None:
        """Start watching for .env file changes.

        Args:
            callback: Function to call when reload is needed and safe to execute.
        """
        self._reload_callback = callback
        self._initialize_mtimes()

        # Start background watcher thread
        self._watcher_thread = threading.Thread(
            target=self._watch_loop, daemon=True, name="EnvHotReloadWatcher"
        )
        self._watcher_thread.start()
        logger.info("Environment hot-reload watcher started")

    def stop(self) -> None:
        """Stop watching for .env file changes."""
        self._stop_watcher.set()
        if self._watcher_thread:
            self._watcher_thread.join(timeout=2.0)
        logger.info("Environment hot-reload watcher stopped")

    def _initialize_mtimes(self) -> None:
        """Initialize modification times for all watched files."""
        for path in self._env_paths:
            try:
                self._file_mtimes[path] = path.stat().st_mtime
            except OSError as e:
                logger.warning(f"Could not stat {path}: {e}")

    def _watch_loop(self) -> None:
        """Background thread that watches for file changes."""
        while not self._stop_watcher.is_set():
            try:
                self._check_for_changes()
            except Exception as e:
                logger.error(f"Error in env reload watcher: {e}")

            # Check every 2 seconds
            self._stop_watcher.wait(2.0)

    def _check_for_changes(self) -> None:
        """Check if any env files have been modified."""
        for path in self._env_paths:
            try:
                if not path.is_file():
                    continue

                current_mtime = path.stat().st_mtime
                last_mtime = self._file_mtimes.get(path, 0)

                if current_mtime > last_mtime:
                    logger.info(f"Detected .env file change: {path}")
                    self._file_mtimes[path] = current_mtime
                    self._pending_reload = True
                    self._try_reload()

            except OSError as e:
                logger.warning(f"Error checking {path}: {e}")

    def _try_reload(self) -> None:
        """Try to reload settings if no requests are active."""
        if not self._pending_reload:
            return

        with self._reload_lock:
            if self._active_requests > 0:
                logger.info(
                    f"Deferring .env reload: {self._active_requests} active requests"
                )
                return

            self._pending_reload = False
            if self._reload_callback:
                try:
                    logger.info("Reloading settings from .env")
                    self._reload_callback()
                    logger.info("Settings reloaded successfully")
                except Exception as e:
                    logger.error(f"Error reloading settings: {e}")
                    self._pending_reload = True  # Retry on next check

    def register_request_start(self) -> None:
        """Call when a request starts."""
        with self._reload_lock:
            self._active_requests += 1

    def register_request_end(self) -> None:
        """Call when a request completes (success or error)."""
        with self._reload_lock:
            self._active_requests = max(0, self._active_requests - 1)
            if self._active_requests == 0 and self._pending_reload:
                self._try_reload()

    async def register_request_end_async(self) -> None:
        """Async version of register_request_end."""
        # Run in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.register_request_end)


# Global instance
_reload_manager: SettingsReloadManager | None = None


def get_reload_manager() -> SettingsReloadManager:
    """Get the global reload manager instance."""
    global _reload_manager
    if _reload_manager is None:
        _reload_manager = SettingsReloadManager()
    return _reload_manager


def start_reload_watcher(callback: Callable[[], None]) -> None:
    """Start the environment variable reload watcher.

    Args:
        callback: Function to call when it's safe to reload settings.
    """
    manager = get_reload_manager()
    manager.start(callback)


def stop_reload_watcher() -> None:
    """Stop the environment variable reload watcher."""
    manager = get_reload_manager()
    manager.stop()
