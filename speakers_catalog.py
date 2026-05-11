"""Завантаження спікерів з speakers.csv для майстра та JSON Gemini."""

from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
SPEAKERS_CSV = _ROOT / "speakers.csv"

# Як у seo_bundle.example_generation_input: social — лише осмислені URL.
_SOCIAL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Youtube", "youtube"),
    ("Telegram", "telegram"),
    ("Facebook", "facebook"),
    ("Instagram", "instagram"),
    ("TikTok", "tiktok"),
    ("Patreon", "patreon"),
    ("threads", "threads"),
    ("linkedin", "linkedin"),
    ("WebSite", "website"),
    ("PayPal", "paypal"),
    ("Mono", "mono"),
    ("AppStore", "app_store"),
    ("PlayMarket", "play_market"),
)

# Порядок і підписи для людського опису / доповнення з каталогу (не змінюйте довільно — збіг з UX).
SPEAKER_SOCIAL_KEY_ORDER: tuple[str, ...] = tuple(k for _, k in _SOCIAL_COLUMNS)
SPEAKER_SOCIAL_LABELS_UK: dict[str, str] = {
    "youtube": "YouTube",
    "telegram": "Telegram",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "patreon": "Patreon",
    "threads": "Threads",
    "linkedin": "LinkedIn",
    "website": "Сайт",
    "paypal": "PayPal",
    "mono": "Monobank",
    "app_store": "App Store",
    "play_market": "Google Play",
}


def _normalize_url(cell: str) -> str | None:
    raw = (cell or "").strip()
    if not raw:
        return None
    low = raw.lower()
    if low in ("email", "n/a", "-", "—"):
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    # рядки на кшталт youtube.com/... або www....
    if "." in raw or "/" in raw:
        return "https://" + raw.lstrip("/").removeprefix("http://").removeprefix("https://")
    return None


def _slug_header(label: str) -> str:
    """Ключ для зіставлення заголовків CSV (Google Sheets може дати пробіли / регістр)."""
    s = (label or "").strip().lstrip("\ufeff").lower()
    return "".join(ch for ch in s if ch.isalnum())


def _row_by_slug(row: dict[str, str]) -> dict[str, str]:
    """Один рядок DictReader → dict[slug_header] = value (останній виграє при дублях slug)."""
    out: dict[str, str] = {}
    for k, v in row.items():
        sk = _slug_header(k or "")
        if sk:
            out[sk] = v or ""
    return out


_HEADER_SLUG_TO_COL: dict[str, str] = {
    **{_slug_header(col): col for col, _ in _SOCIAL_COLUMNS},
    # Синоніми заголовків після експорту Google Таблиць
    _slug_header("Website"): "WebSite",
    _slug_header("Google Play"): "PlayMarket",
    _slug_header("Apple App Store"): "AppStore",
    _slug_header("Play Store"): "PlayMarket",
    _slug_header("Playstore"): "PlayMarket",
}


def _entry_from_cells(cells: dict[str, str], index: int) -> dict[str, object]:
    name = (cells.get("speaker") or "").strip() or f"Спікер #{index + 1}"
    social: dict[str, str] = {}
    for col, key in _SOCIAL_COLUMNS:
        url = _normalize_url(cells.get(col, "") or "")
        if url:
            social[key] = url
    return {"id": index, "display_name": name, "social": social}


def _canonical_row_cells(row: dict[str, str]) -> dict[str, str]:
    slugged = _row_by_slug(row)
    flat: dict[str, str] = {}
    # speaker: типові варіанти заголовка в CSV / Google Таблицях
    for s in ("speaker", "name", "displayname", "spiker"):
        if s in slugged and (slugged[s] or "").strip():
            flat["speaker"] = slugged[s]
            break
    else:
        flat["speaker"] = ""

    for slug_src, canon_col in _HEADER_SLUG_TO_COL.items():
        if canon_col == "speaker":
            continue
        if slug_src not in slugged:
            continue
        cur = flat.get(canon_col, "").strip()
        cell = slugged.get(slug_src, "").strip()
        if cell and not cur:
            flat[canon_col] = cell

    return flat


@lru_cache
def list_speakers() -> tuple[dict[str, object], ...]:
    """Незмінний список записів з display_name та social (для Gemini)."""
    if not SPEAKERS_CSV.is_file():
        return ()
    with SPEAKERS_CSV.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    out: list[dict[str, object]] = []
    for row in rows:
        cells = _canonical_row_cells(row or {})
        if not (cells.get("speaker") or "").strip():
            continue
        out.append(_entry_from_cells(cells, len(out)))
    return tuple(out)


def gemini_speaker_dict(entry: dict[str, object]) -> dict[str, object]:
    """Один елемент масиву speakers у вхідному JSON для моделі."""
    return {
        "display_name": entry["display_name"],
        "social": dict(entry["social"]) if entry.get("social") else {},
    }
