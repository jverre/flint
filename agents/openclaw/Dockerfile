FROM node:22-bookworm

RUN apt-get update && apt-get install -y \
    git curl python3 python3-pip build-essential && \
    rm -rf /var/lib/apt/lists/*

# Enable corepack for pnpm
RUN corepack enable

WORKDIR /app

# Clone OpenClaw
RUN git clone --depth 1 https://github.com/openclaw/openclaw.git .

# Install dependencies with pnpm
RUN pnpm install --frozen-lockfile || pnpm install --no-frozen-lockfile

# Build the project
RUN pnpm build || true

ENV OPENCLAW_HOST=0.0.0.0
ENV OPENCLAW_PORT=4000
ENV NODE_ENV=production
