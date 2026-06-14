# Telegram Support Bot — container image
# Webhook + Flask served by Gunicorn; PTB runs on a background asyncio thread.
#
# IMPORTANT: run a SINGLE Gunicorn worker only. _ensure_ptb_started() runs at
# import time, so more than one worker would start multiple bot threads and fight
# over the Telegram webhook. Use threads (not workers) for Flask concurrency.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite DB lives on a mounted volume in production (set DB_PATH=/data/bot_data.db
# and run with: -v /opt/telegram-support-bot/data:/data).
VOLUME ["/data"]

EXPOSE 8443

CMD ["sh", "-c", "gunicorn main:flask_app --bind 0.0.0.0:${PORT:-8443} --workers 1 --threads 8 --timeout 120"]
