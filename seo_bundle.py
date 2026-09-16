"""Контракт SEO-пакета: ліміти YouTube, завантаження системного промпта, парсинг і злиття з Gemini."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import speakers_catalog

_ROOT = Path(__file__).resolve().parent
SYSTEM_PROMPT_FILE = _ROOT / "system_promt.txt"

# Насправді YouTube рахує скаляри UTF-16 для title; 100 — безпечне ціле з документації.
YOUTUBE_TITLE_MAX_CHARS = 100
YOUTUBE_DESCRIPTION_MAX_CHARS = 5000
# Документація згадує верхню межу кількості тегів; обмежуємо розумно для моделі.
YOUTUBE_TAGS_SOFT_MAX = 100

# Фіксований хвіст опису (YouTube; telegram.full_package_plain дзеркалить description).
_PUBLISH_SUPPORT_FOOTER_LINES = """--------------------------------------
Підтримайте нас: Patreon: https://www.patreon.com/c/honey_erbe
Telegram: https://t.me/MedoyidClub
Web: https://www.medoyid-club.com
Мерч: https://medoyid.com/futbolkimedoediv
Радіо Медоїд: https://www.medoyid-club.com/uk/radio
Email: partners@medoyid-club.com"""

# Старі варіанти футера — знімаємо при ідемпотентному оновленні.
_LEGACY_PUBLISH_SUPPORT_FOOTERS = (
    """--------------------------------------
Підтримайте нас: Patreon: https://www.patreon.com/c/honey_erbe
Telegram: https://t.me/MedoyidClub
Web: https://www.medoyid-club.com
Мерч: https://medoyid.com/futbolkimedoediv
Радіо Медоїд: https://www.medoyid-club.com/uk/radio""",
    """--------------------------------------
Підтримайте нас: Patreon: https://www.patreon.com/c/honey_erbe
Telegram: https://t.me/MedoyidClub
Web: https://www.medoyid-club.com""",
    """--------------------------------------
Підтримайте нас: Patreon: https://www.patreon.com/c/honey_erbe
Зворотний зв'язок: honey.erbe@gmail.com""",
    """--------------------------------------
Підтримайте нас: Patreon: https://www.patreon.com/c/honey_erbe
Зворотний звʼязок: honey.erbe@gmail.com""",
)

_TELEGRAM_FULL_PACKAGE_SAFE = 4085


def load_system_prompt(path: Path | None = None) -> str:
    p = path or SYSTEM_PROMPT_FILE
    return p.read_text(encoding="utf-8")


def example_generation_input() -> dict[str, Any]:
    """Еталонна форма вхідного JSON для Gemini (підставляється з майстра в боті)."""
    return {
        "draft_material": "Чернетковий опис теми й тез трансляції.",
        "locale": "uk",
        "style": "філософський",
        "speakers": [
            {
                "display_name": "👩🏻‍🦳Тетяна Гукало",
                "social": {
                    "facebook": "https://www.facebook.com/tania.gukalo",
                    "telegram": "https://t.me/tetianagukalo",
                    "youtube": "https://www.youtube.com/@taniagukalo",
                },
            }
        ],
        "playlists": [
            {
                "playlist_id": "PLxxxxxxxx",
                "title": "Історії мистецтва",
                "canonical_url": "https://www.youtube.com/playlist?list=PLxxxxxxxx",
            }
        ],
        "scheduled_start_time": "2026-06-12T18:00:00+03:00",
        "timezone": "Europe/Kyiv",
        "channel_display_name": "Клуб Медоїдів",
        "youtube_defaults": {
            "privacy_status": "private",
            "category_id": "22",
            "made_for_kids": False,
        },
        "youtube_limits": {
            "title_max": YOUTUBE_TITLE_MAX_CHARS,
            "description_max": YOUTUBE_DESCRIPTION_MAX_CHARS,
            "tags_max": YOUTUBE_TAGS_SOFT_MAX,
        },
    }


