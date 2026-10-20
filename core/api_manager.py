"""
core/api_manager.py — handles which backend/API key MistAI talks to.
"""

import os
import re
from .config import API_MODE, PRIMARY_API, FALLBACK_API, MODEL_LABELS

# Removed from a backend error message before it is shown or logged, in case
# the backend echoes something sensitive back.
_ERROR_REDACT = re.compile(
    r"https?://\S+|\b(?:bearer\s+)?(?:mistai|sk|pk|key|tok)[-_][A-Za-z0-9_-]{6,}",
    re.I,
)
ERROR_DETAIL_CHARS = 120


def describe_api_error(response, model=""):
    """One plain sentence for a failed LLM response: the status code plus the
    body's "error"/"message" field (capped, URLs and key-like strings
    removed). Never headers, keys, URLs or the request text. The caller
    decides what to do; nothing here retries or switches models."""
    status = getattr(response, "status_code", None)
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            value = body.get("error") or body.get("message") or ""
            if isinstance(value, dict):
                value = value.get("message") or value.get("error") or ""
            detail = " ".join(_ERROR_REDACT.sub("[removed]", str(value)).split())
    except Exception:
        pass
    if len(detail) > ERROR_DETAIL_CHARS:
        detail = detail[: ERROR_DETAIL_CHARS - 3].rstrip() + "..."
    # The name the user sees in Settings (MODEL_LABELS), not the backend id.
    name = MODEL_LABELS.get(model) or (model or "the model").capitalize()
    code = f"HTTP {status}" if status else "no status"
    reason = f"{code}: {detail}" if detail else code
    if status == 429:
        return (f"{name} is rate-limited or unavailable right now ({reason}). "
                f"Pick another model in Settings or try again later.")
    return f"{name} request failed ({reason})."


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
