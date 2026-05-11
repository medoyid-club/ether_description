"""
Створення запланованої YouTube-трансляції (Live Broadcast + Stream, RTMP / «Streaming software»).

Потрібні OAuth scopes з `youtube_oauth_setup.py` (зокрема `youtube.force-ssl`).

Налаштування, що задаються кодом / змінними середовища (перекривають бандл де вказано):
- приватність за замовчуванням `unlisted`, або `YOUTUBE_LIVE_PRIVACY_OVERRIDE`;
- категорія відео `YOUTUBE_LIVE_CATEGORY_ID` (Entertainment = 24);
- RTMP (`liveStreams`): зазвичай створюємо новий потік через `liveStreams.insert` і отримуємо ingestion URL + stream key із відповіді API.
- Опціонально `stream_key.txt` (або шлях `YOUTUBE_STREAM_KEY_FILE`) може містити один або кілька ключів (по одному рядку, порядок = пріоритет).
  Тоді викликається `liveStreams.list` лише щоб підібрати відповідний `liveStream.id` і перевикористати його. Якщо list падає
  або ключа серед потоків немає — автоматично робиться звичний `insert` (новий ключ у папці сесії).

Примітка про монетизацію тощо Partner Program аналогічно як раніше — попередження у `youtube_live_result.json`.
"""


from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from io import BytesIO

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError, MediaUploadSizeError
from googleapiclient.http import MediaFileUpload

import settings
import seo_bundle
import session_output
from youtube_playlists import load_credentials

log = logging.getLogger(__name__)

# YouTube thumbnails.set (Data API) повертає MediaUploadSizeError, якщо тіло > цього порогу (~2 МІБ).
YOUTUBE_THUMBNAIL_MAX_UPLOAD_BYTES = 2 * 1024 * 1024


class YoutubeLiveError(RuntimeError):
    pass


def _http_err_detail(exc: HttpError) -> str:
    content = getattr(exc, "content", None)
    if isinstance(content, (bytes, bytearray)):
        try:
            return content.decode("utf-8", errors="replace")[:2000]
        except Exception:
            return repr(content[:500])
    return str(exc)


def _pil_image_to_jpeg_bytes_under(im_rgb, max_bytes: int) -> bytes | None:
    """Повертає JPEG-байти з розміром файлу не більшим за max_bytes, або None."""
    from PIL import Image

    base = im_rgb.convert("RGB")
    w0, h0 = base.size

    scales: list[float] = []
    sc = 1.0
    for _ in range(28):
        scales.append(sc)
        if max(int(w0 * sc), int(h0 * sc)) <= 320:
            break
        sc *= 0.9

    qualities = list(range(92, 40, -3)) + [38, 35, 32, 29, 26, 22, 19]

    for scale in scales:
        if abs(scale - 1.0) < 1e-9:
            candidate = base
        else:
            nw = max(96, int(w0 * scale))
            nh = max(96, int(h0 * scale))
            candidate = base.resize((nw, nh), Image.Resampling.LANCZOS)

        for qual in qualities:
            buf = BytesIO()
            candidate.save(buf, format="JPEG", quality=qual, optimize=True, progressive=True)
            blob = buf.getvalue()
            if len(blob) <= max_bytes:
                return blob

    return None


