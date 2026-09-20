# syntax=docker/dockerfile:1.7
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libportaudio2 libasound2 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .
COPY config ./config
# The openwakeword wheel ships no model files; bake them in so the Pi works offline.
RUN pi-con --download-models

EXPOSE 8080
ENTRYPOINT ["pi-con"]
