"""Високий рівень: нормалізація входу, виклик Gemini, парсинг і злиття SEO-бандлу."""

from __future__ import annotations

import json
import logging
from typing import Any

import gemini_http
from gemini_bundle_schema import GEMINI_BUNDLE_SCHEMA
from gemini_http import fetch_models
from gemini_models import resolve_generation_model
import seo_bundle
import settings

log = logging.getLogger(__name__)


def _generation_config(use_schema: bool) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "temperature": 0.65,
        "topP": 0.95,
        "maxOutputTokens": 8192,
        "responseMimeType": "application/json",
    }
    if use_schema:
        cfg["responseSchema"] = GEMINI_BUNDLE_SCHEMA
    return cfg


def extract_candidate_text(generate_response: dict[str, Any]) -> str:
    try:
        cands = generate_response["candidates"]
        c0 = cands[0]
        if not c0.get("content"):
            raise RuntimeError(f"Gemini заблокував вміст або не повернув parts: {c0}")
        finish = c0.get("finishReason")
        if finish and finish not in ("STOP", "MAX_TOKENS"):
            log.warning("Gemini finishReason=%s keys=%s", finish, list(c0.keys()))
        parts = c0["content"]["parts"]
        text = "".join(part.get("text") or "" for part in parts)
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Неочікувана відповідь Gemini: {generate_response}") from e
    if not text.strip():
        raise RuntimeError("Gemini повернув порожній текст")
    return text


def generate_bundle_with_gemini(user_input: dict[str, Any], *, use_schema: bool = True) -> dict[str, Any]:
    """Повний цикл без YouTube-кроку: Gemini → парсинг → зливання з авторитетним input."""
    api_key = settings.require_gemini_api_key()
    ms = fetch_models(api_key)
    model_id = resolve_generation_model(ms, env_override=settings.gemini_model_override())
    inp = seo_bundle.normalize_generation_input(user_input)
    payload_text = json.dumps(inp, ensure_ascii=False)
    sys_prompt = seo_bundle.load_system_prompt()

    trials = [True, False] if use_schema else [False]
    errors: list[RuntimeError | str] = []
    blob: dict[str, Any] | None = None

    for with_schema in trials:
        label = "responseSchema+MIME-json" if with_schema else "MIME-json only"
        try:
            log.info("Gemini generateContent model=%s mode=%s", model_id, label)
            blob = gemini_http.generate_content_json(
                api_key,
                model_id,
                system_instruction=sys_prompt,
                user_text=(
                    "Ось коректний кореневий JSON USER_INPUT згідно з системним промптом: "
                    f"{payload_text}"
                ),
                generation_config=_generation_config(use_schema=with_schema),
                timeout_s=180.0,
            )
            break
        except RuntimeError as e:
            errors.append(str(e))
            log.warning(
                "Помилка Gemini (%s): %s; спробуємо наступний режим якщо є",
                label,
                e,
            )

    if blob is None:
        raise RuntimeError(
            "Gemini не зміг згенерувати відповідь. Спроби:\n" + "\n---\n".join(str(x) for x in errors)
        )

    raw_text = extract_candidate_text(blob)
    parsed = seo_bundle.parse_gemini_bundle_text(raw_text)
    merged = seo_bundle.merge_bundle_with_normalized_input(parsed, inp)

    seo_bundle.ensure_bundle_skeleton(merged)
    seo_bundle.ensure_publish_standard_blocks(merged, inp)
    for w in seo_bundle.validate_bundle_warnings(merged):
        log.warning("bundle validation: %s", w)

    merged = seo_bundle.apply_youtube_hard_limits(merged, inp, trim_description=False)
    return merged


async def generate_bundle_with_gemini_async(user_input: dict[str, Any]) -> dict[str, Any]:
    """Обгортка для python-telegram-bot (async)."""
    import asyncio

    return await asyncio.to_thread(generate_bundle_with_gemini, user_input)
