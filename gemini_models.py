"""Вибір найкращої доступної Gemini-моделі (пріоритет 3.x і вище для generateContent)."""

from __future__ import annotations

import re
from typing import Any

GEMINI_MINOR_VERSION_PATTERN = re.compile(r"gemini-(\d+)(?:[\.\-])(\d+)", re.I)
GEMINI_MAJOR_ONLY_PATTERN = re.compile(r"\bgemini-(\d+)\b", re.I)

# Стабільний аліас дешевших/швидких задач тексту — якщо є у відповіді API, він обирається
# першим перед евристикою «найновіший 3.x-pro» (див. resolve_generation_model).
PROJECT_DEFAULT_LITE_LATEST_ALIAS = "gemini-flash-lite-latest"


def generation_ready(m: dict[str, Any]) -> bool:
    methods = set(m.get("supportedGenerationMethods") or [])
    return "generateContent" in methods


def extract_version(name: str) -> tuple[int, int] | None:
    """
    Повертає (major, minor) з рядка на кшталт models/gemini-3.0-pro або gemini-2.5-flash.
    Якщо не вдалося — None.
    """
    base = name.split("/")[-1]
    mm = GEMINI_MINOR_VERSION_PATTERN.search(base)
    if mm:
        return int(mm.group(1)), int(mm.group(2))
    mo = GEMINI_MAJOR_ONLY_PATTERN.search(base)
    if mo:
        return int(mo.group(1)), 0
    return None


def _short_model_id(full_name: str) -> str:
    return full_name.split("/")[-1]


def _auto_pick_skip(short_id: str) -> bool:
    """
    Виключаємо спеціалізовані прев'ю (TTS, image, deep research тощо) з автопідбору.
    Для SEO+JSON лишаємо «звичайні» pro/flash. Якщо після фільтра нікого не лишилось — fallback нижче.
    """
    n = short_id.lower()
    if "customtools" in n:
        return True
    if "deep-research" in n:
        return True
    if "robotics" in n:
        return True
    if "computer-use" in n:
        return True
    if "nano-banana" in n:
        return True
    if n.startswith("lyria-"):
        return True
    if "image" in n:
        return True
    if "tts" in n:
        return True
    return False


def _score(name: str) -> tuple:
    """
    Більше — краще. Упорядковуємо: major>=3, далі major.minor, далі pro > flash > інше.
    Останні поля: коротший id кращий при рівних метриках (уникнення «-customtools» через фільтр).
    """
    ver = extract_version(name)
    major, minor = (ver if ver else (0, 0))
    base = name.lower()
    short = _short_model_id(name)
    family = 0
    if "gemini" in base and "embedding" not in base and "vision" not in base:
        family = 1
    has_3 = major >= 3
    kind = 0
    if "pro" in base:
        kind = 3
    elif "flash" in base or "lite" in base:
        kind = 2
    elif "gemma" in base:
        kind = 0
    return (family, has_3, major, minor, kind, -len(short), short)


def pick_generate_content_model(
    models: list[dict[str, Any]], *, env_override: str | None = None
) -> str:
    """
    Повертає коротку назву моделі для path (без models/), напр. gemini-3.0-pro.
    """
    if env_override and env_override.strip():
        return env_override.strip().removeprefix("models/")

    candidates: list[dict[str, Any]] = [m for m in models if generation_ready(m)]
    names = [str(m.get("name", "")) for m in candidates if m.get("name")]
    gemini = [n for n in names if "gemini" in n.lower() and "embedding" not in n.lower()]
    if not gemini:
        raise RuntimeError(
            "Не знайдено жодної моделі Gemini з підтримкою generateContent. "
            "Перевірте ключ API та квоту."
        )

    filtered = [n for n in gemini if not _auto_pick_skip(_short_model_id(n))]
    pick_from = filtered if filtered else gemini

    pick_from.sort(key=_score, reverse=True)
    best = pick_from[0]
    return best.split("/")[-1]


def resolve_generation_model(
    models: list[dict[str, Any]],
    *,
    env_override: str | None = None,
    preferred_flash_lite_latest: str = PROJECT_DEFAULT_LITE_LATEST_ALIAS,
) -> str:
    """
    Політика вибору для бота:
    1) Якщо заданий GEMINI_MODEL у .env — лише він.
    2) Якщо у списку API є gemini-flash-lite-latest — берем її як проєктний дефолт (швидко/дешево).
    3) Інакше — евристика pick_generate_content_model (Gemini 3.x pro тощо).
    """
    if env_override and env_override.strip():
        return env_override.strip().removeprefix("models/")
    shorts = {_short_model_id(str(m["name"])) for m in models if m.get("name")}
    if preferred_flash_lite_latest in shorts:
        return preferred_flash_lite_latest
    return pick_generate_content_model(models, env_override=None)


def format_model_table(models: list[dict[str, Any]]) -> str:
    """Текстовий звіт для скрипта list_gemini_models."""
    rows: list[str] = []
    for m in sorted(models, key=lambda x: str(x.get("name", ""))):
        name = str(m.get("name", ""))
        methods = ",".join(m.get("supportedGenerationMethods") or [])
        ver = extract_version(name)
        ver_s = f"{ver[0]}.{ver[1]}" if ver else "?"
        rows.append(f"{name}\tgenerateContent={('generateContent' in methods)}\tver~{ver_s}")
    return "\n".join(rows)