def _prepare_thumbnail_for_youtube_upload(
    thumbnail_path: Path,
    session_dir: Path,
    warns: list[str],
) -> Path | None:
    """Якщо файл > 2 MiB — спроба зберегти JPEG у межах ліміту Data API thumbnails.set."""
    if not thumbnail_path.is_file():
        return None
    max_b = YOUTUBE_THUMBNAIL_MAX_UPLOAD_BYTES
    sz0 = thumbnail_path.stat().st_size
    if sz0 <= max_b:
        return thumbnail_path

    try:
        from PIL import Image, ImageOps
    except ImportError:
        w = (
            f"Мініатюру для YouTube пропущено: {sz0} байт > {max_b} (ліміт API). "
            "Встановіть Pillow (`pip install Pillow`) або завантажте файл ≤ 2 МБ."
        )
        warns.append(w)
        log.warning(w)
        return None

    try:
        with Image.open(thumbnail_path) as src:
            rgb = ImageOps.exif_transpose(src).convert("RGB").copy()
    except Exception as exc:
        w = f"Мініатюру для YouTube пропущено (неможливо прочитати зображення): {exc}"
        warns.append(w)
        log.warning(w)
        return None

    jpeg = _pil_image_to_jpeg_bytes_under(rgb, max_b)
    if jpeg is None:
        w = (
            f"Мініатюру для YouTube не вдалося стиснути до {max_b} байт (спробуйте менше зображення)."
        )
        warns.append(w)
        log.warning(w)
        return None

    outp = session_dir / "thumbnail_youtube_under_2mb.jpg"
    outp.write_bytes(jpeg)
    log.info(
        "YouTube thumbnail: стиснуто для API (%s байт → %s байт JPEG) → %s",
        sz0,
        len(jpeg),
        outp.name,
    )
    warns.append(
        "Мініатюру перекодовано в JPEG під ліміт YouTube Data API (~2 МБ) "
        f"({sz0} → {len(jpeg)} байт)."
    )
    return outp


def _lang_from_bundle(bundle: dict[str, Any]) -> str:
    yt = bundle.get("youtube") or {}
    loc = str(yt.get("default_language") or "").strip().lower()
    return loc if loc in {"uk", "ru", "en"} else "uk"


def _privacy_for_live() -> str:
    return settings.youtube_live_broadcast_privacy_override() or "unlisted"


def _trim_tags(tags: list[str], *, per_tag_max: int = 30, max_tags: int = 35) -> list[str]:
    out: list[str] = []
    for t in tags:
        s = str(t).strip()
        if not s:
            continue
        if len(s) > per_tag_max:
            s = s[:per_tag_max]
        if s not in out:
            out.append(s)
        if len(out) >= max_tags:
            break
    return out


_STREAM_KEY_LINES_SKIP = frozenset(
    {
        "DEFAULT",
        "REPLACE_ME",
        "REPLACE_WITH_YOUR_YOUTUBE_STREAM_KEY",
        "YOUR_STREAM_KEY_HERE",
        "YOUR_YOUTUBE_STREAM_KEY",
    },
)


def load_preferred_ingestion_stream_keys() -> list[str]:
    """
    Усі ключі з `youtube_local_stream_key_file()` для пошуку liveStream за `cdn.ingestionInfo.streamName`.

    Кожний непорожній рядок без коментарів = окремий ключ; порядок зверху вниз = пріоритет при зіставленні та при повторній
    спробі bind (якщо потік зайнятий).
    Коментарі (# …), плейсхолдери та рядки REPLACE_* ігноруються.
    """
    path = settings.youtube_local_stream_key_file()
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Не вдалося прочитати файл stream key (%s): %s", path, exc)
        return []

    out: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.upper() in _STREAM_KEY_LINES_SKIP or s.upper().startswith("REPLACE_"):
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def load_preferred_ingestion_stream_key() -> str | None:
    """Перший ключ з файла (для зворотної сумісності)."""
    keys = load_preferred_ingestion_stream_keys()
    return keys[0] if keys else None


def _ingestion_stream_name_from_item(it: dict[str, Any]) -> str:
    cdn = it.get("cdn") or {}
    if not isinstance(cdn, dict):
        return ""
    info = cdn.get("ingestionInfo") or {}
    if not isinstance(info, dict):
        return ""
    return str(info.get("streamName") or "").strip()


