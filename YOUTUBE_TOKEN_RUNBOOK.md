# OAuth `token.json` для цього проєкту

Файл **не комітиться** (див. `.gitignore`). Рекомендований формат — **JSON** з `Credentials.to_json()` після `youtube_oauth_setup.py`.

## Оновити або створити токен (на машині з браузером)

З кореня репозиторію, з активованим venv:

```bash
source .venv/bin/activate
python youtube_oauth_setup.py
```

Потрібен `client_secret*.json` у корені або змінна `YOUTUBE_CLIENT_SECRET_FILE` у `.env`. Результат за замовчуванням — `token.json` (або шлях з `YOUTUBE_TOKEN_FILE`).

Скрипт запитує scope’и з `youtube_oauth_setup.py` (у т.ч. `youtube.force-ssl` для live). Якщо раніше видавали вузький токен — пройдіть скрипт ще раз.

## Сервер без графічного браузера

Згенеруйте `token.json` на робочій станції тією ж командою, потім **безпечно** скопіюйте файл на сервер (`scp`, `rsync` тощо). Не публікуйте токен.

Код також вміє читати **pickle** у файлі токена (старі експорти), але для нових установок краще JSON.

## Після заміни токена

Перезапустіть бота, наприклад:

```bash
systemctl --user restart ether-description-bot.service
```

(назва служби — як у вашому `.service`; див. [UBUNTU.md](UBUNTU.md).)

## Типові проблеми

- **`could not locate runnable browser`** на сервері — очікувано; використайте генерацію на ПК + копіювання `token.json`, або SSH з `-X`/браузером на тій самій машині.
- **403 на плейлисти / live** — перевірте, що в токені є потрібні scope’и; прогоніть `youtube_oauth_setup.py` знову.
- Банер **unverified app** у Google — для власного акаунта часто достатньо «Advanced» → продовжити (для публічних клієнтів потрібна верифікація за політикою Google).