def normalize_generation_input(data: dict[str, Any]) -> dict[str, Any]:
    """
    Єдиний вигляд перед Gemini: draft_material завжди list[str].
    Поля youtube_limits доповнюються типовими лімітами YouTube якщо не задані.
    """
    out = copy.deepcopy(data)
    dm = out.get("draft_material")
    if dm is None:
        lines: list[str] = []
    elif isinstance(dm, str):
        lines = [dm.strip()] if dm.strip() else []
    elif isinstance(dm, list):
        lines = [str(x).strip() for x in dm if str(x).strip()]
    else:
        lines = [str(dm)]
    out["draft_material"] = lines

    lim = dict(out.get("youtube_limits") or {})
    lim.setdefault("title_max", YOUTUBE_TITLE_MAX_CHARS)
    lim.setdefault("description_max", YOUTUBE_DESCRIPTION_MAX_CHARS)
    lim.setdefault("tags_max", YOUTUBE_TAGS_SOFT_MAX)
    out["youtube_limits"] = lim
    return out


def _compact_for_match(s: str) -> str:
    return "".join(ch for ch in s.lower() if not ch.isspace())


def _url_in_aggregate_text(blob: str, url: str) -> bool:
    """Чи є змога вважати, що цей URL уже згадано в тексті (різні нормалізації)."""
    u = str(url).strip()
    if not u:
        return True
    b_lo = blob.lower()
    variants = (
        u,
        u.rstrip("/"),
        unquote(u),
        unquote(u).lower(),
        u.replace("https://", "").replace("http://", ""),
    )
    for v in variants:
        if not v.strip():
            continue
        vl = v.lower()
        if vl in b_lo:
            return True
        if _compact_for_match(v) in _compact_for_match(blob):
            return True
    p = urlparse(u)
    if p.netloc and p.path:
        np = (p.netloc + p.path).lower()
        if np in _compact_for_match(blob):
            return True
    qs = parse_qs(p.query or "", keep_blank_values=True)
    for k, vals in qs.items():
        for v in vals:
            frag = f"{k.lower()}={v.lower()}"
            if len(frag) > 8 and frag in b_lo:
                return True
            if len(vals) == 1 and len(v.strip()) >= 12 and v.lower() in b_lo:
                return True
    for seg in filter(None, (p.path or "").split("/")):
        if len(seg) >= 14 and seg.lower() in b_lo:
            return True
    return False


def _strip_one_footer(text: str, footer: str) -> str:
    t = text.rstrip()
    suf = "\n\n" + footer
    changed = True
    while changed:
        changed = False
        if t.endswith(footer):
            t = t[: -len(footer)].rstrip()
            changed = True
            continue
        if t.endswith(suf):
            t = t[: -len(suf)].rstrip()
            changed = True
            continue
    return t


def _strip_publish_automation_suffix(text: str) -> str:
    """Прибирає раніше автододані блоки (каталог + фіксований футер підтримки)."""
    t = text.rstrip()
    for footer in (_PUBLISH_SUPPORT_FOOTER_LINES, *_LEGACY_PUBLISH_SUPPORT_FOOTERS):
        t = _strip_one_footer(t, footer)

    intro = "\n\nДодаткові посилання з каталогу спікерів:\n\n"
    j = t.rfind(intro)
    if j >= 0:
        return t[:j].rstrip()

    intro2 = "Додаткові посилання з каталогу спікерів:\n\n"
    j2 = t.rfind(intro2)
    if j2 >= 0:
        t = t[:j2].rstrip()

    return t


def prefer_human_package_as_youtube_description(bundle: dict[str, Any]) -> None:
    """
    Єдиний публічний опис = колишній «телеграм-пакунок» (формат, який ми залишаємо).
    Якщо telegram.full_package_plain непорожній — він стає youtube.description;
    інакше description копіюється в telegram для дзеркала схеми.
    """
    ensure_bundle_skeleton(bundle)
    yt = bundle["youtube"]
    tg = bundle.setdefault("telegram", {})
    if not isinstance(tg, dict):
        bundle["telegram"] = {"full_package_plain": ""}
        tg = bundle["telegram"]

    plain = str(tg.get("full_package_plain") or "").strip()
    desc = str(yt.get("description") or "").strip()
    if plain:
        yt["description"] = plain
        tg["full_package_plain"] = plain
    elif desc:
        tg["full_package_plain"] = desc
        yt["description"] = desc


