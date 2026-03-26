# 📡 DAILY SIGNALS

Breakthroughs decoded for you – ein täglicher Newsletter über biotech & medizinische Forschung.

## Features

- 📰 Automatische tägliche Newsletter (7-8 Uhr)
- 🧬 Filtered aus bioRxiv, medRxiv & arXiv
- 🤯 Mind-Blowing Fact des Tages
- 🔥 Top 5 Highlights mit Erklärungen
- 💡 10+ Further Insights
- 📚 Learning Resources zum Vertiefen

## Setup (Lokal)

```bash
# Clone Repo
git clone https://github.com/yourusername/daily-signals.git
cd daily-signals

# Install Dependencies
pip install -r requirements.txt

# Copy .env
cp .env.example .env

# Bearbeite .env mit deinen Credentials
nano .env

# Run
python main.py
```

## Environment Variables

```
TELEGRAM_TOKEN=dein_bot_token
TELEGRAM_CHAT_ID=deine_chat_id
```

## Deploy zu Render

1. Push dein Repo zu GitHub
2. Gehe zu [render.com](https://render.com)
3. Create → Web Service
4. Select dein GitHub Repo
5. Build: `pip install -r requirements.txt`
6. Start: `python main.py`
7. Add Environment Variables
8. Deploy!

## Cron Job (täglich 7 Uhr)

Render: Create → Cron Job
- Schedule: `0 7 * * *`
- Command: Curl dein Render Service

Oder GitHub Actions verwenden (.github/workflows/daily.yml)

## Tech Stack

- Python 3.9+
- Telegram Bot API
- feedparser (RSS)
- Render.com (Hosting)

## License

MIT
