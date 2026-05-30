"""
Тайм-коди для опису відео: субтитри через YouTube Data API (captions) + Gemini → вставка перед блоком #тегів.

Окремо від SEO-майстра; потрібні ті самі OAuth `token.json`, що й для плейлистів / live
(scope `youtube.force-ssl` — уже в youtube_oauth_setup.py).
"""

from __future__ import annotations

import io
import logging
import re
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

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

_SRT_TIMESTAMP = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)

_ISO8601_DURATION = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")

_CAPTION_LANG_PRIORITY = ("uk", "ru", "en")


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


def parse_iso8601_duration(raw: str) -> float:
    """PT1H2M3S → секунди (YouTube contentDetails.duration)."""
    m = _ISO8601_DURATION.match((raw or "").strip())
    if not m:
        return 0.0
    hours, minutes, seconds = (int(part or 0) for part in m.groups())
    return float(hours * 3600 + minutes * 60 + seconds)


def _srt_timestamp_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_srt_captions(content: str) -> list[dict[str, Any]]:
    """SRT → list[{text, start, duration}]."""
    entries: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", (content or "").strip()):
        lines = [ln.rstrip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue

        ts_idx = 1 if lines[0].strip().isdigit() else 0
        if ts_idx >= len(lines):
            continue

        match = _SRT_TIMESTAMP.match(lines[ts_idx].strip())
        if not match:
            continue

        start = _srt_timestamp_to_seconds(*match.groups()[:4])
        end = _srt_timestamp_to_seconds(*match.groups()[4:8])
        text = " ".join(line.strip() for line in lines[ts_idx + 1 :] if line.strip())
        if not text:
            continue
        entries.append({"text": text, "start": start, "duration": max(0.0, end - start)})
    return entries


def _caption_track_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    snippet = item.get("snippet") or {}
    lang = _normalize_transcript_lang(str(snippet.get("language") or ""))
    try:
        lang_rank = _CAPTION_LANG_PRIORITY.index(lang)
    except ValueError:
        lang_rank = len(_CAPTION_LANG_PRIORITY)
    kind_rank = 0 if str(snippet.get("trackKind") or "") != "ASR" else 1
    return (lang_rank, kind_rank, lang)


def _select_caption_track(items: list[dict[str, Any]]) -> dict[str, Any]:
    return min(items, key=_caption_track_sort_key)


def _download_caption_srt(service, caption_id: str) -> str:
    request = service.captions().download(id=caption_id, tfmt="srt")
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue().decode("utf-8", errors="replace")


def fetch_caption_entries(service, video_id: str) -> tuple[list[dict[str, Any]], str]:
    """
    Завантажує субтитри власного відео через YouTube Data API (captions.list / captions.download).

    Потрібен OAuth з youtube.force-ssl; працює з VPS без проксі, на відміну від публічного скрейпінгу.
    """
    try:
        listed = service.captions().list(part="snippet", videoId=video_id).execute()
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in (403, "403"):
            raise TimecodesPipelineError(
                "YouTube API: немає доступу до субтитрів (403). "
                "Перевірте, що token.json видано з scope youtube.force-ssl "
                "(запустіть youtube_oauth_setup.py ще раз)."
            ) from exc
        raise TimecodesPipelineError(f"YouTube API captions.list: {exc}") from exc

    items = listed.get("items") or []
    if not items:
        raise TimecodesPipelineError(
            f"Для відео {video_id} немає доріжок субтитрів у YouTube. "
            "У Studio увімкніть автогенерацію або завантажте субтитри вручну."
        )

    available = [
        f"{_normalize_transcript_lang(str((it.get('snippet') or {}).get('language') or ''))}"
        f"({(it.get('snippet') or {}).get('trackKind', '?')})"
        for it in items
    ]
    log.info("Caption tracks for %s via YouTube API: %s", video_id, available)

    track = _select_caption_track(items)
    snippet = track.get("snippet") or {}
    caption_id = str(track["id"])
    lang = _normalize_transcript_lang(str(snippet.get("language") or ""))

    try:
        srt_body = _download_caption_srt(service, caption_id)
    except HttpError as exc:
        raise TimecodesPipelineError(f"YouTube API captions.download: {exc}") from exc

    entries = parse_srt_captions(srt_body)
    if not entries:
        raise TimecodesPipelineError(
            f"Субтитри для {video_id} завантажено, але SRT порожній або не розпізнано."
        )

    log.info(
        "Captions downloaded for %s: lang=%s segments=%s trackKind=%s",
        video_id,
        lang,
        len(entries),
        snippet.get("trackKind"),
    )
    return entries, lang


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

    entries, tlang = fetch_caption_entries(service, vid)
    cd = item.get("contentDetails") or {}
    duration_sec = parse_iso8601_duration(str(cd.get("duration") or "")) or _duration_sec_from_entries(
        entries
    )
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
