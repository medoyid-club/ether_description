"""JSON Schema під structured output Gemini (Gemini Developer API REST, type у ВЕЛИКІЙ формі)."""

from __future__ import annotations

from typing import Any

GEMINI_BUNDLE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "bundle_version": {"type": "STRING"},
        "telegram": {
            "type": "OBJECT",
            "properties": {
                "full_package_plain": {"type": "STRING"},
            },
            "required": ["full_package_plain"],
        },
        "youtube": {
            "type": "OBJECT",
            "properties": {
                "title": {"type": "STRING"},
                "description": {"type": "STRING"},
                "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
                "scheduled_start_time_rfc3339": {"type": "STRING"},
                "privacy_status": {"type": "STRING"},
                "category_id": {"type": "STRING"},
                "default_language": {"type": "STRING"},
                "playlist_ids": {"type": "ARRAY", "items": {"type": "STRING"}},
                "self_declared_made_for_kids": {"type": "BOOLEAN"},
                "custom_notes_for_operator": {"type": "STRING"},
            },
            "required": [
                "title",
                "description",
                "tags",
                "scheduled_start_time_rfc3339",
                "privacy_status",
                "category_id",
                "default_language",
                "playlist_ids",
                "self_declared_made_for_kids",
                "custom_notes_for_operator",
            ],
        },
        "seo": {
            "type": "OBJECT",
            "properties": {
                "hashtags_line": {"type": "STRING"},
                "keywords_line": {"type": "STRING"},
                "highlight_bullets": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
            "required": ["hashtags_line", "keywords_line", "highlight_bullets"],
        },
    },
    "required": ["bundle_version", "telegram", "youtube", "seo"],
}
