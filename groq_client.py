"""
groq_client.py — one place for every Groq API call in EduHire
==============================================================
Why this exists
---------------
Groq retires / moves models often (e.g. llama-3.1-8b-instant and
llama-3.3-70b-versatile moved to Enterprise in 2026, mixtral and gemma2 were removed).
Hard-coding model names in 5 different files made EduHire break every time.

This module:
  * asks Groq which models YOUR key can use  (GET /models, cached 30 min)
  * picks the best available one automatically
  * if a model fails with "does not exist / no access", blacklists it and retries with the next
  * retries timeouts / 429 / 5xx with back-off
  * handles reasoning models (openai/gpt-oss-*) so the answer is never empty

Public API
----------
list_models(api_key)                -> list[str]
pick_model(api_key, preferred)      -> str
chat(messages, api_key, ...)        -> (text | None, error | None)
error_message(error)                -> user-friendly string
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from typing import Optional

BASE_URL = "https://api.groq.com/openai/v1"
CHAT_URL = f"{BASE_URL}/chat/completions"
MODELS_URL = f"{BASE_URL}/models"

# Best first. Anything not listed here but chat-capable is still used as a last resort.
PREFERRED_MODELS = [
    "openai/gpt-oss-120b",
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-20b",
    "llama-3.1-8b-instant",
    "qwen/qwen3-32b",
]
DEFAULT_MODEL = "openai/gpt-oss-120b"
FAST_MODEL = "openai/gpt-oss-20b"

_NON_CHAT_HINTS = ("whisper", "tts", "orpheus", "guard", "embed", "compound", "safeguard", "playai")

_models_cache: dict[str, tuple[float, list[str]]] = {}
_bad_models: dict[str, set[str]] = {}
_CACHE_TTL = 30 * 60


def _kid(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()[:12]


def _headers(api_key: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }


def _is_chat_model(model_id: str) -> bool:
    m = model_id.lower()
    return not any(h in m for h in _NON_CHAT_HINTS)


def _is_reasoning(model_id: str) -> bool:
    return model_id.startswith("openai/gpt-oss")


# ── Model discovery ──────────────────────────────────────────────────────────

def list_models(api_key: str, force: bool = False) -> list[str]:
    """Chat-capable model ids available to this key. [] if Groq can't be reached."""
    if not api_key:
        return []
    kid = _kid(api_key)
    now = time.time()
    hit = _models_cache.get(kid)
    if hit and not force and now - hit[0] < _CACHE_TTL:
        return hit[1]
    try:
        req = urllib.request.Request(MODELS_URL, headers=_headers(api_key), method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ids = [m["id"] for m in data.get("data", []) if _is_chat_model(m.get("id", ""))]
        _models_cache[kid] = (now, ids)
        return ids
    except Exception:
        return hit[1] if hit else []


def pick_model(api_key: str, preferred: Optional[str] = None, exclude: Optional[set] = None) -> str:
    exclude = exclude or set()
    avail = [m for m in list_models(api_key) if m not in exclude]
    if not avail:                                   # couldn't list -> best guess
        if preferred and preferred not in exclude:
            return preferred
        for m in PREFERRED_MODELS:
            if m not in exclude:
                return m
        return DEFAULT_MODEL
    if preferred and preferred in avail:
        return preferred
    for m in PREFERRED_MODELS:
        if m in avail:
            return m
    return avail[0]


def friendly_label(model_id: str) -> str:
    labels = {
        "openai/gpt-oss-120b": "⭐ GPT-OSS 120B (Best quality)",
        "openai/gpt-oss-20b": "⚡ GPT-OSS 20B (Fastest)",
        "llama-3.3-70b-versatile": "🦙 Llama 3.3 70B",
        "llama-3.1-8b-instant": "🦙 Llama 3.1 8B",
        "qwen/qwen3-32b": "🧠 Qwen3 32B",
    }
    return labels.get(model_id, model_id)


# ── Chat ─────────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    return text.strip()


def chat(
    messages: list[dict],
    api_key: str,
    model: Optional[str] = None,
    max_tokens: int = 600,
    temperature: float = 0.4,
    timeout: int = 45,
    max_retries: int = 4,
) -> tuple[Optional[str], Optional[str]]:
    """
    Returns (text, None) on success or (None, error_code) on failure.
    error_code is one of: invalid_key, rate_limit, timeout, empty, http_<code>: <msg>
    """
    if not api_key:
        return None, "invalid_key"
    bad = _bad_models.setdefault(_kid(api_key), set())
    last_err = "failed"
    drop_reasoning_param = False

    for attempt in range(max_retries + 2):
        mdl = pick_model(api_key, model, exclude=bad)
        payload = {
            "model": mdl,
            "messages": messages,
            "max_tokens": max_tokens + (1500 if _is_reasoning(mdl) else 0),
            "temperature": temperature,
        }
        if _is_reasoning(mdl) and not drop_reasoning_param:
            payload["reasoning_effort"] = "low"
        try:
            req = urllib.request.Request(
                CHAT_URL, data=json.dumps(payload).encode("utf-8"),
                headers=_headers(api_key), method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = _clean(data["choices"][0]["message"].get("content") or "")
            if text:
                return text, None
            last_err = "empty"
            continue
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            try:
                msg = json.loads(body).get("error", {}).get("message", body[:200])
            except Exception:
                msg = body[:200]
            low = msg.lower()
            if e.code == 401 or "invalid_api_key" in low:
                return None, "invalid_key"
            if e.code == 429:
                last_err = "rate_limit"
                time.sleep(min(2 ** attempt * 2, 20))
                continue
            if e.code in (400, 403, 404) and any(
                s in low for s in ("does not exist", "not have access", "decommission",
                                   "model_not_found", "no longer supported", "deprecated")
            ):
                bad.add(mdl)                  # retired / no access -> never use it again for this key
                _models_cache.pop(_kid(api_key), None)
                last_err = f"http_{e.code}: {msg[:150]}"
                continue
            if e.code == 400 and "reasoning" in low and not drop_reasoning_param:
                drop_reasoning_param = True
                continue
            if e.code >= 500:
                last_err = f"http_{e.code}: {msg[:150]}"
                time.sleep(1.5)
                continue
            return None, f"http_{e.code}: {msg[:150]}"
        except Exception as e:                # timeout / DNS / reset
            last_err = "timeout" if "timed out" in str(e).lower() else f"network: {str(e)[:100]}"
            time.sleep(1.5)
            continue
    return None, last_err


def error_message(err: Optional[str]) -> str:
    err = err or ""
    if err == "invalid_key":
        return "❌ Invalid Groq API key. Get a free key at https://console.groq.com"
    if err == "rate_limit":
        return "⏳ Groq rate limit hit. Wait a minute and try again."
    if err.startswith("http_"):
        return f"⚠️ Groq API error: {err.split(': ', 1)[-1]}"
    return ""        # timeout / network / empty -> caller falls back to rule-based answer