_HTTPS_PREFIXED_EMAIL_RE = re.compile(
    r"https?://([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b"
)


def _fix_https_prefixed_emails(text: str) -> str:
    """Прибирає помилковий https:// перед email (PayPal тощо)."""
    return _HTTPS_PREFIXED_EMAIL_RE.sub(r"\1", text or "")


def _plain_speaker_name(display_name: str) -> str:
    """Ім'я без emoji/символів — для пошуку блоку в описі."""
    parts = [
        ch if (ch.isalpha() or ch in " '-’`.") else " " for ch in (display_name or "")
    ]
    return " ".join("".join(parts).split())


def _missing_social_lines(sp: dict[str, Any], blob: str) -> list[str]:
    soc = sp.get("social")
    if not isinstance(soc, dict):
        return []
    labels = speakers_catalog.SPEAKER_SOCIAL_LABELS_UK
    out: list[str] = []
    for key in speakers_catalog.SPEAKER_SOCIAL_KEY_ORDER:
        val = soc.get(key)
        if not isinstance(val, str) or not val.strip():
            continue
        u = val.strip()
        if not _url_in_aggregate_text(blob, u):
            out.append(f"{labels.get(key, key)}: {u}")
    return out


def _inject_missing_catalog_links(text: str, inp: dict[str, Any]) -> str:
    """
    Дописує відсутні посилання з speakers.csv у вже наявний блок спікера.
    Без окремого заголовка «Додаткові посилання…».
    """
    speakers = inp.get("speakers")
    if not isinstance(speakers, list) or not text:
        return text

    result = text
    for sp in speakers:
        if not isinstance(sp, dict):
            continue
        name = str(sp.get("display_name") or "").strip()
        plain = _plain_speaker_name(name)
        if not plain:
            continue
        miss = _missing_social_lines(sp, result)
        if not miss:
            continue

        lines = result.splitlines()
        name_idx = -1
        plain_lo = plain.lower()
        for i, line in enumerate(lines):
            if plain_lo in line.lower():
                name_idx = i
        if name_idx < 0:
            # Блоку немає — додаємо один звичайний блок спікера в кінці (перед футером).
            block = name + ":\n" + "\n".join(miss)
            result = (result.rstrip() + "\n\n" + block).strip()
            continue

        # Кінець блоку: до розділювача / порожнього рядка перед наступним спікером / кінця.
        end = name_idx + 1
        while end < len(lines):
            ln = lines[end].strip()
            if ln.startswith("---") or set(ln) == {"-"} and len(ln) >= 10:
                break
            if not ln and end + 1 < len(lines):
                nxt = lines[end + 1].strip()
                # наступний блок спікера або хештеги
                if nxt.startswith("#") or (nxt.endswith(":") and "http" not in nxt.lower()):
                    break
            end += 1

        insert_at = end
        # Вставляємо після останнього непорожнього рядка блоку.
        while insert_at > name_idx + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines[insert_at:insert_at] = miss
        result = "\n".join(lines)

    return result


def _truncate_core_preserving_footer(core: str, room: int) -> str:
    s = core.rstrip()
    if room <= 0:
        return "..."
    if len(s) <= room:
        return s
    return s[: max(0, room - 3)].rstrip() + "..."