def _try_collect_my_live_streams(service: Any) -> tuple[list[dict[str, Any]], str | None]:
    """
    Повертає `(items, err_text)` або список liveStreams вашого каналу. Не кидає винятків навіть якщо YouTube вернув 500.
    Частину 500/502/503/504 пробує повторити.
    Мінімальний `part=id,cdn` — менший ризик backendError порівняно з надто широким part.
    """
    acc: list[dict[str, Any]] = []
    page_token: str | None = None
    try:
        while True:
            last_exc: HttpError | None = None
            resp: dict[str, Any] | None = None

            req = service.liveStreams().list(
                part="id,cdn",
                mine=True,
                maxResults=50,
                pageToken=page_token,
            )

            for attempt in range(3):
                try:
                    resp = req.execute()
                    break
                except HttpError as exc:
                    last_exc = exc
                    status_raw = getattr(getattr(exc, "resp", None), "status", None)
                    try:
                        code = int(str(status_raw))
                    except (TypeError, ValueError):
                        code = 0
                    detail_low = _http_err_detail(exc).lower()
                    retryable = (
                        code in {500, 502, 503, 504}
                        or "backenderror" in detail_low
                        or '"status": "internal"' in detail_low
                    )
                    if retryable and attempt < 2:
                        time.sleep(1.35 * (attempt + 1))
                        continue
                    raise exc

            if resp is None and last_exc is not None:
                raise last_exc

            assert isinstance(resp, dict)
            chunk = resp.get("items") or []
            acc.extend(it for it in chunk if isinstance(it, dict))

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return acc, None
    except HttpError as exc:
        tail = _http_err_detail(exc)
        log.warning("liveStreams.list недоступний: %s", tail[:900])
        return [], tail[:4000]
    except Exception as exc:
        log.warning("liveStreams.list unexpected: %s", exc)
        return [], str(exc)[:4000]


def _ordered_streams_matching_keys(
    streams: list[dict[str, Any]], ordered_unique_keys: list[str]
) -> list[dict[str, Any]]:
    """
    За ключами по черзі знаходимо liveStream із таким ingestion streamName. Один ресурс лише один раз у списку кандидатів.
    """
    want = [w.strip() for w in ordered_unique_keys if str(w).strip()]
    picked: list[dict[str, Any]] = []
    picked_ids: set[str] = set()

    for w in want:
        if not w:
            continue
        for it in streams:
            sid = str(it.get("id") or "").strip()
            if sid and sid not in picked_ids and _ingestion_stream_name_from_item(it) == w:
                picked.append(it)
                picked_ids.add(sid)
                break
    return picked


def _insert_new_live_stream(service: Any, stream_title: str) -> dict[str, Any]:
    stream_body = {
        "snippet": {
            "title": stream_title,
            "description": "RTMP stream for ether_description bot",
        },
        "cdn": {
            "ingestionType": "rtmp",
            "resolution": settings.youtube_live_stream_resolution(),
            "frameRate": settings.youtube_live_stream_frame_rate(),
        },
        "contentDetails": {"isReusable": True},
    }
    try:
        return (
            service.liveStreams()
            .insert(part="snippet,cdn,contentDetails,status", body=stream_body)
            .execute()
        )
    except HttpError as exc:
        raise YoutubeLiveError(_http_err_detail(exc)) from exc


