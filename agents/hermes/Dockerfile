FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    git curl build-essential && \
    rm -rf /var/lib/apt/lists/*

# Install Node.js (required by Hermes)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone Hermes Agent
RUN git clone --depth 1 https://github.com/NousResearch/hermes-agent.git .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || \
    pip install --no-cache-dir -e . 2>/dev/null || true

# Install Node dependencies if present
RUN if [ -f package.json ]; then npm install --production; fi

ENV HERMES_HOST=0.0.0.0
ENV HERMES_PORT=3000
