"""
YouTube Data API v3 — плейлісти поточного каналу (`mine=true`).

Підтримка `token.json`:
  • JSON від InstalledAppFlow / Credentials.to_json() (рекомендовано; див. youtube_oauth_setup.py)
  • pickle з Credentials (сумісність зі старими експортами)

Для успішних `playlists.list` зазвичай потрібен scope `youtube.readonly` або ширший (`youtube.force-ssl`, `youtube`).
Токен лише з `youtube.upload` дає 403 на плейлисти — див. `youtube_oauth_setup.py`. Для live / ефіру знадобиться `youtube.force-ssl`.
"""

from __future__ import annotations

import json
import logging
import pickle
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import settings

log = logging.getLogger("youtube_playlists")


class YouTubePlaylistError(RuntimeError):
    """Неможливо завантажити плейлісти (auth, HTTP, налаштування)."""


def _token_path() -> Path:
    return settings.youtube_token_file()


def load_credentials() -> Credentials:
    """
    Завантажує або оновлює OAuth credentials з token.json / pickle.
    """
    path = _token_path()
    if not path.is_file():
        raise YouTubePlaylistError(
            f"Немає файлу токена: {path}. Скопіюйте token.json у корінь або задайте YOUTUBE_TOKEN_FILE у `.env`.",
        )

    raw = path.read_bytes()
    looks_like_json = raw.lstrip().startswith(b"{")

    if looks_like_json:
        info = json.loads(raw.decode("utf-8"))
        creds = Credentials.from_authorized_user_info(info)
    else:
        try:
            creds = pickle.loads(raw, fix_imports=True)
        except Exception as exc:
            raise YouTubePlaylistError(
                "token.json не розпізнано: ні JSON OAuth, ані pickle credentials. Перевстановіть токен.",
            ) from exc
        if not isinstance(creds, Credentials):
            raise YouTubePlaylistError("pickle у token.json не містить Credentials Google.")

    secret_path = settings.youtube_client_secret_file()
    if not secret_path:
        raise YouTubePlaylistError(
            "Не знайдено client_secret*.json у корені проєкту — додайте файл або змінну YOUTUBE_CLIENT_SECRET_FILE у `.env`.",
        )

    if not creds.valid:
        try:
            if creds.refresh_token:
                creds.refresh(Request())
            else:
                raise YouTubePlaylistError("Токен прострочений і немає refresh_token — авторизуйтеся знову.")
        except Exception as exc:
            raise YouTubePlaylistError(f"Не вдалося оновити OAuth токен: {exc}") from exc

        _persist_credentials(path, creds, as_json_text=looks_like_json)

    return creds


def _persist_credentials(path: Path, creds: Credentials, *, as_json_text: bool) -> None:
    try:
        if as_json_text or path.suffix.lower() == ".json":
            path.write_text(creds.to_json(), encoding="utf-8")
            log.info("OAuth токен оновлено (JSON): %s", path)
        else:
            path.write_bytes(pickle.dumps(creds, protocol=pickle.HIGHEST_PROTOCOL))
            log.info("OAuth токен оновлено (pickle): %s", path)
    except OSError as exc:
        log.warning("Не вдалося записати оновлений токен %s: %s", path, exc)


def fetch_my_playlists(*, max_total: int = 80) -> list[dict[str, str]]:
    """
    Усі доступні мені плейлісти каналу. Формат елемента збігається з seo_bundle:
    playlist_id, title, canonical_url.
    """
    creds = load_credentials()

    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    playlists: list[dict[str, str]] = []
    page_token: str | None = None

    try:
        while True:
            req = service.playlists().list(
                part="snippet",
                mine=True,
                maxResults=min(50, max_total - len(playlists)),
                pageToken=page_token,
            )
            resp: dict[str, Any] = req.execute()
            items = resp.get("items") or []
            if not items:
                break

            for it in items:
                pid = str(it["id"])
                title = (
                    ((it.get("snippet") or {}).get("localized") or {}).get("title")
                    or (it.get("snippet") or {}).get("title")
                    or "(без назви)"
                )
                url = f"https://www.youtube.com/playlist?list={pid}"
                playlists.append(
                    {"playlist_id": pid, "title": str(title).strip(), "canonical_url": url}
                )

            page_token = resp.get("nextPageToken")
            if not page_token or len(playlists) >= max_total:
                break

    except HttpError as exc:
        hint = ""
        status = getattr(exc, "resp", None)
        status_code = getattr(status, "status", None)
        detail = getattr(exc, "content", None)
        snippet = repr(detail[:200]) if isinstance(detail, (bytes, bytearray)) else str(exc)
        if status_code == 403 or status_code == "403":
            hint = (
                " Можливо, поточному токену бракує scope для читання плейлистів (потрібен youtube.readonly "
                "або повний youtube). Отримайте новий token.json з потрібними доступами у Google OAuth."
            )
        raise YouTubePlaylistError(f"YouTube API HTTP {status_code}: {snippet}{hint}") from exc
    finally:
        try:
            service.close()
        except Exception:
            pass

    return playlists
