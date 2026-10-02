"""
core/api_manager.py — handles which backend/API key MistAI talks to.
"""

import os
from .config import API_MODE, PRIMARY_API, FALLBACK_API


class APIKeyManager:
    def __init__(self):
        self._api_key = os.environ.get("MISTAI_API_KEY", "")
        configured_backend = os.environ.get(
            "MISTAI_USE_BACKEND", "true" if API_MODE == "backend" else "false"
        )
        self._use_backend = configured_backend.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._primary_api = PRIMARY_API
        self._fallback_api = FALLBACK_API

        if self._api_key:
            print("[APIKey] API key loaded from environment.")
        else:
            print("[APIKey] No API key found in environment. Backend fallback enabled.")

    def set_api_key(self, key):
        self._api_key = key
        print("[APIKey] API key updated.")

    def use_backend(self, enabled):
        self._use_backend = enabled
        print(f"[APIKey] Backend mode: {enabled}")

    def get_headers(self):
        if self._api_key:
            return {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
        return {"Content-Type": "application/json"}

    def get_api_url(self):
        if self._use_backend:
            return "https://mist-ai.fly.dev/api/chat"
        return self._primary_api

    def get_fallback_url(self):
        # The public backend endpoint is intentionally usable without a
        # bearer token.  Its /api/v1 fallback is not: trying it with no key
        # only turns a useful public-route error into a misleading 401.
        # Return no fallback in that configuration so callers can report the
        # original failure honestly.
        if self._use_backend and not self._api_key:
            return None
        return self._fallback_api

    @property
    def has_key(self):
        return self._api_key != ""
