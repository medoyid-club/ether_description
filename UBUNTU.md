# Запуск бота на Ubuntu у фоні

Коротко: віртуальне середовище Python, `.env`, потім **systemd --user** (або `nohup`).

## Залежності та встановлення

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
git clone <URL-репозиторію> ether_description
cd ether_description

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Конфігурація

```bash
cp .env.example .env
nano .env
```

Обовʼязково вкажіть хоча б `TELEGRAM_BOT_TOKEN`; для Gemini та YouTube див. коментарі в `.env.example`.

На сервер поруч із проєктом покладіть **не через git**:

- `client_secret*.json` (OAuth клієнт з Google Cloud, тип Desktop), або шлях у `YOUTUBE_CLIENT_SECRET_FILE`;
- `token.json` — див. [YOUTUBE_TOKEN_RUNBOOK.md](YOUTUBE_TOKEN_RUNBOOK.md);
- за потреби `stream_key.txt` (локальні ключі стріму).

## Перевірка вручну

```bash
cd /повний/шлях/до/ether_description
source .venv/bin/activate
python main.py
```

Зупинка: `Ctrl+C`.

## Фон: systemd (користувацька служба)

Замініть `YOU` та шлях до проєкту.

```bash
mkdir -p ~/.config/systemd/user
nano ~/.config/systemd/user/ether-description-bot.service
```

Вміст юніта:

```ini
[Unit]
Description=Ether description Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/YOU/ether_description
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/YOU/ether_description/.venv/bin/python main.py
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
```

Увімкнути та дивитися логи:

```bash
systemctl --user daemon-reload
systemctl --user enable --now ether-description-bot.service
journalctl --user -u ether-description-bot.service -f
```

Після виходу з SSH процес user-служби зазвичай лишається лише якщо увімкнено **linger**:

```bash
loginctl enable-linger "$USER"
```

## Альтернатива без systemd

```bash
cd /повний/шлях/до/ether_description
source .venv/bin/activate
nohup python main.py >> bot.log 2>&1 &
disown
```

Перегляд логу: `tail -f bot.log`.
