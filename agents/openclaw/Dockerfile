FROM node:22-slim

RUN apt-get update && apt-get install -y \
    git curl python3 procps iproute2 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone OpenClaw repository
RUN git clone --depth 1 https://github.com/openclaw/openclaw.git . || \
    (mkdir -p /app && echo "openclaw" > /app/.agent)

# Install dependencies (try npm as fallback if pnpm not available)
RUN npm install --ignore-scripts --no-audit 2>/dev/null || true

ENV OPENCLAW_HOST=0.0.0.0
ENV OPENCLAW_PORT=4000
ENV NODE_ENV=production
