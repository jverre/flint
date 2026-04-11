FROM node:20-slim

RUN apt-get update && apt-get install -y \
    git curl python3 python3-pip build-essential && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone OpenClaw
RUN git clone --depth 1 https://github.com/openclaw/openclaw.git .

# Install dependencies
RUN if [ -f package.json ]; then npm install --production; fi
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt --break-system-packages; fi

ENV OPENCLAW_HOST=0.0.0.0
ENV OPENCLAW_PORT=4000
