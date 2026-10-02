"""Configuration-level API route safety checks."""

from core.api_manager import APIKeyManager


def test_no_key_backend_mode_uses_public_route_without_a_guaranteed_401_fallback(
    monkeypatch,
):
    monkeypatch.delenv("MISTAI_API_KEY", raising=False)
    monkeypatch.setenv("MISTAI_USE_BACKEND", "true")

    manager = APIKeyManager()

    assert manager.get_api_url().endswith("/api/chat")
    assert manager.get_fallback_url() is None


def test_environment_can_explicitly_select_direct_mode(monkeypatch):
    monkeypatch.setenv("MISTAI_API_KEY", "mistai_test")
    monkeypatch.setenv("MISTAI_USE_BACKEND", "false")

    manager = APIKeyManager()

    assert manager.get_api_url() == manager._primary_api
    assert manager.get_fallback_url() == manager._fallback_api
