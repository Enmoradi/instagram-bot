FROM node:22-bookworm-slim AS node-runtime
FROM python:3.11-slim-bookworm

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node

# ffmpeg برای ادغام ویدیو/صدا و تبدیل به MP3 لازم است
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libstdc++6 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py music_ui.py music_store.py music_worker.py lyrics.py web_service.py ./

CMD ["python", "bot.py"]
