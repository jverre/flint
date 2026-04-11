"""Agent catalog — registry of pre-packaged AI agents for Flint microVMs."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentDefinition:
    """Definition of a pre-packaged agent available for deployment."""

    name: str
    description: str
    repo: str
    version: str
    dockerfile: str
    rootfs_size_mb: int = 2048
    default_env: dict[str, str] = field(default_factory=dict)
    post_start_cmd: str | None = None
    homepage: str = ""
    license: str = ""
    tags: list[str] = field(default_factory=list)


# ── Agent Definitions ──────────────────────────────────────────────────────

_HERMES = AgentDefinition(
    name="hermes",
    description="Self-improving AI agent by Nous Research with persistent memory and a learning loop.",
    repo="https://github.com/NousResearch/hermes-agent.git",
    version="latest",
    homepage="https://hermes-agent.nousresearch.com",
    license="Apache-2.0",
    tags=["ai-agent", "self-improving", "multi-platform", "nous-research"],
    rootfs_size_mb=4096,
    default_env={
        "HERMES_HOST": "0.0.0.0",
        "HERMES_PORT": "3000",
    },
    dockerfile="""\
FROM python:3.12-slim

RUN apt-get update && apt-get install -y \\
    git curl build-essential && \\
    rm -rf /var/lib/apt/lists/*

# Install Node.js (required by Hermes)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \\
    apt-get install -y nodejs && \\
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone Hermes Agent
RUN git clone --depth 1 https://github.com/NousResearch/hermes-agent.git .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || \\
    pip install --no-cache-dir -e . 2>/dev/null || true

# Install Node dependencies if present
RUN if [ -f package.json ]; then npm install --production; fi

ENV HERMES_HOST=0.0.0.0
ENV HERMES_PORT=3000

# Flint injection (always last)
RUN apt-get update && apt-get install -y iproute2 || true
COPY flintd /usr/local/bin/flintd
COPY init-net.sh /etc/init-net.sh
RUN chmod +x /usr/local/bin/flintd /etc/init-net.sh
""",
    post_start_cmd="cd /app && python -m hermes_agent 2>/dev/null || python main.py 2>/dev/null &",
)

_OPENCLAW = AgentDefinition(
    name="openclaw",
    description="Autonomous AI agent framework for real-world task execution via messaging platforms.",
    repo="https://github.com/openclaw/openclaw.git",
    version="latest",
    homepage="https://docs.openclaw.ai",
    license="MIT",
    tags=["ai-agent", "autonomous", "messaging", "task-execution"],
    rootfs_size_mb=4096,
    default_env={
        "OPENCLAW_HOST": "0.0.0.0",
        "OPENCLAW_PORT": "4000",
    },
    dockerfile="""\
FROM node:20-slim

RUN apt-get update && apt-get install -y \\
    git curl python3 python3-pip build-essential && \\
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone OpenClaw
RUN git clone --depth 1 https://github.com/openclaw/openclaw.git .

# Install dependencies
RUN if [ -f package.json ]; then npm install --production; fi
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt --break-system-packages; fi

ENV OPENCLAW_HOST=0.0.0.0
ENV OPENCLAW_PORT=4000

# Flint injection (always last)
RUN apt-get update && apt-get install -y iproute2 || true
COPY flintd /usr/local/bin/flintd
COPY init-net.sh /etc/init-net.sh
RUN chmod +x /usr/local/bin/flintd /etc/init-net.sh
""",
    post_start_cmd="cd /app && npm start 2>/dev/null || node index.js 2>/dev/null &",
)


# ── Catalog Registry ───────────────────────────────────────────────────────

_CATALOG: dict[str, AgentDefinition] = {
    "hermes": _HERMES,
    "openclaw": _OPENCLAW,
}


def register_agent(definition: AgentDefinition) -> None:
    """Register a custom agent definition in the catalog."""
    _CATALOG[definition.name] = definition


def list_agents() -> list[AgentDefinition]:
    """Return all available agent definitions."""
    return list(_CATALOG.values())


def get_agent(name: str) -> AgentDefinition | None:
    """Look up an agent definition by name."""
    return _CATALOG.get(name)
