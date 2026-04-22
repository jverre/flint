FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    git curl nodejs npm procps iproute2 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/hermes

# Clone Hermes Agent repository
RUN git clone --depth 1 https://github.com/NousResearch/hermes-agent.git . || \
    (mkdir -p /opt/hermes && echo "hermes-agent" > /opt/hermes/.agent)

# Install core Python deps only (skip heavy extras like Playwright/Chromium)
RUN pip install --no-cache-dir -e . 2>/dev/null || \
    pip install --no-cache-dir -r requirements.txt 2>/dev/null || \
    echo "No installable Python package found"

ENV HERMES_HOST=0.0.0.0
ENV HERMES_PORT=3000