def create_scheduled_broadcast(
    *,
    bundle: dict[str, Any],
    session_dir: Path,
    thumbnail_path: Path | None = None,
) -> dict[str, Any]:
    """
    Повертає узагальнений результат (для Telegram / JSON): id, ingest, попередження.
    У `session_dir` зберігаються сири відповіді API та файли ключів для OBS.
    """
    seo_bundle.ensure_bundle_skeleton(bundle)
    yt = bundle.get("youtube") or {}
    if not isinstance(yt, dict):
        raise YoutubeLiveError("youtube у бандлі не є об'єктом.")

    title = str(yt.get("title") or "").strip()
    desc = str(yt.get("description") or "").strip()
    start = str(yt.get("scheduled_start_time_rfc3339") or "").strip()

    if not title:
        raise YoutubeLiveError("Порожній youtube.title.")
    if not start:
        raise YoutubeLiveError("Порожній youtube.scheduled_start_time_rfc3339.")

    category_id = settings.youtube_live_category_id()
    lang = _lang_from_bundle(bundle)
    raw_tags = yt.get("tags")
    tags_src = raw_tags if isinstance(raw_tags, list) else []
    tags = _trim_tags([str(x) for x in tags_src])
    privacy = _privacy_for_live()
    playlists = [
        str(p).strip()
        for p in (yt.get("playlist_ids") or [])
        if str(p).strip()
    ]

    creds = load_credentials()
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)

    warns: list[str] = []

    broadcast_body = {
        "snippet": {"title": title, "description": desc, "scheduledStartTime": start},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        "contentDetails": {
            "monitorStream": {"enableMonitorStream": False, "broadcastStreamDelayMs": 0},
            "enableEmbed": True,
            "enableDvr": True,
            "recordFromStart": True,
            "projection": "rectangular",
            "enableLowLatency": False,
            "latencyPreference": "normal",
            "enableAutoStart": True,
            "enableAutoStop": True,
            "closedCaptionsType": "closedCaptionsDisabled",
        },
    }

    try:
        b_insert = (
            service.liveBroadcasts()
            .insert(part="snippet,status,contentDetails", body=broadcast_body)
            .execute()
        )
        session_output.dump_json(session_dir / "api_liveBroadcast_insert.json", b_insert)
        broadcast_id = str(b_insert.get("id") or "").strip()
        if not broadcast_id:
            raise YoutubeLiveError("YouTube не повернув id ефіру.")
    except HttpError as exc:
        raise YoutubeLiveError(_http_err_detail(exc)) from exc

    stream_title = f"Streaming software — {title}"[:128]

    pref_keys = load_preferred_ingestion_stream_keys()
    reused_from_local_key_file = False
    bind_conflict_fallback_used = False
    stream_resource: dict[str, Any] | None = None

    mine_streams: list[dict[str, Any]] = []
    list_streams_err: str | None = None
    reuse_candidates: list[dict[str, Any]] = []

    if pref_keys:
        mine_streams, list_streams_err = _try_collect_my_live_streams(service)
        session_output.dump_json(
            session_dir / "live_streams_mine_probe.json",
            {
                "stream_key_line_count_unique": len(pref_keys),
                "mine_live_streams_count": len(mine_streams),
                "live_streams_list_error_preview": (
                    None if not list_streams_err else list_streams_err[:4000]
                ),
            },
        )
        if list_streams_err:
            warns.append(
                "liveStreams.list не виконано (нерідко короткочасний backendError/500 або мережа). "
                "Без успішного list неможливо знайти liveStream за ключем із файла — створимо новий liveStream через insert. "
                "Новий stream key є у obs_stream_key.txt.\n"
                + list_streams_err[:1200]
            )
        elif not mine_streams:
            warns.append(
                "YouTube повернув 0 ваших liveStreams (mine=true) — ключ(і) з stream_key.txt не до чого зіставити. "
                "Створюємо новий liveStream."
            )
        else:
            reuse_candidates = _ordered_streams_matching_keys(mine_streams, pref_keys)
            if not reuse_candidates:
                warns.append(
                    "Жоден Stream key із stream_key.txt не дорівнює ingestionInfo.streamName жодному з ваших потоків. "
                    "Створюємо новий liveStream."
                )

    had_reuse_candidates = bool(reuse_candidates)
    reuse_bind_errors: list[str] = []

    if reuse_candidates:
        for cand in reuse_candidates:
            cand_id = str(cand.get("id") or "").strip()
            if not cand_id:
                continue
            try:
                bind_ok = (
                    service.liveBroadcasts()
                    .bind(
                        part="id,snippet,contentDetails,status",
                        id=broadcast_id,
                        streamId=cand_id,
                    )
                    .execute()
                )
                session_output.dump_json(session_dir / "api_liveBroadcast_bind.json", bind_ok)
                stream_resource = cand
                reused_from_local_key_file = True
                session_output.dump_json(session_dir / "liveStream_from_stream_key_txt.json", cand)
                log.info(
                    "YouTube Live: прив'язано liveStream id=%s (перший успішний кандидат за stream_key.txt).",
                    cand_id,
                )
                break
            except HttpError as exc:
                reuse_bind_errors.append(
                    f"liveStream {cand_id}: {_http_err_detail(exc)[:900]}"
                )

        if not reused_from_local_key_file and reuse_bind_errors:
            session_output.dump_json(
                session_dir / "live_stream_bind_reuse_errors.json",
                {"errors": reuse_bind_errors[:20]},
            )
            warns.append(
                "Усі кандидати з ключів із stream_key.txt зайняті або їх неможливо прив’язати — створено новий liveStream."
            )

    if stream_resource is None:
        s_insert = _insert_new_live_stream(service, stream_title)
        stream_resource = s_insert
        session_output.dump_json(session_dir / "api_liveStream_insert.json", s_insert)

        sid_new = str(stream_resource.get("id") or "").strip()
        if not sid_new:
            raise YoutubeLiveError("Створено liveStream, але відповідь API без id.")

        try:
            bound_new = (
                service.liveBroadcasts()
                .bind(part="id,snippet,contentDetails,status", id=broadcast_id, streamId=sid_new)
                .execute()
            )
            if had_reuse_candidates:
                bind_conflict_fallback_used = True
                session_output.dump_json(
                    session_dir / "api_liveBroadcast_bind_after_failed_reuse_candidates.json",
                    bound_new,
                )
            else:
                session_output.dump_json(session_dir / "api_liveBroadcast_bind.json", bound_new)
        except HttpError as exc:
            raise YoutubeLiveError(_http_err_detail(exc)) from exc

    stream_id = str(stream_resource.get("id") or "").strip()
    if not stream_id:
        raise YoutubeLiveError("liveStream із API без id.")

    try:
        mon_body = {
            "id": broadcast_id,
            "monetizationDetails": {
                "adsMonetizationStatus": "on",
                "cuepointSchedule": {
                    "enabled": True,
                    "ytOptimizedCuepointConfig": "MEDIUM",
                },
            },
        }
        mon = (
            service.liveBroadcasts()
            .update(part="monetizationDetails", body=mon_body)
            .execute()
        )
        session_output.dump_json(session_dir / "api_liveBroadcast_monetization_update.json", mon)
    except HttpError as exc:
        w = f"monetization update skipped: {_http_err_detail(exc)}"
        log.warning(w)
        warns.append(w)

    if thumbnail_path and thumbnail_path.is_file():
        thumb_for_api = _prepare_thumbnail_for_youtube_upload(thumbnail_path, session_dir, warns)
        if thumb_for_api is None:
            pass
        else:
            try:
                up = (
                    service.thumbnails()
                    .set(videoId=broadcast_id, media_body=MediaFileUpload(str(thumb_for_api)))
                    .execute()
                )
                session_output.dump_json(session_dir / "api_thumbnails_set.json", up)
            except (HttpError, MediaUploadSizeError) as exc:
                detail = (
                    _http_err_detail(exc) if isinstance(exc, HttpError) else str(exc)
                )
                w = f"thumbnail set skipped: {detail}"
                log.warning(w)
                warns.append(w)

    video_id = broadcast_id
    try:
        listed = service.videos().list(part="snippet,status", id=video_id, maxResults=1).execute()
        session_output.dump_json(session_dir / "api_videos_list_before_update.json", listed)
        items = listed.get("items") or []
        if not items:
            warns.append("videos.list не знайшов відео за id ефіру — пропускаємо videos.update.")
        else:
            prev = items[0]
            snack = dict(prev.get("snippet") or {})
            stat = dict(prev.get("status") or {})

            snack["title"] = title
            snack["description"] = desc
            snack["categoryId"] = category_id
            snack["defaultLanguage"] = lang
            snack["defaultAudioLanguage"] = lang
            snack["tags"] = tags

            stat["privacyStatus"] = privacy
            stat["embeddable"] = True
            stat["license"] = "creativeCommon"
            stat["selfDeclaredMadeForKids"] = False
            stat["commentsDisabled"] = False
            stat["containsSyntheticMedia"] = False

            upd_body = {"id": video_id, "snippet": snack, "status": stat}
            upd = service.videos().update(part="snippet,status", body=upd_body).execute()
            session_output.dump_json(session_dir / "api_videos_update.json", upd)
    except HttpError as exc:
        w = f"videos.update skipped: {_http_err_detail(exc)}"
        log.warning(w)
        warns.append(w)

    playlist_results: list[dict[str, Any]] = []
    for pid in playlists:
        try:
            pl_ins = (
                service.playlistItems()
                .insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "playlistId": pid,
                            "resourceId": {"kind": "youtube#video", "videoId": video_id},
                        },
                    },
                )
                .execute()
            )
            playlist_results.append({"playlist_id": pid, "ok": True, "response": pl_ins})
        except HttpError as exc:
            playlist_results.append(
                {"playlist_id": pid, "ok": False, "error": _http_err_detail(exc)},
            )

    session_output.dump_json(session_dir / "playlist_inserts.json", playlist_results)

    cdn = (stream_resource.get("cdn") or {}) if isinstance(stream_resource.get("cdn"), dict) else {}
    ingestion = (cdn.get("ingestionInfo") or {}) if isinstance(cdn.get("ingestionInfo"), dict) else {}

    rtmp_server = str(ingestion.get("ingestionAddress") or "").strip()
    rtmp_key = str(ingestion.get("streamName") or "").strip()
    if not rtmp_server or not rtmp_key:
        warns.append(
            "YouTube повернув неповні RTMP ingestion поля у liveStream відповіді — "
            "відкрийте Студія → Налаштування трансляції та скопіюйте stream key уручну."
        )

    session_output.write_text(
        session_dir / "obs_stream_key.txt",
        f"=== RTMP (vMix / OBS / Custom «Streaming software») ===\n"
        f"Server:\n{rtmp_server}\n\n"
        f"Stream key (КОНФІДЕНЦІЙНО):\n{rtmp_key}\n\n"
        f"Broadcast / video ID: {broadcast_id}\n"
        f"Stream ID: {stream_id}\n",
    )

    watch = f"https://www.youtube.com/watch?v={video_id}"
    studio = f"https://studio.youtube.com/video/{video_id}/livestreaming"

    reused_stream_effective = reused_from_local_key_file and not bind_conflict_fallback_used

    result = {
        "broadcast_id": broadcast_id,
        "stream_id": stream_id,
        "video_id": video_id,
        "watch_url": watch,
        "studio_url": studio,
        "reused_existing_live_stream_from_stream_key_txt": reused_stream_effective,
        "bind_conflict_fallback_created_new_stream": bind_conflict_fallback_used,
        "stream_key_local_file_path": str(settings.youtube_local_stream_key_file().resolve()),
        "ingest": {
            "rtmp_server": rtmp_server,
            "stream_key_saved_to": str((session_dir / "obs_stream_key.txt").resolve()),
        },
        "privacy_status": privacy,
        "category_id": category_id,
        "default_language": lang,
        "tags_applied": tags,
        "thumbnail_path": str(thumbnail_path.resolve()) if thumbnail_path else None,
        "warnings": warns,
        "playlists": playlist_results,
    }
    session_output.dump_json(session_dir / "youtube_live_result.json", result)

    try:
        service.close()
    except Exception:
        pass

    return result
