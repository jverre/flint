FROM debian:13.4

ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    build-essential nodejs npm python3 python3-pip python3-dev \
    git curl ripgrep gcc libffi-dev procps && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/hermes

# Clone Hermes Agent
RUN git clone --depth 1 https://github.com/NousResearch/hermes-agent.git .

# Install Python dependencies via uv (matching upstream Dockerfile)
RUN pip install --no-cache-dir uv --break-system-packages && \
    uv pip install --system --break-system-packages --no-cache -e ".[all]"

# Install Node dependencies if present
RUN if [ -f package.json ]; then npm install --prefer-offline --no-audit; fi

ENV HERMES_HOST=0.0.0.0
ENV HERMES_PORT=3000
