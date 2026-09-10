# tg-service — образ для Linux / VPS.
# На macOS этот образ не нужен: Whisper через mlx требует доступа к Metal,
# которого у контейнера на macOS нет. На Mac запускайте через uv напрямую.

FROM python:3.12-slim

# ffmpeg нужен для декодирования голосовых и видео
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Сначала только манифесты: слой с зависимостями переиспользуется между сборками
COPY pyproject.toml uv.lock .python-version ./
COPY tg_service/__init__.py tg_service/__init__.py
RUN uv sync --frozen --extra media --no-install-project

# Затем код
COPY tg_service tg_service
COPY migrations migrations
RUN uv sync --frozen --extra media

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/root/.cache/huggingface

# data/ монтируется томом: там сессия Telegram, скачанные файлы и логи
VOLUME ["/app/data"]

EXPOSE 8077 8078

# Переопределяется в docker-compose: tg-service | tg-media-worker | tg-login | tg-mcp
CMD ["tg-service"]
