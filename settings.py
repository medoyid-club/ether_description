"""Завантаження налаштувань з локального `.env` у корені проєкту."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent


def _load_dotenv_files() -> None:
    env_path = _ROOT / ".env"
    if env_path.is_file():
        # override=True: значення з `.env` мають перекривати глобальні змінні Windows/CI,
        # інакше старий GEMINI_API_KEY у системі блокує щойно створений ключ у файлі.
        load_dotenv(env_path, override=True)


_load_dotenv_files()


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задано змінну середовища {name} (додайте у файл `.env`).")
    return value


def telegram_bot_token() -> str:
    return require_env("TELEGRAM_BOT_TOKEN")


def gemini_api_key() -> str | None:
    v = os.getenv("GEMINI_API_KEY", "").strip()
    return v or None


def require_gemini_api_key() -> str:
    k = gemini_api_key()
    if not k:
        raise RuntimeError("Не задано GEMINI_API_KEY у `.env`.")
    return k


def gemini_model_override() -> str | None:
    """Форс моделі, напр. gemini-3.0-pro (без префікса models/)."""
    v = os.getenv("GEMINI_MODEL", "").strip()
    return v or None


def youtube_client_secret_file() -> Path | None:
    raw = os.getenv("YOUTUBE_CLIENT_SECRET_FILE", "").strip()
    if not raw:
        matches = sorted(_ROOT.glob("client_secret*.json"))
        return matches[0] if matches else None
    p = Path(raw)
    if not p.is_absolute():
        p = _ROOT / p
    return p if p.is_file() else None


def youtube_token_file() -> Path:
    """OAuth token (JSON від google або pickle з іншої машини — див. youtube_playlists.load_credentials)."""
    raw = os.getenv("YOUTUBE_TOKEN_FILE", "token.json").strip() or "token.json"
    p = Path(raw)
    return p if p.is_absolute() else _ROOT / p


def youtube_channel_display_name() -> str:
    """Назва каналу в USER_INPUT для Gemini; за бажанням YOUTUBE_CHANNEL_DISPLAY_NAME у `.env`."""
    v = os.getenv("YOUTUBE_CHANNEL_DISPLAY_NAME", "").strip()
    if v:
        return v
    return "YouTube канал"


def output_sessions_root() -> Path:
    """Корінь для збереження сесій бота локально (за замовчуванням `./output`)."""
    raw = os.getenv("OUTPUT_DIR", "").strip()
    rel = Path(raw) if raw else Path("output")
    return rel if rel.is_absolute() else _ROOT / rel


def youtube_live_category_id() -> str:
    """
    Числовий ID категорії YouTube для відео/ефіру (за замовчуванням Entertainment = 24).
    Перевірте для вашого регіону через videoCategories.list.
    """
    v = os.getenv("YOUTUBE_LIVE_CATEGORY_ID", "").strip()
    return v or "24"


def youtube_live_stream_resolution() -> str:
    return os.getenv("YOUTUBE_LIVE_STREAM_RESOLUTION", "1080p").strip() or "1080p"


def youtube_live_stream_frame_rate() -> str:
    return os.getenv("YOUTUBE_LIVE_STREAM_FRAME_RATE", "30fps").strip() or "30fps"


def youtube_live_broadcast_privacy_override() -> str | None:
    """
    Якщо задано (private|unlisted|public), перекриває те, що в бандлі.
    Інакше використовуємо `unlisted` жорстко в коді live (за вимогою оператора).
    """
    v = os.getenv("YOUTUBE_LIVE_PRIVACY_OVERRIDE", "").strip().lower()
    return v if v in {"private", "unlisted", "public"} else None


def youtube_local_stream_key_file() -> Path:
    """
    Файл з бажаним Stream key для vMix: якщо на каналі вже є liveStream з таким ключем,
    бот зв’язує його з новою трансляцією замість liveStreams.insert.
    Змінна YOUTUBE_STREAM_KEY_FILE або `stream_key.txt` у корені.
    """
    raw = os.getenv("YOUTUBE_STREAM_KEY_FILE", "stream_key.txt").strip() or "stream_key.txt"
    p = Path(raw)
    return p if p.is_absolute() else _ROOT / p


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def telegram_httpx_request():
    """
    Клієнт з довшими таймаутами за замовчуванням (у PTB ~5 с часто мало для першого connect).

    Параметри пулу/таймаутів задаються лише тут: після `.request(instance)` PTB не дозволяє викликати
    `.pool_timeout(...)` чи `.connection_pool_size(...)` у Application.builder — RuntimeError.

    Додатково: TELEGRAM_PROXY (http://user:pass@host:port).
    """
    import platform as plt

    from telegram.request import HTTPXRequest

    is_win = plt.system() == "Windows"
    # Як у D:\\work\\YouTube enhanced_bot_polling для Windows — малий пул; можна перевизначити з .env.
    pool_default = 1 if is_win else 8
    pool_timeout_default = 30.0 if is_win else 15.0

    proxy = os.getenv("TELEGRAM_PROXY", "").strip() or None
    return HTTPXRequest(
        connection_pool_size=_int_env("TELEGRAM_HTTP_CONNECTION_POOL_SIZE", pool_default),
        connect_timeout=_float_env("TELEGRAM_HTTP_CONNECT_TIMEOUT", 30.0),
        read_timeout=_float_env("TELEGRAM_HTTP_READ_TIMEOUT", 45.0),
        write_timeout=_float_env("TELEGRAM_HTTP_WRITE_TIMEOUT", 45.0),
        pool_timeout=_float_env(
            "TELEGRAM_HTTP_POOL_TIMEOUT",
            pool_timeout_default,
        ),
        proxy=proxy,
    )


def telegram_bootstrap_retries() -> int:
    """Скільки разів повторювати підключення під час старту (0 у PTB означає «без повторів»)."""
    return _int_env("TELEGRAM_BOOTSTRAP_RETRIES", 25)


def telegram_debug_incoming_updates() -> bool:
    """TELEGRAM_DEBUG_UPDATES=1 — INFO-лог кожного вхідного message/callback (до хендлерів групи 0)."""
    return os.getenv("TELEGRAM_DEBUG_UPDATES", "").strip() in {"1", "true", "True", "yes", "YES"}


def telegram_watch_chat_ids() -> tuple[int, ...]:
    """
    TELEGRAM_CHANNEL: один або кілька числових id через кому.
    Для мігрованих груп автоматично додається «дзеркало» між -xxxxxxxxxx та -100xxxxxxxxxx,
    бо в апдейтах часто один варіант, а в .env — інший.
    """
    raw = os.getenv("TELEGRAM_CHANNEL", "").strip()
    if not raw:
        return ()

    merged: set[int] = set()
    for part in raw.replace(" ", "").split(","):
        if not part or part.startswith("@"):
            continue
        try:
            base = int(part)
        except ValueError:
            continue
        merged.add(base)
        a = abs(base)
        s = str(a)
        if base < 0 and s.startswith("100") and len(s) > 3:
            try:
                tail = int(s[3:])
                merged.add(-tail)
            except ValueError:
                pass
        elif base < 0 and not s.startswith("100"):
            try:
                merged.add(-int(f"100{s}"))
            except ValueError:
                pass

    return tuple(sorted(merged))
