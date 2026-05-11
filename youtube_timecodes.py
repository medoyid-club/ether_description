"""
Тайм-коди для опису відео: транскрипт (youtube-transcript-api) + Gemini → вставка перед блоком #тегів + оновлення через YouTube Data API.

Окремо від SEO-майстра; потрібні ті самі OAuth `token.json`, що й для плейлистів / live.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import gemini_generate
import gemini_http
import settings
from gemini_models import resolve_generation_model

log = logging.getLogger(__name__)

YOUTUBE_DESCRIPTION_MAX = 5000

_VIDEO_ID_FROM_URL = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/live/|youtube\.com/shorts/)"
    r"([A-Za-z0-9_-]{11})"
)


class TimecodesPipelineError(RuntimeError):
    """Помилка кроку пайплайну (не ваш канал, немає субтитрів тощо)."""


def extract_video_id(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", raw):
        return raw
    m = _VIDEO_ID_FROM_URL.search(raw)
    return m.group(1) if m else None


def _normalize_transcript_lang(code: str) -> str:
    c = (code or "").strip().lower()
    if c.startswith("ua"):
        return "uk"
    return c


def _detect_output_language(transcript_sample: str) -> str:
    """Код мови для тексту тайм-кодів (uk|ru|en|…)."""
    s = transcript_sample[:4000].lower()
    ukr = set("ґєії")
    if any(ch in ukr for ch in s):
        return "uk"
    if any("\u0400" <= ch <= "\u04ff" for ch in s):
        return "ru"
    return "en"


def _language_instruction(code: str) -> str:
    m = {
        "uk": "Ukrainian (українською)",
        "ru": "Russian (по-русски)",
        "en": "English",
    }
    return m.get(code, "the same language as the transcript (match the subtitles language exactly)")


def _fetched_to_entries(fetched: Any) -> list[dict[str, Any]]:
    """FetchedTranscript (або сумісний ітерабельний набір snippets) → list[dict]."""
    out: list[dict[str, Any]] = []
    for snippet in fetched:
        if hasattr(snippet, "text"):
            out.append(
                {
                    "text": str(getattr(snippet, "text", "") or ""),
                    "start": float(getattr(snippet, "start", 0) or 0),
                    "duration": float(getattr(snippet, "duration", 0) or 0),
                }
            )
        elif isinstance(snippet, dict):
            out.append(snippet)
    return out


def fetch_transcript_entries(video_id: str) -> tuple[list[dict[str, Any]], str]:
    """Повертає список сегментів {text, start, duration} та код мови субтитрів."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as exc:
        raise TimecodesPipelineError(
            "Не встановлено пакет youtube-transcript-api. Виконайте: pip install youtube-transcript-api"
        ) from exc

    max_retries = 3
    retry_delay = 2.0
    outer_err: Exception | None = None

    for attempt in range(max_retries):
        last_err: Exception | None = None
        try:
            # API v1.2+: екземпляр + list() замість класових list_transcripts / get_transcript
            api = YouTubeTranscriptApi()
            transcript_list = api.list(video_id)
            transcripts = list(transcript_list)
            available = [t.language_code for t in transcripts]
            log.info("Transcript langs for %s: %s", video_id, available)

            priority = ["uk", "ru", "en"]
            for lang in priority:
                if lang not in available:
                    continue
                try:
                    tr = transcript_list.find_transcript([lang])
                    fetched = tr.fetch()
                    return _fetched_to_entries(fetched), tr.language_code
                except Exception as e:
                    log.warning("transcript fetch %s lang=%s: %s", video_id, lang, e)
                    last_err = e
                    continue

            for tr in transcripts:
                if tr.language_code in priority:
                    continue
                try:
                    fetched = tr.fetch()
                    return _fetched_to_entries(fetched), tr.language_code
                except Exception as e:
                    last_err = e
                    continue

            raise TimecodesPipelineError(
                f"Немає доступного транскрипту для відео {video_id}. "
                "Перевірте наявність субтитрів (авто або ручних) на YouTube."
            ) from last_err

        except TimecodesPipelineError:
            raise
        except Exception as e:
            outer_err = e
            if attempt == max_retries - 1:
                break
            log.warning("transcript attempt %s failed: %s", attempt + 1, e)
            time.sleep(retry_delay)
            retry_delay *= 2

    raise TimecodesPipelineError(
        f"Не вдалося завантажити транскрипт для {video_id}: {outer_err}"
    ) from outer_err


