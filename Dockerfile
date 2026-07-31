# syntax=docker/dockerfile:1
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY ig_monitor ./ig_monitor

RUN python -m pip install --no-cache-dir .

CMD ["python", "-m", "ig_monitor.scheduler", "--config", "/srv/ig-monitor/config.yaml"]
