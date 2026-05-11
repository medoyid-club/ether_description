"""
Одноразова авторизація YouTube під Windows/Linux: відкриє браузер і запише JSON token.json.

Scopes —
  youtube.readonly   — список плейлистів (playlists.list)
  youtube.upload     — звичайне завантаження відеофайлу
  youtube.force-ssl  — створення ефіру та керування відео/трансляціями через Data API (liveBroadcast тощо)

Якщо ви вже видавали свіжий token.json без force-ssl — запустіть скрипт ще раз, щоб оновити токен із повним набором.

Запуск з корня проєкту (venv активний):
  python youtube_oauth_setup.py

Шляхи: client_secret — YOUTUBE_CLIENT_SECRET_FILE або перший client_secret*.json у корені;
       token.json — через youtube_token_file() / --out PATH.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

import settings

SCOPES = (
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
)

def resolve_client_secret() -> Path:
    p = settings.youtube_client_secret_file()
    if not p:
        raise SystemExit(
            "Не знайдено OAuth client JSON. Завантажте файл з GCP (desktop) у корінь як "
            "client_secret*.json або вкажіть YOUTUBE_CLIENT_SECRET_FILE у `.env`.",
        )
    return p


def main() -> None:
    parser = argparse.ArgumentParser(description="YouTube OAuth → token.json (JSON формат)")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Файл токена (за замовчуванням те саме що в бота: youtube_token_file / .env)",
    )
    args = parser.parse_args()
    out = args.out.resolve() if args.out else settings.youtube_token_file()
    cs = resolve_client_secret()
    flow = InstalledAppFlow.from_client_secrets_file(str(cs), list(SCOPES))
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent")
    out.write_text(creds.to_json(), encoding="utf-8")
    print("Записано:", out.resolve())
    print("Scopes:", list(SCOPES))


if __name__ == "__main__":
    main()
