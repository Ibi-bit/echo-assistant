FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/server/models/hf

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/server
COPY server/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt \
    && pip install --no-deps openwakeword==0.6.0

COPY server/ ./
RUN chmod +x scripts/download_models.sh && ./scripts/download_models.sh

EXPOSE 8765
# .env is supplied by docker-compose (env_file) or the platform's environment.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
