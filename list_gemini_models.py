"""
Список доступних Gemini-моделей для ключа з `.env`, пріоритет 3.x+ для generateContent.
Запуск з корня проєкту:
  python list_gemini_models.py
Якщо задати GEMINI_MODEL у `.env`, у виводі також покажемо використаний override.
"""

from __future__ import annotations

import logging

from gemini_http import fetch_models
from gemini_models import extract_version, generation_ready, resolve_generation_model
import settings

logging.basicConfig(level=logging.INFO, format="%(message)s")


def main() -> None:
    models = None
    try:
        key = settings.require_gemini_api_key()
        models = fetch_models(key)
    except RuntimeError as e:
        msg = str(e).lower()
        print("Помилка при зверненні до Gemini API:\n")
        print(e)
        print()
        if "api key expired" in msg or "invalid" in msg and "api" in msg:
            print(
                "Схоже, ключ GEMINI_API_KEY недійздатний або прострочений.\n"
                "Створіть новий у https://aistudio.google.com/apikey та оновіть `.env`."
            )
        return

    usable = []
    for m in models:
        name = str(m.get("name", ""))
        if not name:
            continue
        if generation_ready(m):
            ver = extract_version(name)
            ver_s = f"{ver[0]}.{ver[1]}" if ver else "?"
            methods = ",".join(m.get("supportedGenerationMethods") or [])
            usable.append((name, ver_s, methods))

    print(f"Знайдено моделей із generateContent: {len(usable)} із {len(models)} загалом\n")
    for name, ver_s, methods in sorted(usable, key=lambda r: r[0]):
        mark = "*" if "gemini-3" in name.lower() else " "
        print(f"{mark} {name}\t(~{ver_s})\t[{methods}]")

    override = settings.gemini_model_override()
    try:
        selected = resolve_generation_model(models, env_override=None)
        alt = resolve_generation_model(models, env_override=override) if override else selected
        print(
            "\nРекомендований за замовчуванням (flash-lite-latest якщо є в API і без GEMINI_MODEL): ",
            selected,
        )
        if override:
            print("Фактично з GEMINI_MODEL у .env: ", alt)
    except RuntimeError as e:
        print("\nНе вдалося обрати модель:", e)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print("Помилка:", e)
        raise SystemExit(1)