def _format_time_hms(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def format_transcript_with_timestamps(
    entries: list[dict[str, Any]], *, video_duration_sec: float
) -> str:
    """
    Текст для промпта: рядки [H:MM:SS] текст.
    Для довгих епізодів робимо рівномірну вибірку по ВСЬОМУ таймлайну, інакше обрізання по
    ліміту символів залишало б модель лише з початком відео (типово ~1 год без кінця).
    """
    if video_duration_sec > 7200:
        sampled = entries[::3]
    elif video_duration_sec > 3600:
        sampled = entries[::2]
    else:
        sampled = entries

    rows: list[tuple[float, str]] = []
    for entry in sampled:
        if not isinstance(entry, dict):
            continue
        start = float(entry.get("start", 0))
        text = str(entry.get("text", "")).replace("\n", " ").strip()
        if not text:
            continue
        rows.append((start, f"[{_format_time_hms(start)}] {text}"))

    if not rows:
        return ""

    duration_min = max(1, int(video_duration_sec // 60))
    # Великий контекст Flash; запас під system + заголовок відео
    max_chars = 650_000

    full_body = "\n".join(ln for _, ln in rows)
    header_full = (
        f"[META] Тривалість відео ~{duration_min} хв ({int(video_duration_sec)} с). "
        "Транскрипт нижче — суцільний; тайм-коди мають покривати весь епізод до фіналу.\n\n"
    )
    if len(header_full) + len(full_body) <= max_chars:
        log.info(
            "Транскрипт для Gemini: повний дамп після stride, рядків=%s символів=%s",
            len(rows),
            len(full_body),
        )
        return header_full + full_body

    n = len(rows)
    avg_len = max(45.0, len(full_body) / n)
    k_target = max(280, min(n, int((max_chars - 500) / avg_len)))

    def build_indices(target_k: int) -> list[int]:
        tk = max(2, min(target_k, n))
        idx_set = {
            min(n - 1, int(round(i * (n - 1) / max(tk - 1, 1)))) for i in range(tk)
        }
        idx_set.add(0)
        idx_set.add(n - 1)
        return sorted(idx_set)

    k_cur = min(k_target, n)
    chosen: list[int] = []
    body = ""
    while k_cur >= 120:
        chosen = build_indices(k_cur)
        body = "\n".join(rows[i][1] for i in chosen)
        hdr = (
            f"[META] Тривалість відео ~{duration_min} хв ({int(video_duration_sec)} с). "
            f"Транскрипт — рівномірна вибірка {len(chosen)} з {n} фрагментів по всій тривалості "
            f"(останній уривок біля {_format_time_hms(rows[chosen[-1]][0])}). "
            "Тайм-коди мають доходити до кінця всієї заявленої тривалості, включно з фінальними хвилинами.\n\n"
        )
        if len(hdr) + len(body) <= max_chars:
            log.info(
                "Транскрипт для Gemini: вибірка %s/%s рядків, %s символів (~%s хв)",
                len(chosen),
                n,
                len(hdr) + len(body),
                duration_min,
            )
            return hdr + body
        k_cur = int(k_cur * 0.82)

    # Останній резерв: грубий крок
    step = max(1, n // 400)
    sparse = list(range(0, n, step))
    if sparse[-1] != n - 1:
        sparse.append(n - 1)
    body = "\n".join(rows[i][1] for i in sparse)
    hdr = (
        f"[META] Тривалість ~{duration_min} хв. Дуже стисла вибірка {len(sparse)} рядків по всьому епізоду; "
        f"останній фрагмент ~{_format_time_hms(rows[sparse[-1]][0])}. Тайм-коди — до кінця всієї тривалості.\n\n"
    )
    log.warning(
        "Транскрипт для Gemini: аварійна стисла вибірка %s рядків, %s символів",
        len(sparse),
        len(hdr) + len(body),
    )
    return hdr + body


def _duration_sec_from_entries(entries: list[dict[str, Any]]) -> float:
    last = entries[-1] if entries else {}
    if isinstance(last, dict):
        return float(last.get("start", 0)) + float(last.get("duration", 0))
    return 0.0


def _gemini_max_timecodes(duration_minutes: int) -> int:
    if duration_minutes <= 30:
        return 15
    if duration_minutes <= 60:
        return 22
    if duration_minutes <= 120:
        return 32
    return 45


def _gemini_timecode_count_hint(duration_minutes: int) -> str:
    if duration_minutes <= 30:
        return "8–12"
    if duration_minutes <= 60:
        return "12–18"
    if duration_minutes <= 120:
        return "18–28"
    return "25–40"


def generate_timecode_lines_gemini(
    *,
    video_title: str,
    formatted_transcript: str,
    transcript_lang_code: str,
    duration_minutes: int,
) -> list[str]:
    api_key = settings.require_gemini_api_key()
    models = gemini_http.fetch_models(api_key)
    model_id = resolve_generation_model(models, env_override=settings.gemini_model_override())

    out_lang = _normalize_transcript_lang(transcript_lang_code)
    lang_phrase = _language_instruction(out_lang)
    rng = _gemini_timecode_count_hint(duration_minutes)
    max_lines = _gemini_max_timecodes(duration_minutes)

    system = (
        "You output ONLY plain-text YouTube chapter lines for video descriptions. "
        "No preamble, no markdown, no numbering, no bullets. "
        "Each line MUST start with a timestamp followed by ONE space then a short chapter title "
        "(3–7 words).\n"
        "Timestamp format: M:SS or MM:SS if under 1 hour; HH:MM:SS if the video is 1 hour or longer.\n"
        "THE FIRST LINE MUST START WITH 0:00 (chapter title for the very beginning).\n"
        "The transcript in the user message may be downsampled for length but is spaced across the FULL timeline "
        "(first and last subtitles are included). Do NOT stop chapters where the excerpt ends — use approximate "
        f"timing for missing tail using the stated duration (~{duration_minutes} minutes) and title/topic continuity.\n"
        "Cover the ENTIRE stated duration through the closing minutes; last chapter timestamps must fall "
        "in the final portion of the video (not mid-way).\n"
        f"Estimated number of chapters: about {rng} (stay at or below {max_lines} lines).\n"
        f"Chapter titles MUST be written in {lang_phrase}.\n"
        "Do not invent topics that are unsupported by the transcript."
    )

    user = (
        f'Video title (context): "{video_title}"\n'
        f"Stated approximate duration (use for lining up final chapters): {duration_minutes} minutes.\n"
        "TRANSCRIPT WITH TIMESTAMPS (each line begins with [time:]; "
        "[META] explains whether the excerpt is full or uniformly sampled):\n\n"
        f"{formatted_transcript}"
    )

    blob = gemini_http.generate_content_json(
        api_key,
        model_id,
        system_instruction=system,
        user_text=user,
        generation_config={
            "temperature": 0.2,
            "topP": 0.9,
            "maxOutputTokens": 8192,
            "responseMimeType": "text/plain",
        },
        timeout_s=300.0,
    )
    raw = gemini_generate.extract_candidate_text(blob)
    lines_out: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        low = s.lower()
        if any(p in low for p in ("video start", "video end", "start of video", "end of video")):
            continue
        lines_out.append(s)

    if len(lines_out) > max_lines:
        step = len(lines_out) / max_lines
        idxs = sorted({min(len(lines_out) - 1, int(i * step)) for i in range(max_lines)})
        lines_out = [lines_out[i] for i in idxs]

    return lines_out


def insert_timecodes_before_hashtag_block(
    description: str,
    block: str,
    *,
    toc_header: str = "⏱ Тайм-коди:",
) -> str:
    """Вставляє блок тайм-кодів одразу перед першим рядком, що починається з # (гештеги)."""
    desc = description or ""
    block = block.strip()
    if not block:
        return desc

    lines = desc.splitlines()
    idx = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith("＃"):
            idx = i
            break

    toc = toc_header.strip() if toc_header else "⏱ Тайм-коди:"
    piece = toc + "\n" + block

    if idx is None:
        sep = "\n\n" if desc.rstrip() else ""
        return (desc.rstrip() + sep + piece).strip()

    head = "\n".join(lines[:idx]).rstrip()
    tail = "\n".join(lines[idx:])
    sep_h = "\n\n" if head else ""
    sep_t = "\n\n" if tail else ""
    return (head + sep_h + piece + sep_t + tail).strip()


def get_my_channel_id(service) -> str:
    resp = service.channels().list(part="id", mine=True).execute()
    items = resp.get("items") or []
    if not items:
        raise TimecodesPipelineError(
            "YouTube API: не вдалося визначити ваш channelId (channels.list mine=true порожній)."
        )
    return str(items[0]["id"])


def fetch_owned_video_snippet(service, video_id: str, my_channel_id: str) -> dict[str, Any]:
    lst = (
        service.videos()
        .list(part="snippet,contentDetails", id=video_id, maxResults=1)
        .execute()
    )
    items = lst.get("items") or []
    if not items:
        raise TimecodesPipelineError(f"Відео {video_id} не знайдено або недоступне.")
    item = items[0]
    ch = str((item.get("snippet") or {}).get("channelId") or "")
    if ch != my_channel_id:
        raise TimecodesPipelineError(
            "Це відео не з вашого каналу (інший channelId). Надішліть посилання саме на свій епізод."
        )
    return item


def merge_description_with_timecodes(
    current_description: str,
    timecode_lines: list[str],
    *,
    toc_lang_code: str = "uk",
) -> str:
    block = "\n".join(timecode_lines)
    header = (
        "⏱ Chapters:"
        if str(toc_lang_code).lower().startswith("en")
        else "⏱ Тайм-коди:"
    )
    merged = insert_timecodes_before_hashtag_block(
        current_description, block, toc_header=header
    )
    if len(merged) > YOUTUBE_DESCRIPTION_MAX:
        raise TimecodesPipelineError(
            f"Опис після вставки тайм-кодів перевищує {YOUTUBE_DESCRIPTION_MAX} символів. "
            "Скоротіть опис вручну в Studio або зменшіть кількість глав."
        )
    return merged


def update_video_description(service, *, video_id: str, new_description: str) -> None:
    listed = (
        service.videos()
        .list(part="snippet", id=video_id, maxResults=1)
        .execute()
    )
    items = listed.get("items") or []
    if not items:
        raise TimecodesPipelineError("videos.list не повернув відео перед оновленням.")
    sn = dict(items[0]["snippet"])
    sn["description"] = new_description
    service.videos().update(part="snippet", body={"id": video_id, "snippet": sn}).execute()


def run_timecodes_preview(url_or_id: str) -> dict[str, Any]:
    """
    Повне синхронне виконання: перевірка каналу, транскрипт, Gemini, злиття опису.

    Повертає dict із ключами video_id, title, channel_id, transcript_lang,
    old_description, new_description, timecode_lines (list[str]).
    """
    import youtube_playlists

    vid = extract_video_id(url_or_id)
    if not vid:
        raise TimecodesPipelineError(
            "Не вдалося розпізнати id відео. Надішліть посилання youtube.com або youtu.be, або рядок з 11 символів."
        )

    creds = youtube_playlists.load_credentials()
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    my_ch = get_my_channel_id(service)
    item = fetch_owned_video_snippet(service, vid, my_ch)

    snippet = item.get("snippet") or {}
    title = str(snippet.get("title") or "").strip()
    old_desc = str(snippet.get("description") or "")

    entries, tlang = fetch_transcript_entries(vid)
    duration_sec = _duration_sec_from_entries(entries)
    duration_min = max(1, int(duration_sec // 60) or 1)
    transcript_fmt = format_transcript_with_timestamps(entries, video_duration_sec=duration_sec)

    sample_text = " ".join(
        str(e.get("text", "")) for e in entries[: min(80, len(entries))]
    )
    preview_lang = _detect_output_language(sample_text)

    tc_lines = generate_timecode_lines_gemini(
        video_title=title,
        formatted_transcript=transcript_fmt,
        transcript_lang_code=tlang or preview_lang,
        duration_minutes=duration_min,
    )
    if not tc_lines:
        raise TimecodesPipelineError("Модель не повернула жодного рядка тайм-кодів.")

    new_desc = merge_description_with_timecodes(
        old_desc, tc_lines, toc_lang_code=tlang or preview_lang
    )

    return {
        "video_id": vid,
        "title": title,
        "channel_id": my_ch,
        "transcript_lang": tlang or preview_lang,
        "old_description": old_desc,
        "new_description": new_desc,
        "timecode_lines": tc_lines,
    }


def push_timecodes_to_youtube(video_id: str, new_description: str) -> None:
    import youtube_playlists

    creds = youtube_playlists.load_credentials()
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    try:
        update_video_description(service, video_id=video_id, new_description=new_description)
    except HttpError as exc:
        raise TimecodesPipelineError(f"YouTube API: {exc}") from exc
