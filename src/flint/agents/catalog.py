"""Agent catalog — registry of pre-packaged AI agents for Flint microVMs."""

from __future__ import annotations

from dataclasses import dataclass, field

# GHCR registry where pre-built agent images are published by CI.
GHCR_REGISTRY = "ghcr.io/jverre/flint"


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
    docker_image: str = ""


def _agent_dockerfile(docker_image: str) -> str:
    """Generate a Dockerfile that pulls from a pre-built GHCR image.

    The Flint injection layer (flintd + init-net.sh) is appended automatically
    by the template build pipeline, so we only need the FROM line here.
    """
    return (
        f"FROM {docker_image}\n"
        "\n"
        "# Flint injection (always last)\n"
        "RUN apt-get update && apt-get install -y iproute2 || apk add iproute2 || true\n"
        "COPY flintd /usr/local/bin/flintd\n"
        "COPY init-net.sh /etc/init-net.sh\n"
        "RUN chmod +x /usr/local/bin/flintd /etc/init-net.sh\n"
    )


# ── Agent Definitions ──────────────────────────────────────────────────────

_HERMES = AgentDefinition(
    name="hermes",
    description="Self-improving AI agent by Nous Research with persistent memory and a learning loop.",
    repo="https://github.com/NousResearch/hermes-agent.git",
    version="latest",
    homepage="https://hermes-agent.nousresearch.com",
    license="Apache-2.0",
    tags=["ai-agent", "self-improving", "multi-platform", "nous-research"],
    rootfs_size_mb=2048,
    docker_image=f"{GHCR_REGISTRY}/agent-hermes:latest",
    default_env={
        "HERMES_HOST": "0.0.0.0",
        "HERMES_PORT": "3000",
    },
    dockerfile=_agent_dockerfile(f"{GHCR_REGISTRY}/agent-hermes:latest"),
    post_start_cmd="cd /opt/hermes && hermes gateway --bind 0.0.0.0 --port 3000 2>/dev/null &",
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
    docker_image=f"{GHCR_REGISTRY}/agent-openclaw:latest",
    default_env={
        "OPENCLAW_HOST": "0.0.0.0",
        "OPENCLAW_PORT": "4000",
    },
    dockerfile=_agent_dockerfile(f"{GHCR_REGISTRY}/agent-openclaw:latest"),
    post_start_cmd="cd /app && node dist/index.js gateway --bind 0.0.0.0 --port 4000 2>/dev/null &",
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
