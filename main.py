"""
Етап 1: Telegram-бот — прийом теми ефіру (текст або .txt-документ).

Запуск з кореня проєкту після `pip install -r requirements.txt`:
  python main.py

Команди: /start або /help; для SEO-майстра спочатку /new (/draft або /seo), потім чорновик. /cancel — скасувати сценарій.
Окремо: /timestamps (/time, /toc, /timecodes, /chapters) — тайм-коди в опис відео за посиланням.
"""

from __future__ import annotations

import asyncio
import logging
import platform
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# На Windows із httpx/SSL часті таймаути до api.telegram.org без Proactor
# (той самий прийом, що й у `D:\\work\\YouTube\\src\\bot\\run_telegram_bot.py`
# та `enhanced_bot_polling.py`): політику циклу подій задаємо ДО імпортів telegram.
if platform.system() == "Windows":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except AttributeError:
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except AttributeError:
            pass

import gemini_generate
import seo_bundle
import session_output
import settings
import youtube_live
import youtube_playlists
import youtube_timecodes
from speakers_catalog import gemini_speaker_dict, list_speakers
from io import BytesIO

from telegram import Document, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction, MessageLimit
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
# У логах httpx на INFO видно повний URL з токеном бота — небезпечно при шарингу логів.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ether_bot")

MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
# Ліміт прийому заставки в Telegram; окремо YouTube `thumbnails.set` приймає не більш ~2 MiB —
# там у `youtube_live` при потребі перекодування в JPEG.
MAX_COVER_FILE_BYTES = 12 * 1024 * 1024
PREVIEW_LIMIT = 800

CONTENT_STATE = 1
LANGUAGE_STATE = 2
SPEAKERS_STATE = 3
STYLE_STATE = 4
TIME_STATE = 5
PLAYLISTS_STATE = 6
SEO_REVIEW_STATE = 7
COVER_STATE = 8

TIMESTAMP_STATE_WAIT_URL = 101
TIMESTAMP_STATE_CONFIRM = 102

SEO_LOCALE_LABELS = {
    "uk": "🇺🇦 Українська",
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
}

# Ключ → підпис; значення `style` у JSON для Gemini — сам підпис (як у system_promt / seo_bundle).
STYLE_CHOICES: tuple[tuple[str, str], ...] = (
    ("neutral", "Нейтральний, інформативний"),
    ("philosophical", "Філософський, рефлексивний"),
    ("political", "Політичний, гострий"),
    ("conversational", "Розмовний, для чату з глядачами"),
    ("academic_light", "Академічний, але доступний"),
)
STYLE_LABELS: dict[str, str] = dict(STYLE_CHOICES)

TIMEZONE_CANONICAL = "Europe/Kyiv"
KYIV_TZ = ZoneInfo(TIMEZONE_CANONICAL)

TIME_PRESETS: tuple[tuple[str, str], ...] = (
    ("today_18", "Сьогодні 18:00"),
    ("today_21", "Сьогодні 21:00"),
    ("tomorrow_18", "Завтра 18:00"),
    ("tomorrow_21", "Завтра 21:00"),
)

# PTB обробляє кожну групу окремо: після ConversationHandler (група 0) все одно викликається
# MessageHandler у групі 1. Позначаємо update_id, щоб nudge не дублював відповідь після прийому чернетки.
SKIP_LONG_TEXT_NUDGE_UPDATE_ID = "_skip_long_text_nudge_update_id"
WIZARD_STEP_KEY = "wizard_step"


def _truncate_button_caption(text: str, limit: int = 64) -> str:
    t = text.strip()
    if len(t) <= limit:
        return t
    return t[: max(1, limit - 1)] + "…"


def language_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(SEO_LOCALE_LABELS["uk"], callback_data="lang:uk")],
            [InlineKeyboardButton(SEO_LOCALE_LABELS["ru"], callback_data="lang:ru")],
            [InlineKeyboardButton(SEO_LOCALE_LABELS["en"], callback_data="lang:en")],
        ]
    )


def _speaker_button_title(display_name: str, selected: bool) -> str:
    prefix = "✓ " if selected else ""
    max_len = 64 - len(prefix)
    name = display_name.strip()
    if len(name) > max_len:
        name = name[: max(1, max_len - 1)] + "…"
    return prefix + name


def speakers_choice_keyboard(selected: set[int]) -> InlineKeyboardMarkup:
    catalog = list_speakers()
    rows: list[list[InlineKeyboardButton]] = []
    for i, entry in enumerate(catalog):
        rows.append(
            [
                InlineKeyboardButton(
                    _speaker_button_title(str(entry["display_name"]), i in selected),
                    callback_data=f"spk:t:{i}",
                )
            ]
        )
    n = len(selected)
    rows.append(
        [InlineKeyboardButton(f"✅ Готово ({n})", callback_data="spk:done")],
    )
    return InlineKeyboardMarkup(rows)