def ensure_publish_standard_blocks(bundle: dict[str, Any], inp: dict[str, Any]) -> None:
    """
    Підтягує відсутні посилання з каталогу спікерів у блоки спікерів (без окремої секції),
    виправляє https:// перед email і додає фіксований футер. Ідемпотентно.
    youtube.description — канон; telegram.full_package_plain дзеркалить його.
    """
    ensure_bundle_skeleton(bundle)
    prefer_human_package_as_youtube_description(bundle)
    nin = normalize_generation_input(inp)
    lim = nin.get("youtube_limits") or {}
    dmax = int(lim.get("description_max") or YOUTUBE_DESCRIPTION_MAX_CHARS)

    yt = bundle["youtube"]
    tg = bundle.setdefault("telegram", {})

    yt_core = _strip_publish_automation_suffix(str(yt.get("description") or ""))
    yt_core = _fix_https_prefixed_emails(yt_core)
    yt_core = _inject_missing_catalog_links(yt_core, nin)

    suffix = "\n\n" + _PUBLISH_SUPPORT_FOOTER_LINES
    suffix_len = len(suffix)
    yt_room = max(0, dmax - suffix_len - 2)
    new_yt_body = _truncate_core_preserving_footer(yt_core, yt_room)
    final_desc = (new_yt_body + suffix).strip()
    yt["description"] = final_desc
    if isinstance(tg, dict):
        tg_room = max(0, _TELEGRAM_FULL_PACKAGE_SAFE - suffix_len - 2)
        tg["full_package_plain"] = _truncate_core_preserving_footer(final_desc, tg_room)


def _strip_markdown_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("\ufeff"):
        t = t.lstrip("\ufeff")
    if "```" not in t:
        return t.strip()
    parts = t.split("```")
    if len(parts) >= 3:
        return parts[1].strip()
    return t.replace("```", "").strip()


def parse_gemini_bundle_text(raw: str) -> dict[str, Any]:
    """Знімає markdown-огорожі, відрізає преамбулу, парсить перший JSON-об'єкт."""
    s = _strip_markdown_fences(raw)
    start = s.find("{")
    if start < 0:
        raise ValueError("У відповіді моделі не знайдено JSON-об'єкта (немає '{' )")
    obj, _ = json.JSONDecoder().raw_decode(s[start:])
    if not isinstance(obj, dict):
        raise ValueError("Кореневий JSON має бути об'єктом")
    return obj


def _empty_bundle() -> dict[str, Any]:
    return {
        "bundle_version": "1",
        "telegram": {"full_package_plain": ""},
        "youtube": {
            "title": "",
            "description": "",
            "tags": [],
            "scheduled_start_time_rfc3339": "",
            "privacy_status": "private",
            "category_id": "22",
            "default_language": "uk",
            "playlist_ids": [],
            "self_declared_made_for_kids": False,
            "custom_notes_for_operator": "",
        },
        "seo": {"hashtags_line": "", "keywords_line": "", "highlight_bullets": []},
    }


def ensure_bundle_skeleton(bundle: dict[str, Any]) -> None:
    """Доповнює відсутні ключі in-place (щоб не ламати подальший код)."""
    base = _empty_bundle()
    for k, v in base.items():
        if k not in bundle:
            bundle[k] = copy.deepcopy(v)
            continue
        if isinstance(v, dict) and isinstance(bundle[k], dict):
            for sk, sv in v.items():
                bundle[k].setdefault(sk, copy.deepcopy(sv))


def validate_bundle_warnings(bundle: dict[str, Any]) -> list[str]:
    warn: list[str] = []
    try:
        yt = bundle.get("youtube") or {}
        title = str(yt.get("title", ""))
        desc = str(yt.get("description", ""))
        if not title.strip():
            warn.append("youtube.title порожній")
        if not desc.strip():
            warn.append("youtube.description порожній")
        if len(title) > YOUTUBE_TITLE_MAX_CHARS:
            warn.append(f"youtube.title довший за {YOUTUBE_TITLE_MAX_CHARS} символів (до ужимання)")
        if len(desc) > YOUTUBE_DESCRIPTION_MAX_CHARS:
            warn.append(
                f"youtube.description довший за {YOUTUBE_DESCRIPTION_MAX_CHARS} символів (до ужимання)"
            )
        tags = yt.get("tags")
        if not isinstance(tags, list):
            warn.append("youtube.tags не масив")
    except Exception as e:
        warn.append(f"validation internal: {e}")
    return warn


def _playlist_ids_from_input(inp: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for p in inp.get("playlists") or []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("playlist_id") or "").strip()
        if pid and pid not in out:
            out.append(pid)
    return out


