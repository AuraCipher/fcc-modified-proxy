"""Tests for numbered NVIDIA NIM API key loading."""

from config.settings import Settings, numbered_nvidia_nim_api_keys


def test_numbered_nim_api_keys_from_env(monkeypatch):
    monkeypatch.setenv("NVIDIA_NIM_API_KEY1", "first")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY2", "second")
    monkeypatch.delenv("NVIDIA_NIM_API_KEY3", raising=False)
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)

    keys = numbered_nvidia_nim_api_keys(Settings.model_config)
    assert keys == ("first", "second")


def test_settings_prefers_numbered_keys_over_legacy(monkeypatch):
    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": None}
    )
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "legacy")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY1", "one")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY2", "two")

    settings = Settings()
    assert settings.nvidia_nim_api_keys == ("one", "two")
    assert settings.nvidia_nim_api_key == "legacy"


def test_settings_falls_back_to_legacy_key(monkeypatch):
    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": None}
    )
    monkeypatch.delenv("NVIDIA_NIM_API_KEY1", raising=False)
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "legacy-only")

    settings = Settings()
    assert settings.nvidia_nim_api_keys == ("legacy-only",)
    assert settings.nvidia_nim_api_key == "legacy-only"
