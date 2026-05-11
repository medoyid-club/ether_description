"""Створення папки `output/` під кожну сесію майстра й запис JSON / файлів локально."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

import settings


def _safe_filename_part(s: str, max_len: int = 80) -> str:
    t = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (s.strip() or "untitled"))
    return t[:max_len] if t else "untitled"


def new_session_directory(
    *,
    chat_id: int | None,
    user_id: int | None,
    title_hint: str,
) -> Path:
    """
    output/<UTC date>_<chat>_<user>_<slug>-<minute-second>/
    """
    root = settings.output_sessions_root()
    root.mkdir(parents=True, exist_ok=True)
    utc = datetime.now(timezone.utc)
    slug = _safe_filename_part(title_hint.replace("\n", " "), max_len=60)
    name = (
        f"{utc.strftime('%Y-%m-%d')}_chat{chat_id or 0}_u{user_id or 0}_"
        f"{slug}-{utc.strftime('%H%M%S')}_{uuid.uuid4().hex[:8]}"
    )
    d = root / name
    d.mkdir(parents=True, exist_ok=False)
    return d


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