def style_choice_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, label in STYLE_CHOICES:
        rows.append(
            [
                InlineKeyboardButton(
                    _truncate_button_caption(label),
                    callback_data=f"sty:{key}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def time_choice_keyboard() -> InlineKeyboardMarkup:
    """
    Префікс callback трьома частинами: time:preset:<ключ>.
    Обовʼязково лишається ручний ввід текстом під повідомленням.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    TIME_PRESETS[0][1],
                    callback_data=f"time:preset:{TIME_PRESETS[0][0]}",
                ),
                InlineKeyboardButton(
                    TIME_PRESETS[1][1],
                    callback_data=f"time:preset:{TIME_PRESETS[1][0]}",
                ),
            ],
            [
                InlineKeyboardButton(
                    TIME_PRESETS[2][1],
                    callback_data=f"time:preset:{TIME_PRESETS[2][0]}",
                ),
                InlineKeyboardButton(
                    TIME_PRESETS[3][1],
                    callback_data=f"time:preset:{TIME_PRESETS[3][0]}",
                ),
            ],
        ]
    )


def playlists_choice_keyboard(
    playlists: list[dict[str, str]], selected: set[int]
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, pl in enumerate(playlists):
        rows.append(
            [
                InlineKeyboardButton(
                    _speaker_button_title(str(pl.get("title", "")), i in selected),
                    callback_data=f"ytpl:t:{i}",
                )
            ]
        )
    n = len(selected)
    rows.append(
        [
            InlineKeyboardButton(f"✅ Готово ({n})", callback_data="ytpl:done"),
            InlineKeyboardButton("Пропустити", callback_data="ytpl:skip"),
        ],
    )
    return InlineKeyboardMarkup(rows)


def seo_review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Прийняти текст пакета", callback_data="seo:accept")]]
    )


def cover_step_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Без заставки", callback_data="cov:skip")]]
    )


def _message_reply_kwargs(anchor: Message | None) -> dict[str, int]:
    """message_thread_id для форум-тем у супергрупах."""
    if anchor is None:
        return {}
    tid = getattr(anchor, "message_thread_id", None)
    return {"message_thread_id": tid} if tid is not None else {}


def _split_long_telegram_text(text: str, *, max_chunk: int) -> list[str]:
    t = text.strip()
    if not t:
        return []
    chunks: list[str] = []
    offset = 0
    while offset < len(t):
        if len(t) - offset <= max_chunk:
            chunks.append(t[offset:])
            break
        window_end = offset + max_chunk
        slice_ = t[offset:window_end]
        cut = slice_.rfind("\n\n")
        if cut < max_chunk // 3:
            cut = slice_.rfind("\n")
        if cut < max_chunk // 3:
            cut = len(slice_) - 1
        chunks.append(t[offset : offset + cut + 1].rstrip())
        offset += cut + 1
    return chunks


def _youtube_preview_from_bundle(bundle: dict, *, desc_max: int) -> tuple[str, str]:
    yt = bundle.get("youtube") if isinstance(bundle.get("youtube"), dict) else {}
    title = str(yt.get("title") or "").strip() or "—"
    desc = str(yt.get("description") or "").strip()
    preview = desc[:desc_max].rstrip() if desc else ""
    if len(desc) > desc_max:
        preview += "…"
    return title, preview or "—"


def _looks_like_cover_image_document(doc: Document | None) -> bool:
    if doc is None:
        return False
    mime = (doc.mime_type or "").lower().strip()
    fn = (doc.file_name or "").lower()
    if mime.startswith("image/"):
        return True
    return fn.endswith((".jpg", ".jpeg", ".png", ".webp"))


async def _send_seo_generated_messages(
    bot,
    *,
    chat_id: int,
    reply_kw: dict[str, int],
    bundle: dict,
) -> None:
    seo_bundle.ensure_bundle_skeleton(bundle)

    chunk_cap = min(3900, MessageLimit.MAX_TEXT_LENGTH - 64)
    title, desc_prev = _youtube_preview_from_bundle(bundle, desc_max=900)

    header = (
        "Gemini: SEO зібрано (одне звернення до API за сесію /new).\n\n"
        f"YouTube заголовок:\n{title}\n\n"
        f"YouTube опис (початок):\n{desc_prev}\n"
    )
    tg = bundle.get("telegram") if isinstance(bundle.get("telegram"), dict) else {}
    full_plain = str(tg.get("full_package_plain") or "").strip()
    chunks = _split_long_telegram_text(full_plain, max_chunk=chunk_cap)

    await bot.send_message(chat_id=chat_id, text=header, **reply_kw)

    if not chunks:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ Модель не повернула `telegram.full_package_plain`. Можете надіслати текст пакета вручну "
                "одним повідомленням або /cancel і /new."
            ),
            **reply_kw,
        )
    else:
        nch = len(chunks)
        for i, chunk in enumerate(chunks, start=1):
            prefix = f"Текст пакета Telegram ({i}/{nch})\n\n" if nch > 1 else "Текст пакета Telegram\n\n"
            await bot.send_message(chat_id=chat_id, text=prefix + chunk, **reply_kw)

    await bot.send_message(
        chat_id=chat_id,
        text=(
            "Перевірте текст вище.\n"
            "• Натисніть «Прийняти текст пакета» — перейдемо до етапу 7 (заставка).\n"
            "• Або одним повідомленням надішліть виправлений текст для Telegram "
            "(замість поточного `full_package_plain` у збереженому бандлі)."
        ),
        reply_markup=seo_review_keyboard(),
        **reply_kw,
    )


async def _run_gemini_once_and_enter_review(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    anchor: Message,
) -> int:
    ud = context.user_data
    if ud.get("_gemini_generating"):
        if update.callback_query:
            await update.callback_query.answer("Генерація вже триває…", show_alert=False)
        return PLAYLISTS_STATE

    ud["_gemini_generating"] = True
    bot = context.bot
    rk = _message_reply_kwargs(anchor)
    cid = anchor.chat_id

    try:
        await bot.send_chat_action(chat_id=cid, action=ChatAction.TYPING, **rk)

        if ud.get("gemini_called") and isinstance(ud.get("last_seo_bundle"), dict):
            bundle = ud["last_seo_bundle"]
        else:
            if ud.get("gemini_called"):
                await bot.send_message(
                    chat_id=cid,
                    text=(
                        "Помилка стану майстра: Gemini вже був викликаний, але бандла немає. "
                        "/cancel і /new."
                    ),
                    **rk,
                )
                return ConversationHandler.END
            inp = seo_bundle.generation_input_from_user_data(ud)
            bundle = await gemini_generate.generate_bundle_with_gemini_async(inp)
            ud["last_seo_bundle"] = bundle
            ud["gemini_called"] = True

        seo_bundle.ensure_bundle_skeleton(bundle)

        ud[WIZARD_STEP_KEY] = "await_seo_review"
        await _send_seo_generated_messages(bot, chat_id=cid, reply_kw=rk, bundle=bundle)
        return SEO_REVIEW_STATE

    except Exception as exc:
        log.exception(
            "Помилка Gemini user=%s", update.effective_user.id if update.effective_user else "?"
        )
        await bot.send_message(
            chat_id=cid,
            text=(
                "Не вдалося згенерувати SEO через Gemini.\n\n"
                f"{exc}\n\n"
                "Перевірте GEMINI_API_KEY, квоту та мережу.\n/cancel потім /new — знову буде лише один виклик Gemini."
            ),
            **rk,
        )
        return ConversationHandler.END
    finally:
        ud.pop("_gemini_generating", None)


def _now_kyiv() -> datetime:
    return datetime.now(KYIV_TZ)


def _combine_kyiv(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=KYIV_TZ)


def _preset_start_datetime(preset_key: str) -> datetime | None:
    mapping = dict(TIME_PRESETS)
    if preset_key not in mapping:
        return None
    today = _now_kyiv().astimezone(KYIV_TZ).date()
    tomorrow = today + timedelta(days=1)
    if preset_key.startswith("today_"):
        d = today
        suf = preset_key[len("today_") :]
    elif preset_key.startswith("tomorrow_"):
        d = tomorrow
        suf = preset_key[len("tomorrow_") :]
    else:
        return None
    if suf == "18":
        return _combine_kyiv(d, 18, 0)
    if suf == "21":
        return _combine_kyiv(d, 21, 0)
    return None


def _parse_manual_start_time(text: str) -> datetime | None:
    s = text.strip()
    fmts = (
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y %H.%M",
        "%Y-%m-%d %H:%M",
    )
    for fmt in fmts:
        try:
            naive = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=KYIV_TZ)
    return None


async def _clear_time_prompt_keyboard(
    bot: object,
    chat_id: int | None,
    message_id: int | None,
) -> None:
    if chat_id is None or message_id is None:
        return
    empty = InlineKeyboardMarkup([])
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=empty)
    except Exception:
        pass


async def finalize_scheduled_time(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    dt: datetime,
    *,
    via_callback_query: bool,
) -> int:
    now = _now_kyiv()
    dt_a = dt.astimezone(KYIV_TZ)
    if dt_a <= now:
        hint = (
            "Цей момент часу уже минув (за Києвом). "
            "Оберіть іншу опцію кнопкою або відправте дату у майбутньому, "
            "наприклад 15.06.2026 19:30"
        )
        if via_callback_query and update.callback_query:
            await update.callback_query.answer(text=hint, show_alert=True)
            return TIME_STATE
        if update.message:
            await update.message.reply_text(hint)
        return TIME_STATE

    iso = dt_a.isoformat(timespec="seconds")
    tz_name = dt_a.tzname() or "Київ"
    readable = dt_a.strftime("%d.%m.%Y %H:%M") + f" ({tz_name})"

    context.user_data["timezone"] = TIMEZONE_CANONICAL
    context.user_data["scheduled_start_time"] = iso
    context.user_data.pop(WIZARD_STEP_KEY, None)
    mid = context.user_data.pop("_time_prompt_msg_id", None)
    cid = context.user_data.pop("_time_prompt_chat_id", None)

    log.info(
        "Задано початок ефіру %s (%s), user=%s",
        iso,
        readable,
        update.effective_user.id if update.effective_user else "?",
    )

    summary = (
        f"Етап 5 ✓ Час початку ефіру — за Києвом ({TIMEZONE_CANONICAL}):\n"
        f"{readable}\n"
        f"ISO: {iso}\n\n"
        "Далі завантаження плейлистів із каналу (YouTube API) — у наступному повідомленні бота."
    )
    cleared = InlineKeyboardMarkup([])

    anchor_msg: Message | None = None
    if via_callback_query and update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(summary, reply_markup=cleared)
        anchor_msg = update.callback_query.message
    else:
        await _clear_time_prompt_keyboard(context.bot, cid, mid)
        if update.message:
            anchor_msg = update.message
            await update.message.reply_text(summary)

    if anchor_msg is None:
        log.warning("Немає якоря для кроку плейлистів.")
        context.user_data.pop(WIZARD_STEP_KEY, None)
        return ConversationHandler.END

    return await _present_youtube_playlists_step(anchor_msg, context)


async def _present_youtube_playlists_step(
    anchor_msg: Message,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    context.user_data[WIZARD_STEP_KEY] = "await_playlists"
    context.user_data.pop("playlist_indices", None)
    context.user_data["playlist_indices"] = set()

    try:
        rows = await asyncio.to_thread(youtube_playlists.fetch_my_playlists)
    except youtube_playlists.YouTubePlaylistError as exc:
        log.warning("YouTube playlists: %s", exc)
        await anchor_msg.reply_text(
            "Етап 6: не вдалося підтягнути плейлісти через YouTube API.\n\n"
            f"{exc}\n\n"
            "Перевірте token.json, client_secret*.json у корені та scope (потрібен доступ на читання плейлистів, "
            "наприклад youtube.readonly). Порожній масив плейлистів збережено."
        )
        context.user_data["playlists"] = []
        context.user_data.pop(WIZARD_STEP_KEY, None)
        context.user_data.pop("_youtube_playlists_cache", None)
        return ConversationHandler.END

    if not rows:
        await anchor_msg.reply_text(
            "Етап 6: для цього Google-акаунта немає доступних плейлистів (порожній список)."
        )
        context.user_data["playlists"] = []
        context.user_data.pop(WIZARD_STEP_KEY, None)
        context.user_data.pop("_youtube_playlists_cache", None)
        return ConversationHandler.END

    context.user_data["_youtube_playlists_cache"] = rows
    await anchor_msg.reply_text(
        "Етап 6: плейлисти вашого каналу (YouTube Data API).\n\n"
        "Позначте один або кілька (✓), потім «Готово». «Пропустити» — передати в Gemini порожній список плейлистів.",
        reply_markup=playlists_choice_keyboard(rows, set()),
    )
    return PLAYLISTS_STATE


def _decode_file_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _truncate_for_preview(text: str) -> str:
    t = text.strip()
    if len(t) <= PREVIEW_LIMIT:
        return t
    return t[: PREVIEW_LIMIT] + "\n…"

HELP_TEXT_GROUPS = """Привіт! Я допомагаю зібрати SEO-пакет під YouTube-ефір.

ЩО РОБИТИ У ГРУПІ (важливо):

1. Спочатку надішліть команду /new (можна також /draft або /seo).
2. ПІСЛЯ відповіді бота відправте чорновик текстом або .txt файлом у тому ж чаті.

/start лише показує цю пам’ятку й НЕ включає прийом матеріалу. Якщо ви просто вставите текст без /new перед цим — бот його не зберігає за поточним сценарієм.

/cancel — скасувати активний «майстер» і почати заново через /new.

Окремо від майстра: /timestamps (або /time, /toc, /timecodes, /chapters) — модель Gemini читає субтитри відео; після перевірки бот може записати блок тайм-кодів у YouTube-опис (перед першим рядком з гештегами #…). У Studio мають бути субтитри або автогенерація; доступ той самий OAuth (`token.json`), що для плейлистів/ефірів.

"""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(HELP_TEXT_GROUPS)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Дублікат /start для тих хто звик до /help."""
    await cmd_start(update, context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.message:
        await update.message.reply_text("Сценарій скасовано. Можете почати знову: /new")
    return ConversationHandler.END


async def cmd_start_in_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text(
            "Ви вже запустили /new — надішліть чорновик одним повідомленням або /cancel щоб почати заново.",
        )
    return CONTENT_STATE


async def cmd_help_waiting_draft(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Підказка в середині сценарію після /new — не завершує діалог."""
    if update.message:
        await update.message.reply_text(
            HELP_TEXT_GROUPS
            + "\n\nЗараз очікую чорновик ефіру (текст або .txt)."
        )
    return CONTENT_STATE


async def cmd_start_in_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text(
            "Зараз потрібно обрати мову SEO однією з кнопок під попереднім повідомленням бота або /cancel."
        )
    return LANGUAGE_STATE


async def cmd_help_waiting_language(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.message:
        await update.message.reply_text(
            "Етап 2: мова SEO (title, description, tags).\n\n"
            "Натисніть кнопку під попереднім повідомленням бота (🇺🇦 / 🇷🇺 / 🇬🇧) або скасуйте /cancel.",
        )
    return LANGUAGE_STATE


async def refuse_text_expect_language_buttons(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.message:
        await update.message.reply_text(
            "Чернетку вже прийнято. Оберіть мову SEO кнопками під попереднім повідомленням бота або /cancel."
        )
    return LANGUAGE_STATE


async def refuse_document_in_language_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.message:
        await update.message.reply_text(
            "Файл вже не потрібен — чорновик збережено. Оберіть мову кнопками або /cancel."
        )
    return LANGUAGE_STATE


async def cmd_start_in_speakers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text(
            "Зараз потрібно обрати спікерів кнопками у повідомленні бота (етап 3) або /cancel."
        )
    return SPEAKERS_STATE


async def cmd_help_waiting_speakers(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.message:
        await update.message.reply_text(
            "Етап 3: спікери з `speakers.csv`.\n\n"
            "Натисніть на імʼя, щоб позначити або зняти позначку (✓). "
            "Натисніть «Готово», коли виберете всіх потрібних. /cancel — скасувати весь сценарій."
        )
    return SPEAKERS_STATE


async def refuse_text_in_speakers_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.message:
        await update.message.reply_text(
            "На цьому кроці використовуйте лише кнопки спікерів та «Готово» або /cancel."
        )
    return SPEAKERS_STATE


async def refuse_document_in_speakers_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.message:
        await update.message.reply_text(
            "Файл не потрібен — оберіть спікерів кнопками або /cancel."
        )
    return SPEAKERS_STATE


async def cmd_start_in_style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text(
            "Зараз оберіть стиль SEO однією з кнопок у повідомленні бота (етап 4) або /cancel.",
        )
    return STYLE_STATE


async def cmd_help_waiting_style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        labs = "\n".join(f"• {lab}" for _, lab in STYLE_CHOICES)
        await update.message.reply_text(
            "Етап 4 — тон текстів для YouTube SEO.\n\n"
            f"Доступні пресети:\n{labs}\n\n"
            "Оберіть кнопкою під повідомленням бота або /cancel.",
        )
    return STYLE_STATE


async def refuse_text_in_style_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.message:
        await update.message.reply_text(
            "На цьому кроці потрібна лише кнопка зі стилем або /cancel."
        )
    return STYLE_STATE


async def refuse_document_in_style_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.message:
        await update.message.reply_text(
            "Файл не потрібен — оберіть стиль кнопкою або /cancel."
        )
    return STYLE_STATE


async def cmd_start_in_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text(
            "Зараз вкажіть час початку ефіру: кнопки для швидкого вибору або "
            "текстом у форматі ДД.ММ.РРРР ГГ:ХХ (завжди за Києвом). /cancel щоб скинути.",
        )
    return TIME_STATE


async def cmd_help_waiting_time(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.message:
        await update.message.reply_text(
            "Етап 5 — час старту ефіру.\n\n"
            "Часова зона завжди Europe/Kyiv (київський час).\n"
            "Є швидкі кнопки (сьогодні/завтра 18:00 та 21:00) або введіть дату текстом:\n"
            "15.06.2026 19:45 або 2026-06-15 19:45.\n\n"
            "/cancel — почати заново з /new."
        )
    return TIME_STATE


async def refuse_document_in_time_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.message:
        await update.message.reply_text(
            "Файл не потрібен — лише кнопки або дата текстом форматом ДД.ММ.РРРР ГГ:ХХ, /cancel."
        )
    return TIME_STATE


async def receive_time_preset_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    q = update.callback_query
    if not q or not q.data:
        return TIME_STATE
    parts = q.data.split(":")
    if len(parts) != 3 or parts[0] != "time" or parts[1] != "preset":
        await q.answer()
        return TIME_STATE

    preset_key = parts[2]
    dt = _preset_start_datetime(preset_key)
    if dt is None:
        await q.answer(text="Невідомий пресет", show_alert=True)
        return TIME_STATE

    return await finalize_scheduled_time(
        update, context, dt, via_callback_query=True
    )


async def receive_time_manual_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not update.message or update.message.text is None:
        return TIME_STATE

    txt = update.message.text.strip()
    dt = _parse_manual_start_time(txt)
    if dt is None:
        await update.message.reply_text(
            "Не зрозумів дату й час.\n\n"
            "Час завжди за Києвом (Europe/Kyiv). Формат, наприклад:\n"
            "• 15.06.2026 19:45\n"
            "• 2026-06-15 19:45\n\n"
            "Або скористайтесь швидкими кнопками у попередньому повідомленні бота."
        )
        return TIME_STATE

    return await finalize_scheduled_time(
        update, context, dt, via_callback_query=False
    )


async def cmd_start_in_playlists(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text(
            "На цьому кроці лише кнопки плейлистів YouTube у попередньому повідомленні бота або /cancel."
        )
    return PLAYLISTS_STATE


async def cmd_help_waiting_playlists(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.message:
        await update.message.reply_text(
            "Етап 6: список плейлистів завантажується через YouTube API із вашого авторизованого каналу.\n\n"
            "Відмітьте потрібні плейлісти (✓), натисніть «Готово» або «Пропустити».\n/cancel — скинути весь сценарій."
        )
    return PLAYLISTS_STATE


async def refuse_text_in_playlists_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.message:
        await update.message.reply_text(
            "На цьому етапі оберіть плейлісти кнопками або натисніть «Пропустити»."
        )
    return PLAYLISTS_STATE


async def refuse_document_in_playlists_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.message:
        await update.message.reply_text(
            "Файл не потрібен — лише кнопки плейлистів або /cancel."
        )
    return PLAYLISTS_STATE


async def receive_youtube_playlists_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    q = update.callback_query
    if not q or not q.data:
        return PLAYLISTS_STATE

    data = q.data
    if not data.startswith("ytpl:"):
        await q.answer()
        return PLAYLISTS_STATE

    rest = data[5:]
    rows: list[dict[str, str]] = context.user_data.get("_youtube_playlists_cache") or []

    if rest == "skip":
        await q.answer()
        context.user_data["playlists"] = []
        context.user_data.pop(WIZARD_STEP_KEY, None)
        context.user_data.pop("_youtube_playlists_cache", None)
        context.user_data.pop("playlist_indices", None)
        await q.edit_message_text(
            "Етап 6 ✓ Плейлісти пропущено (порожній список у даних для Gemini).",
            reply_markup=InlineKeyboardMarkup([]),
        )
        log.info(
            "Користувач пропустив плейлісти user=%s",
            update.effective_user.id if update.effective_user else "?",
        )
        if not q.message:
            log.warning("Пропуск плейлистів без q.message.")
            return ConversationHandler.END
        return await _run_gemini_once_and_enter_review(update, context, q.message)

    if not rows:
        await q.answer(text="Список плейлистів недоступний. Спробуйте /new знову.", show_alert=True)
        return ConversationHandler.END

    if rest == "done":
        picked = context.user_data.get("playlist_indices") or set()
        if not picked:
            await q.answer(
                text="Оберіть хоча б один плейліст або натисніть «Пропустити».",
                show_alert=True,
            )
            return PLAYLISTS_STATE
        await q.answer()
        ordered = sorted(picked)
        chosen = [rows[i] for i in ordered if 0 <= i < len(rows)]
        context.user_data["playlists"] = [dict(p) for p in chosen]
        context.user_data.pop(WIZARD_STEP_KEY, None)
        context.user_data.pop("_youtube_playlists_cache", None)
        context.user_data.pop("playlist_indices", None)
        lines = "\n".join(f"• {p['title']}" for p in chosen)
        if len(lines) > 3500:
            lines = lines[:3497] + "…"
        cleared = InlineKeyboardMarkup([])
        await q.edit_message_text(
            f"Етап 6 ✓ Плейлісти ({len(chosen)}):\n{lines}\n\n"
            "Далі: один виклик Gemini і перегляд тексту перед етапом 7.",
            reply_markup=cleared,
        )
        log.info(
            "Обрано плейлістів: %s user=%s",
            len(chosen),
            update.effective_user.id if update.effective_user else "?",
        )
        if not q.message:
            return ConversationHandler.END
        return await _run_gemini_once_and_enter_review(update, context, q.message)

    if rest.startswith("t:"):
        await q.answer()
        try:
            idx = int(rest[2:])
        except ValueError:
            return PLAYLISTS_STATE
        if idx < 0 or idx >= len(rows):
            return PLAYLISTS_STATE
        bag = context.user_data.setdefault("playlist_indices", set())
        if idx in bag:
            bag.remove(idx)
        else:
            bag.add(idx)
        await q.edit_message_reply_markup(
            reply_markup=playlists_choice_keyboard(rows, bag),
        )
        return PLAYLISTS_STATE

    await q.answer()
    return PLAYLISTS_STATE


async def cmd_start_in_seo_review(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.message:
        await update.message.reply_text(
            "Зараз переглядаєте згенерований текст. Натисніть «Прийняти текст пакета» під повідомленням "
            "з кнопкою або надішліть виправлення одним текстовим повідомленням. /cancel — скасувати."
        )
    return SEO_REVIEW_STATE


async def cmd_help_waiting_seo_review(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.message:
        await update.message.reply_text(
            "Після Gemini: один текст згенеровано.\n\n"
            "«Прийняти текст пакета» або надішліть повний замінений текст для Telegram-пакета одним повідомленням. "
            "Файли на цьому кроці не потрібні. /cancel — скасувати."
        )
    return SEO_REVIEW_STATE


async def refuse_document_in_seo_review_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.message:
        await update.message.reply_text(
            "На кроці перегляду надішліть лише текст правок або натисніть «Прийняти текст пакета». /cancel — скасувати."
        )
    return SEO_REVIEW_STATE


async def receive_seo_review_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    q = update.callback_query
    if not q or not q.data:
        return SEO_REVIEW_STATE
    if q.data != "seo:accept":
        await q.answer()
        return SEO_REVIEW_STATE

    ud = context.user_data
    if not isinstance(ud.get("last_seo_bundle"), dict):
        await q.answer(text="Немає збереженого бандла. /cancel та /new.", show_alert=True)
        return SEO_REVIEW_STATE

    await q.answer()
    rk = _message_reply_kwargs(q.message)
    try:
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([]))
    except Exception:
        pass

    ud[WIZARD_STEP_KEY] = "await_cover"
    ud.pop("splash_skipped", None)

    cid = q.message.chat_id if q.message else update.effective_chat.id
    bot = context.bot

    await bot.send_message(
        chat_id=cid,
        text=(
            "Етап 7 — заставка ефіру.\n\n"
            "Надішліть зображення як фото або документом (JPEG, PNG, WebP).\n"
            "Або «Без заставки», якщо зараз нічого додавати не потрібно."
        ),
        reply_markup=cover_step_keyboard(),
        **rk,
    )
    return COVER_STATE


async def receive_seo_review_edit_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not update.message or update.message.text is None:
        return SEO_REVIEW_STATE
    txt = update.message.text.strip()
    if not txt:
        await update.message.reply_text(
            "Надішліть повний текст пакета одним повідомленням або натисніть «Прийняти текст пакета», якщо правок не потрібно.",
        )
        return SEO_REVIEW_STATE
    ud = context.user_data
    b = ud.get("last_seo_bundle")
    if not isinstance(b, dict):
        await update.message.reply_text(
            "Спочатку має бути результат Gemini з цього /new."
        )
        return SEO_REVIEW_STATE
    seo_bundle.ensure_bundle_skeleton(b)
    tg = b.setdefault("telegram", {})
    if not isinstance(tg, dict):
        b["telegram"] = {"full_package_plain": txt}
    else:
        tg["full_package_plain"] = txt

    rk = _message_reply_kwargs(update.message)
    await update.message.reply_text(
        "Текст пакета для Telegram збережено з вашими правками.\nНатисніть «Прийняти текст пакета», коли все готово.",
        reply_markup=seo_review_keyboard(),
        **rk,
    )
    return SEO_REVIEW_STATE


async def cmd_start_in_cover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text(
            "Етап 7 — надішліть заставку фото або зображенням-файлом, або натисніть «Без заставки» під попереднім повідомленням. /cancel — скасувати."
        )
    return COVER_STATE


async def cmd_help_waiting_cover(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.message:
        await update.message.reply_text(
            "Етап 7: файл заставки (JPEG / PNG / WebP) або «Без заставки». /cancel — скасувати."
        )
    return COVER_STATE


async def refuse_text_in_cover_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if update.message:
        await update.message.reply_text(
            "На етапі 7 очікується зображення (фото або файл-картинка) або кнопка «Без заставки». /cancel — скасувати."
        )
    return COVER_STATE


def _wizard_meta_snapshot(ud: dict) -> dict[str, object]:
    keys = (
        "locale",
        "style",
        "scheduled_start_time",
        "timezone",
        "draft_source",
        "draft_filename",
        "splash_kind",
        "splash_filename",
        "splash_skipped",
        "gemini_called",
    )
    meta: dict[str, object] = {k: ud[k] for k in keys if k in ud}
    pl = ud.get("playlists")
    if isinstance(pl, list):
        meta["playlists_saved"] = len(pl)
    return meta


async def _finalize_session_disk_and_youtube(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    reply_anchor: Message | None,
) -> None:
    bot = context.bot
    ud = context.user_data
    chat = update.effective_chat
    user = update.effective_user
    rk = _message_reply_kwargs(reply_anchor)

    if chat is None:
        log.warning("_finalize_session_disk_and_youtube: немає effective_chat.")
        return

    bundle = ud.get("last_seo_bundle")
    if not isinstance(bundle, dict):
        await bot.send_message(
            chat_id=chat.id,
            text="Не вдалося завершити: немає збереженого SEO-бандла. Спробуйте знову /new.",
            **rk,
        )
        return

    seo_bundle.ensure_bundle_skeleton(bundle)

    yt_blk = bundle.get("youtube") or {}
    title_hint = str((yt_blk.get("title") if isinstance(yt_blk, dict) else None) or "live")

    session_dir = session_output.new_session_directory(
        chat_id=chat.id,
        user_id=user.id if user else None,
        title_hint=title_hint,
    )
    ud["last_session_dir"] = str(session_dir.resolve())

    try:
        ginp = seo_bundle.generation_input_from_user_data(ud)
        session_output.dump_json(session_dir / "generation_input.json", ginp)
    except Exception as exc:
        log.warning("generation_input snapshot: %s", exc)
        session_output.dump_json(session_dir / "generation_input.error.json", {"error": str(exc)})
        ginp = None

    if ginp is not None:
        try:
            seo_bundle.ensure_publish_standard_blocks(bundle, ginp)
            bundle = seo_bundle.apply_youtube_hard_limits(
                bundle, ginp, trim_description=False
            )
            ud["last_seo_bundle"] = bundle
        except Exception as exc:
            log.warning("Доповнення опису (каталог + футер) перед YouTube: %s", exc)

    session_output.dump_json(session_dir / "bundle_final.json", bundle)
    session_output.dump_json(session_dir / "wizard_meta.json", _wizard_meta_snapshot(ud))

    thumb_path: Path | None = None
    fid = ud.get("splash_file_id")
    if isinstance(fid, str) and fid.strip():
        fn = str(ud.get("splash_filename") or "")
        suf = Path(fn).suffix.lower() if fn else ""
        if suf not in {".jpg", ".jpeg", ".png", ".webp"}:
            suf = ".jpg"
        splash_dest = session_dir / f"splash{suf}"

        try:
            tg_file = await bot.get_file(fid)
            blob = await tg_file.download_as_bytearray()
            splash_dest.write_bytes(bytes(blob))
            thumb_path = splash_dest
            session_output.dump_json(
                session_dir / "splash_telegram.json",
                {"file_id": fid.strip(), "kind": ud.get("splash_kind"), "filename": fn or None},
            )
        except Exception as exc:
            log.warning("Завантаження заставки з Telegram не вдалося: %s", exc)
            session_output.dump_json(session_dir / "splash_download.error.json", {"error": str(exc)})
            thumb_path = None

    await bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING, **rk)

    try:
        live_result = await asyncio.to_thread(
            youtube_live.create_scheduled_broadcast,
            bundle=bundle,
            session_dir=session_dir,
            thumbnail_path=thumb_path if thumb_path and thumb_path.is_file() else None,
        )
    except youtube_live.YoutubeLiveError as exc:
        log.warning("Створення YouTube Live не вдалося: %s", exc)
        await bot.send_message(
            chat_id=chat.id,
            text=(
                "Локально збережено артефакти майстра.\n\n"
                f"{session_dir.resolve()}\n\n"
                "Створити трансляцію у YouTube не вдалося (деталі в логах / повідомленні нижче):\n\n"
                f"{str(exc)[:3500]}"
            ),
            **rk,
        )
        return

    root = session_dir.resolve()
    watch = str(live_result.get("watch_url") or "").strip()
    studio = str(live_result.get("studio_url") or "").strip()
    rtmp_srv = str(((live_result.get("ingest") or {}).get("rtmp_server")) or "").strip()

    msg_lines = [
        "Етап 8 ✓ Локально збережено в output і створено запланований ефір (RTMP / streaming software).",
        "",
        f"Папка сесії: {root}",
        "",
        f"Перегляд: {watch or '—'}",
        f"Studio: {studio or '—'}",
        "",
    ]

    if live_result.get("reused_existing_live_stream_from_stream_key_txt"):
        msg_lines.append(
            "Потік зіставлено з вашим stream_key.txt (існуючий liveStream на каналі, без liveStreams.insert)."
        )
    elif live_result.get("bind_conflict_fallback_created_new_stream"):
        msg_lines.append(
            "Ключ із stream_key.txt не вдалося прив'язати (потік зайнятий) — створено новий liveStream; "
            "ключ у папці сесії."
        )

    msg_lines.extend(
        [
            "RTMP (vMix Streaming Destination / OBS Custom):",
            f"Адреса ingestion: {rtmp_srv or '—'}",
            "Повний stream key у файлі obs_stream_key.txt у папці сесії (не розголошувати).",
        ]
    )

    warns = live_result.get("warnings")
    if isinstance(warns, list) and warns:
        msg_lines.append("")
        msg_lines.append("Увага (частину налаштувань YouTube могло відхилити API):")
        for w in warns[:8]:
            msg_lines.append("• " + str(w)[:900])

    text = "\n".join(msg_lines)
    if len(text) > 4090:
        text = text[:4086] + "…"

    await bot.send_message(chat_id=chat.id, text=text, **rk)


async def receive_cover_skip_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    q = update.callback_query
    if not q or q.data != "cov:skip":
        if q:
            await q.answer()
        return COVER_STATE
    await q.answer()
    try:
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([]))
    except Exception:
        pass

    ud = context.user_data
    ud.pop("splash_file_id", None)
    ud["splash_skipped"] = True
    ud.pop(WIZARD_STEP_KEY, None)

    await _finalize_session_disk_and_youtube(update, context, reply_anchor=q.message)
    return ConversationHandler.END


async def receive_cover_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    msg = update.message
    if not msg or not msg.photo:
        return COVER_STATE

    rk = _message_reply_kwargs(msg)
    best = max(msg.photo, key=lambda p: (p.width * p.height, p.file_size or 0))
    if best.file_size and best.file_size > MAX_COVER_FILE_BYTES:
        await msg.reply_text(
            f"Файл завеликий (>{MAX_COVER_FILE_BYTES // (1024 * 1024)} МБ). Спробуйте менший знімок.",
            **rk,
        )
        return COVER_STATE

    ud = context.user_data
    ud.pop("splash_skipped", None)
    ud["splash_file_id"] = best.file_id
    ud["splash_kind"] = "photo"
    ud.pop(WIZARD_STEP_KEY, None)

    log.info(
        "Заставка (photo): user=%s file_id_suffix=%s",
        update.effective_user.id if update.effective_user else "?",
        (best.file_id[-8:] if best.file_id else ""),
    )

    await _finalize_session_disk_and_youtube(update, context, reply_anchor=msg)
    return ConversationHandler.END


async def receive_cover_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    msg = update.message
    if not msg or not msg.document:
        return COVER_STATE

    doc = msg.document
    rk = _message_reply_kwargs(msg)

    if not _looks_like_cover_image_document(doc):
        await msg.reply_text(
            "Очікується зображення: JPEG, PNG або WebP (файлом або як звичайне фото).",
            **rk,
        )
        return COVER_STATE

    if doc.file_size and doc.file_size > MAX_COVER_FILE_BYTES:
        await msg.reply_text(
            f"Файл завеликий (>{MAX_COVER_FILE_BYTES // (1024 * 1024)} МБ).",
            **rk,
        )
        return COVER_STATE

    ud = context.user_data
    ud.pop("splash_skipped", None)
    ud["splash_file_id"] = doc.file_id
    ud["splash_kind"] = "document"
    if doc.file_name:
        ud["splash_filename"] = doc.file_name
    ud.pop(WIZARD_STEP_KEY, None)

    log.info(
        "Заставка (document): user=%s mime=%s",
        update.effective_user.id if update.effective_user else "?",
        doc.mime_type or "",
    )

    await _finalize_session_disk_and_youtube(update, context, reply_anchor=msg)
    return ConversationHandler.END


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data[WIZARD_STEP_KEY] = "await_draft"
    if update.effective_user and update.effective_chat:
        log.info(
            "Почато /new: user=%s chat=%s",
            update.effective_user.id,
            update.effective_chat.id,
        )
    if update.message:
        await update.message.reply_text(
            "Надішліть чорновик ефіру одним повідомленням або .txt файлом.\n"
            "/cancel — скасувати цей крок.\n/help — коротка пам’ятка.",
        )
    return CONTENT_STATE


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or update.message.text is None:
        return CONTENT_STATE

    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Текст порожній — надішліть зміст ефіру або /cancel.")
        return CONTENT_STATE

    context.user_data["draft_raw"] = text
    context.user_data["draft_source"] = "text"
    context.user_data.pop("draft_filename", None)

    preview = _truncate_for_preview(text)
    log.info(
        "Прийнято чорновик текстом: %s символів, user=%s chat=%s",
        len(text),
        update.effective_user.id if update.effective_user else "?",
        update.effective_chat.id if update.effective_chat else "?",
    )
    await update.message.reply_text(
        "Етап 1 ✓ Чернетку прийнято (текст).\n\n"
        f"Прев’ю ({len(text)} симв.):\n---\n{preview}\n---",
    )
    await update.message.reply_text(
        "Етап 2: оберіть мову для SEO-пакета (title, опис, tags).",
        reply_markup=language_choice_keyboard(),
    )
    context.user_data[WIZARD_STEP_KEY] = "await_language"
    context.user_data[SKIP_LONG_TEXT_NUDGE_UPDATE_ID] = update.update_id
    return LANGUAGE_STATE


async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.document:
        return CONTENT_STATE

    doc = update.message.document
    if doc.file_size and doc.file_size > MAX_TEXT_FILE_BYTES:
        await update.message.reply_text(
            f"Файл завеликий (> {MAX_TEXT_FILE_BYTES // (1024 * 1024)} МБ). "
            "Надішліть менший .txt або вставте текст у повідомлення."
        )
        return CONTENT_STATE

    name = doc.file_name or ""
    mime = doc.mime_type or ""
    ok_name = name.lower().endswith(".txt")
    ok_mime = mime in ("text/plain", "application/octet-stream") or mime.startswith("text/")
    if not (ok_name or ok_mime):
        await update.message.reply_text(
            "Очікується текстовий файл `.txt` (або текст у повідомленні)."
        )
        return CONTENT_STATE

    await update.message.chat.send_action(ChatAction.TYPING)

    tg_file = await context.bot.get_file(doc.file_id)
    blob = await tg_file.download_as_bytearray()
    text = _decode_file_bytes(bytes(blob)).strip()

    if not text:
        await update.message.reply_text(
            "У файлі не знайдено тексту — перевірте кодування (UTF-8) або надішліть текст повідомленням."
        )
        return CONTENT_STATE

    context.user_data["draft_raw"] = text
    context.user_data["draft_source"] = "file"
    context.user_data["draft_filename"] = name or "(без назви)"

    preview = _truncate_for_preview(text)
    fname = context.user_data["draft_filename"]
    await update.message.reply_text(
        "Етап 1 ✓ Чернетку прийнято (файл).\n\n"
        f"{fname} — {len(text)} симв.\n\n"
        f"Прев’ю:\n---\n{preview}\n---",
    )
    await update.message.reply_text(
        "Етап 2: оберіть мову для SEO-пакета (title, опис, tags).",
        reply_markup=language_choice_keyboard(),
    )
    context.user_data[WIZARD_STEP_KEY] = "await_language"
    context.user_data[SKIP_LONG_TEXT_NUDGE_UPDATE_ID] = update.update_id
    return LANGUAGE_STATE


async def receive_language_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q or not q.data:
        return LANGUAGE_STATE
    parts = q.data.split(":", 1)
    if len(parts) != 2 or parts[0] != "lang":
        await q.answer()
        return LANGUAGE_STATE
    code = parts[1]
    if code not in SEO_LOCALE_LABELS:
        await q.answer(text="Невідомий варіант", show_alert=True)
        return LANGUAGE_STATE

    await q.answer()

    context.user_data["locale"] = code
    label = SEO_LOCALE_LABELS[code]
    log.info(
        "Обрано SEO-locale=%s user=%s",
        code,
        update.effective_user.id if update.effective_user else "?",
    )
    cleared = InlineKeyboardMarkup([])
    await q.edit_message_text(
        f"Етап 2 ✓ Мова SEO: {label}",
        reply_markup=cleared,
    )

    catalog = list_speakers()
    if not catalog:
        context.user_data["speakers"] = []
        context.user_data[WIZARD_STEP_KEY] = "await_style"
        await q.message.reply_text(
            "У каталозі немає спікерів (перевірте `speakers.csv`). Крок 3 пропущено.",
        )
        await q.message.reply_text(
            "Етап 4: стиль SEO-текстів (title, опис, tags). Оберіть один варіант кнопкою нижче.",
            reply_markup=style_choice_keyboard(),
        )
        return STYLE_STATE

    context.user_data[WIZARD_STEP_KEY] = "await_speakers"
    context.user_data.pop("speaker_indices", None)
    context.user_data["speaker_indices"] = set()
    await q.message.reply_text(
        "Етап 3: хто веде або співведе ефір? Оберіть одного або кількох спікерів.\n\n"
        "Повторне натискання знімає позначку (✓). Потім натисніть «Готово».",
        reply_markup=speakers_choice_keyboard(context.user_data["speaker_indices"]),
    )
    return SPEAKERS_STATE


async def receive_speakers_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    q = update.callback_query
    if not q or not q.data or not q.data.startswith("spk:"):
        return SPEAKERS_STATE

    rest = q.data[4:]
    catalog = list_speakers()
    if not catalog:
        await q.answer()
        context.user_data["speakers"] = []
        context.user_data[WIZARD_STEP_KEY] = "await_style"
        await q.edit_message_text(
            "Каталог спікерів порожній або недоступний — крок 3 пропущено.",
            reply_markup=InlineKeyboardMarkup([]),
        )
        await q.message.reply_text(
            "Етап 4: стиль SEO-текстів (title, опис, tags). Оберіть один варіант кнопкою нижче.",
            reply_markup=style_choice_keyboard(),
        )
        return STYLE_STATE

    if rest == "done":
        picked = context.user_data.get("speaker_indices") or set()
        if not picked:
            await q.answer(text="Оберіть хоча б одного спікера.", show_alert=True)
            return SPEAKERS_STATE
        await q.answer()
        ordered = sorted(picked)
        chosen_entries = [catalog[i] for i in ordered]
        context.user_data["speakers"] = [gemini_speaker_dict(e) for e in chosen_entries]
        log.info(
            "Обрано спікерів: %s осіб user=%s",
            len(chosen_entries),
            update.effective_user.id if update.effective_user else "?",
        )
        lines = "\n".join(f"• {e['display_name']}" for e in chosen_entries)
        if len(lines) > 3500:
            lines = lines[:3497] + "…"
        summary = f"Етап 3 ✓ Спікери ({len(chosen_entries)}):\n{lines}"
        cleared = InlineKeyboardMarkup([])
        await q.edit_message_text(summary, reply_markup=cleared)
        context.user_data[WIZARD_STEP_KEY] = "await_style"
        await q.message.reply_text(
            "Етап 4: стиль SEO-текстів (title, опис, tags). Оберіть один варіант кнопкою нижче.",
            reply_markup=style_choice_keyboard(),
        )
        return STYLE_STATE

    if rest.startswith("t:"):
        await q.answer()
        try:
            idx = int(rest[2:])
        except ValueError:
            return SPEAKERS_STATE
        if idx < 0 or idx >= len(catalog):
            return SPEAKERS_STATE
        bag = context.user_data.setdefault("speaker_indices", set())
        if idx in bag:
            bag.remove(idx)
        else:
            bag.add(idx)
        await q.edit_message_reply_markup(reply_markup=speakers_choice_keyboard(bag))
        return SPEAKERS_STATE

    return SPEAKERS_STATE


async def receive_style_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q or not q.data:
        return STYLE_STATE
    parts = q.data.split(":", 1)
    if len(parts) != 2 or parts[0] != "sty":
        await q.answer()
        return STYLE_STATE
    key = parts[1]
    label = STYLE_LABELS.get(key)
    if label is None:
        await q.answer(text="Невідомий стиль", show_alert=True)
        return STYLE_STATE

    await q.answer()
    context.user_data["style"] = label
    log.info(
        "Обрано SEO-style=%r user=%s",
        label,
        update.effective_user.id if update.effective_user else "?",
    )
    cleared = InlineKeyboardMarkup([])
    await q.edit_message_text(
        f"Етап 4 ✓ Стиль: {label}\n\n"
        "Далі — час старту ефіру (Europe/Kyiv, наступне повідомлення).",
        reply_markup=cleared,
    )

    context.user_data[WIZARD_STEP_KEY] = "await_time"
    time_msg = await q.message.reply_text(
        "Етап 5: коли стартує ефір? Часова зона завжди Київ — Europe/Kyiv.\n\n"
        "Натисніть швидку кнопку або надішліть дату текстом одним із форматів:\n"
        "• 15.06.2026 19:45\n"
        "• 2026-06-15 19:45",
        reply_markup=time_choice_keyboard(),
    )
    context.user_data["_time_prompt_chat_id"] = time_msg.chat_id
    context.user_data["_time_prompt_msg_id"] = time_msg.message_id

    return TIME_STATE


def _timestamps_review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Записати опис на YouTube", callback_data="tsx:yes")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="tsx:no")],
        ]
    )


async def timestamps_conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("tsx_job", None)
    m = update.effective_message
    if m:
        await m.reply_text("Режим тайм-кодів закрито. Далі можна /timestamps або SEO-майстер /new.")
    return ConversationHandler.END


async def cmd_timestamps_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if not msg:
        return ConversationHandler.END
    context.user_data.pop("tsx_job", None)
    await msg.reply_text(
        "Режим тайм-кодів.\n\n"
        "Надішліть посилання на відео з вашого каналу (або лише video id).\n"
        "Потрібні субтитри на сторінці YouTube; мова заголовків тайм-кодів відповідає мові субтитрів.\n\n"
        "/cancel — вийти."
    )
    return TIMESTAMP_STATE_WAIT_URL


async def timestamps_receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if not msg or msg.text is None:
        return TIMESTAMP_STATE_WAIT_URL
    raw = msg.text.strip()
    if not youtube_timecodes.extract_video_id(raw):
        await msg.reply_text(
            "Не знайдено id відео. Спробуйте посилання youtube.com, youtu.be, shorts або сам id."
        )
        return TIMESTAMP_STATE_WAIT_URL

    await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
    try:
        result = await asyncio.to_thread(youtube_timecodes.run_timecodes_preview, raw)
    except youtube_timecodes.TimecodesPipelineError as exc:
        log.warning("timestamps preview: %s", exc)
        await msg.reply_text(f"Не вдалося підготувати тайм-коди:\n\n{str(exc)[:3500]}")
        return TIMESTAMP_STATE_WAIT_URL

    vid = result["video_id"]
    title = str(result["title"] or "").strip()
    tlang = result["transcript_lang"]
    merged = result["new_description"]
    lines = result["timecode_lines"]
    snippet = "\n".join(lines[: min(40, len(lines))])

    context.user_data["tsx_job"] = {"video_id": vid, "new_description": merged, "title": title}

    text = (
        f"📺 {title}\nvideo id: {vid} • субтитри: {tlang}\n\n"
        f"Тайм-коди:\n{snippet}"
    )
    if len(lines) > 40:
        text += f"\n… усього рядків: {len(lines)}"
    text += (
        "\n\nНижче — файл із повним текстом опису для YouTube. Перевірте й натисніть кнопку на цьому повідомленні."
    )

    await msg.reply_text(text, reply_markup=_timestamps_review_keyboard())
    bio = BytesIO(merged.encode("utf-8"))
    bio.name = f"youtube_desc_{vid}.txt"
    await msg.reply_document(document=bio, filename=bio.name)
    log.info(
        "Timestamps: preview ready video=%s lang=%s user=%s",
        vid,
        tlang,
        msg.from_user.id if msg.from_user else None,
    )
    return TIMESTAMP_STATE_CONFIRM


async def timestamps_text_while_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message:
        await update.effective_message.reply_text(
            "Натисніть кнопку під повідомленням з тайм-кодами або надішліть /cancel."
        )
    return TIMESTAMP_STATE_CONFIRM


async def timestamps_on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q or not q.message or not q.data:
        return TIMESTAMP_STATE_CONFIRM
    await q.answer()
    job = context.user_data.pop("tsx_job", None)

    async def clear_kb() -> None:
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    if q.data == "tsx:no" or not isinstance(job, dict):
        await clear_kb()
        await q.message.reply_text("Опис на YouTube не змінювався.")
        return ConversationHandler.END

    vid = str(job.get("video_id") or "")
    new_desc = str(job.get("new_description") or "")
    if not vid or not new_desc:
        await clear_kb()
        await q.message.reply_text("Помилка стану. Почніть з /timestamps.")
        return ConversationHandler.END

    await context.bot.send_chat_action(chat_id=q.message.chat_id, action=ChatAction.TYPING)
    try:
        await asyncio.to_thread(youtube_timecodes.push_timecodes_to_youtube, vid, new_desc)
    except youtube_timecodes.TimecodesPipelineError as exc:
        await clear_kb()
        await q.message.reply_text(f"YouTube не оновлено:\n\n{str(exc)[:3500]}")
        return ConversationHandler.END

    await clear_kb()
    await q.message.reply_text(
        f"Опис відео з id {vid} на YouTube оновлено (тайм-коди вставлено перед першим рядком із #, якщо такий був)."
    )
    return ConversationHandler.END


async def post_init(application: Application) -> None:
    """Діагностика: який саме бот отримує оновлення; чи не «зажався» webhook."""
    me = await application.bot.get_me()
    wh = await application.bot.get_webhook_info()
    log.info(
        "Підключено до Telegram як @%s (id=%s). У групі має бути саме цей бот; токен у .env — лише його.",
        me.username,
        me.id,
    )
    if wh.url:
        log.warning(
            "Webhook на боці Telegram зараз активний: url=%r, pending_updates=%s. "
            "Для long polling він має бути скинутий — якщо після запуску url не порожній, шукайте інший процес з тим же токеном.",
            wh.url,
            wh.pending_update_count,
        )
    else:
        log.info(
            "Webhook порожній (ок для polling). Сервер тримає pending_updates=%s",
            wh.pending_update_count,
        )


async def debug_incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    ch = update.effective_chat
    us = update.effective_user
    head = ""
    if msg:
        head = (msg.text or msg.caption or "")[:200]
    log.info(
        "[incoming] update_id=%s chat_id=%s chat_type=%s user_id=%s msg_id=%s head=%r",
        update.update_id,
        ch.id if ch else None,
        getattr(ch, "type", None) if ch else None,
        us.id if us else None,
        msg.message_id if msg else None,
        head,
    )


async def log_handler_errors(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.error:
        log.exception("Помилка під час обробки оновлення Telegram", exc_info=context.error)


MIN_LEN_NUDGE_IF_CHATS_CONFIGURED = 120
MIN_LEN_NUDGE_FALLBACK_GROUPS = 500


async def nudge_use_new_before_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Якщо користувач кидає великий шматок тексту без попереднього /new — жоден handler не відповідає.
    Нагадуємо про два кроки (без Gemini, просто текст).
    """
    skip_for = context.user_data.pop(SKIP_LONG_TEXT_NUDGE_UPDATE_ID, None)
    if skip_for is not None and skip_for == update.update_id:
        return
    if context.user_data.get(WIZARD_STEP_KEY) in (
        "await_language",
        "await_speakers",
        "await_style",
        "await_time",
        "await_playlists",
        "await_seo_review",
        "await_cover",
    ):
        return

    msg = update.effective_message
    ch = update.effective_chat
    if not msg or not ch or msg.text is None:
        return
    text = msg.text.strip()
    if text.startswith("/"):
        return

    tracked = settings.telegram_watch_chat_ids()
    if tracked and ch.id not in tracked:
        return
    min_len = MIN_LEN_NUDGE_IF_CHATS_CONFIGURED if tracked else MIN_LEN_NUDGE_FALLBACK_GROUPS
    if len(text) < min_len:
        return

    log.info(
        "Підказка /new для довгої розмовної розсилки: chat=%s user=%s len=%s",
        ch.id,
        update.effective_user.id if update.effective_user else None,
        len(text),
    )
    await msg.reply_text(
        "Спочатку команда /new (або /draft або /seo), а вже наступним повідомленням — чорновик. "
        "Без /new бот бачить текст, але не зберігає його у поточному сценарії.\n\n"
        "/cancel — скинути активний майстер."
    )


def main() -> None:
    settings.telegram_bot_token()

    conv = ConversationHandler(
        entry_points=[CommandHandler(["new", "draft", "seo"], cmd_new)],
        states={
            CONTENT_STATE: [
                CommandHandler("start", cmd_start_in_conv),
                CommandHandler("help", cmd_help_waiting_draft),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
                MessageHandler(filters.Document.ALL, receive_document),
            ],
            LANGUAGE_STATE: [
                CommandHandler("start", cmd_start_in_language),
                CommandHandler("help", cmd_help_waiting_language),
                CallbackQueryHandler(receive_language_choice, pattern=r"^lang:(uk|ru|en)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, refuse_text_expect_language_buttons),
                MessageHandler(filters.Document.ALL, refuse_document_in_language_step),
            ],
            SPEAKERS_STATE: [
                CommandHandler("start", cmd_start_in_speakers),
                CommandHandler("help", cmd_help_waiting_speakers),
                CallbackQueryHandler(receive_speakers_callback, pattern=r"^spk:(t:\d+|done)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, refuse_text_in_speakers_step),
                MessageHandler(filters.Document.ALL, refuse_document_in_speakers_step),
            ],
            STYLE_STATE: [
                CommandHandler("start", cmd_start_in_style),
                CommandHandler("help", cmd_help_waiting_style),
                CallbackQueryHandler(receive_style_choice, pattern=r"^sty:[a-z0-9_]+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, refuse_text_in_style_step),
                MessageHandler(filters.Document.ALL, refuse_document_in_style_step),
            ],
            TIME_STATE: [
                CommandHandler("start", cmd_start_in_time),
                CommandHandler("help", cmd_help_waiting_time),
                CallbackQueryHandler(
                    receive_time_preset_callback,
                    pattern=r"^time:preset:(today_18|today_21|tomorrow_18|tomorrow_21)$",
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_time_manual_text),
                MessageHandler(filters.Document.ALL, refuse_document_in_time_step),
            ],
            PLAYLISTS_STATE: [
                CommandHandler("start", cmd_start_in_playlists),
                CommandHandler("help", cmd_help_waiting_playlists),
                CallbackQueryHandler(
                    receive_youtube_playlists_callback,
                    pattern=r"^ytpl:(t:\d+|done|skip)$",
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, refuse_text_in_playlists_step),
                MessageHandler(filters.Document.ALL, refuse_document_in_playlists_step),
            ],
            SEO_REVIEW_STATE: [
                CommandHandler("start", cmd_start_in_seo_review),
                CommandHandler("help", cmd_help_waiting_seo_review),
                CallbackQueryHandler(receive_seo_review_callback, pattern=r"^seo:accept$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_seo_review_edit_text),
                MessageHandler(filters.Document.ALL, refuse_document_in_seo_review_step),
            ],
            COVER_STATE: [
                CommandHandler("start", cmd_start_in_cover),
                CommandHandler("help", cmd_help_waiting_cover),
                CallbackQueryHandler(receive_cover_skip_callback, pattern=r"^cov:skip$"),
                MessageHandler(filters.PHOTO, receive_cover_photo),
                MessageHandler(filters.Document.ALL, receive_cover_document),
                MessageHandler(filters.TEXT & ~filters.COMMAND, refuse_text_in_cover_step),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="seo_wizard_content",
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )

    timestamps_conv = ConversationHandler(
        entry_points=[
            CommandHandler(["timestamps", "timecodes", "toc", "chapters", "time"], cmd_timestamps_start),
        ],
        states={
            TIMESTAMP_STATE_WAIT_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, timestamps_receive_url),
            ],
            TIMESTAMP_STATE_CONFIRM: [
                CallbackQueryHandler(timestamps_on_confirm, pattern=r"^tsx:(yes|no)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, timestamps_text_while_confirm),
            ],
        },
        fallbacks=[CommandHandler("cancel", timestamps_conv_cancel)],
        name="youtube_timestamps",
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )

    builder = (
        Application.builder()
        .token(settings.telegram_bot_token())
        .request(settings.telegram_httpx_request())
        .post_init(post_init)
    )
    # На Windows із httpx вимикаємо concurrent_updates (як у D:\work\YouTube\enhanced_bot_polling.py).
    # pool_timeout і connection_pool_size задаються в settings.telegram_httpx_request —
    # після `.request(...)` їх неможливо виставити на Application.builder().
    if platform.system() == "Windows":
        builder = builder.concurrent_updates(False)
    else:
        builder = builder.concurrent_updates(True)
    app = builder.build()

    app.add_error_handler(log_handler_errors)
    if settings.telegram_debug_incoming_updates():
        app.add_handler(
            MessageHandler(filters.ALL, debug_incoming_message, block=False),
            group=-1,
        )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(timestamps_conv)
    app.add_handler(conv)

    tracked = settings.telegram_watch_chat_ids()
    if tracked:
        log.info(
            "Підказки «спочатку /new» лише в чатах id=%s "
            "(якщо [incoming] показує інший chat_id — додайте його в TELEGRAM_CHANNEL через кому).",
            tracked,
        )
        chat_scope = filters.Chat(chat_id=list(tracked))
    else:
        log.info(
            "TELEGRAM_CHANNEL порожній — підказка для чорновика без /new у будь-якій групі, "
            "якщо текст ≥ %s символів.",
            MIN_LEN_NUDGE_FALLBACK_GROUPS,
        )
        chat_scope = filters.ChatType.GROUPS

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & chat_scope,
            nudge_use_new_before_draft,
        ),
        group=1,
    )

    log.info(
        "Бот запущено (polling). SEO-майстер: /new … Gemini … заставка. "
        "Окремо: /timestamps — тайм-коди в опис відео вашого каналу (субтитри + Gemini)."
    )
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=settings.telegram_bootstrap_retries(),
    )


if __name__ == "__main__":
    main()