def merge_bundle_with_normalized_input(bundle: dict[str, Any], inp: dict[str, Any]) -> dict[str, Any]:
    """
    Політика злиття після Gemini:
    - час ефіру, плейлісти, defaults з майстра (inp) перекривають модель;
    - default_language береться з locale якщо задано;
    - решта полів лишаються з моделі.
    """
    merged = copy.deepcopy(bundle)
    ensure_bundle_skeleton(merged)
    yt = merged["youtube"]

    pl = _playlist_ids_from_input(inp)
    if pl:
        yt["playlist_ids"] = pl

    sched = inp.get("scheduled_start_time")
    if isinstance(sched, str) and sched.strip():
        yt["scheduled_start_time_rfc3339"] = sched.strip()

    loc = str(inp.get("locale") or "").strip().lower()
    if loc in {"uk", "ru", "en"}:
        yt["default_language"] = loc

    defaults = inp.get("youtube_defaults") or {}
    if isinstance(defaults, dict):
        ps = str(defaults.get("privacy_status") or "").strip()
        if ps in {"private", "unlisted", "public"}:
            yt["privacy_status"] = ps
        cid = str(defaults.get("category_id") or "").strip()
        if cid:
            yt["category_id"] = cid
        if "made_for_kids" in defaults and isinstance(defaults["made_for_kids"], bool):
            yt["self_declared_made_for_kids"] = defaults["made_for_kids"]

    return merged


def apply_youtube_hard_limits(
    bundle: dict[str, Any], inp: dict[str, Any], *, trim_description: bool = True
) -> dict[str, Any]:
    """Обрізає title/description і кількість tags згідно youtube_limits з нормалізованого input."""
    out = copy.deepcopy(bundle)
    ensure_bundle_skeleton(out)
    lim = (inp or {}).get("youtube_limits") or {}
    tmax = int(lim.get("title_max") or YOUTUBE_TITLE_MAX_CHARS)
    tagmax = int(lim.get("tags_max") or YOUTUBE_TAGS_SOFT_MAX)

    yt = out["youtube"]
    title = str(yt.get("title", ""))
    desc = str(yt.get("description", ""))
    if len(title) > tmax:
        yt["title"] = title[: max(0, tmax - 1)].rstrip() + "..."
    if trim_description:
        dmax = int(lim.get("description_max") or YOUTUBE_DESCRIPTION_MAX_CHARS)
        if len(desc) > dmax:
            yt["description"] = desc[: max(0, dmax - 1)].rstrip() + "..."

    tags = yt.get("tags")
    if isinstance(tags, list):
        cleaned: list[str] = []
        for t in tags:
            s = str(t).strip().lstrip("#").strip()
            if s and s not in cleaned:
                cleaned.append(s)
            if len(cleaned) >= tagmax:
                break
        yt["tags"] = cleaned
    else:
        yt["tags"] = []

    return out


def generation_input_from_user_data(ud: dict[str, Any]) -> dict[str, Any]:
    """Зібрати USER_INPUT для Gemini з context.user_data після завершення майстра в Telegram."""
    import settings as st

    out = copy.deepcopy(example_generation_input())
    draft = ud.get("draft_raw")
    if isinstance(draft, str) and draft.strip():
        out["draft_material"] = draft.strip()

    loc = str(ud.get("locale") or "").strip().lower()
    if loc:
        out["locale"] = loc

    sty = ud.get("style")
    if isinstance(sty, str) and sty.strip():
        out["style"] = sty.strip()

    sp = ud.get("speakers")
    if isinstance(sp, list):
        out["speakers"] = copy.deepcopy(sp)

    pl = ud.get("playlists")
    if isinstance(pl, list):
        out["playlists"] = copy.deepcopy(pl)

    sched = ud.get("scheduled_start_time")
    if isinstance(sched, str) and sched.strip():
        out["scheduled_start_time"] = sched.strip()

    tz = str(ud.get("timezone") or "").strip()
    if tz:
        out["timezone"] = tz

    out["channel_display_name"] = st.youtube_channel_display_name()
    return out


def example_generation_input_json(indent: int = 2) -> str:
    return json.dumps(example_generation_input(), ensure_ascii=False, indent=indent)
