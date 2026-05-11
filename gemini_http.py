"""Мінімальний HTTP-клієнт Google AI Gemini (Gemini Developer API key) через v1beta REST."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

_BASE = "https://generativelanguage.googleapis.com/v1beta"


def _request_json(
    api_key: str,
    path: str,
    *,
    method: str = "GET",
    query: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    q = dict(query or {})
    q["key"] = api_key
    url = f"{_BASE}{path}?{urlencode(q)}"
    data_bytes: bytes | None = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8")
    except HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Gemini HTTP {e.code}: {err_body or e.reason}") from e
    except URLError as e:
        raise RuntimeError(f"Gemini network error: {e.reason}") from e
    if not body:
        return {}
    return json.loads(body)


def fetch_models(api_key: str, *, timeout_s: float = 60.0) -> list[dict[str, Any]]:
    """Повертає сиру відповідь API (елементи models.*) з підтримкою пагінації."""
    out: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        query: dict[str, str] = {"pageSize": "100"}
        if page_token:
            query["pageToken"] = page_token
        blob = _request_json(
            api_key, "/models", method="GET", query=query, payload=None, timeout_s=timeout_s
        )
        chunk = blob.get("models") or []
        out.extend(chunk)
        page_token = blob.get("nextPageToken")
        if not page_token:
            break
    log.info("Fetched %s Gemini models from API", len(out))
    return out


def generate_content_json(
    api_key: str,
    model_short_name: str,
    *,
    system_instruction: str,
    user_text: str,
    generation_config: dict[str, Any],
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    """
    POST .../models/{model}:generateContent
    model_short_name без префікса models/ наприклад gemini-2.5-flash
    """
    model = model_short_name.removeprefix("models/")
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": generation_config,
    }
    if system_instruction.strip():
        body["systemInstruction"] = {"role": "system", "parts": [{"text": system_instruction}]}
    path = f"/models/{model}:generateContent"
    return _request_json(api_key, path, method="POST", payload=body, timeout_s=timeout_s)
