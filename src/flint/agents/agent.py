"""Agent deployment — build templates and launch AI agents in Flint microVMs."""

from __future__ import annotations

from dataclasses import dataclass

from flint.agents.catalog import AgentDefinition, get_agent, list_agents
from flint.sandbox import Sandbox, CommandResult
from flint.template import Template, TemplateInfo


@dataclass
class AgentStatus:
    """Status of a deployed agent."""

    agent_name: str
    sandbox_id: str
    sandbox_state: str
    template_id: str


class Agent:
    """Deploy and manage a pre-packaged AI agent in a Flint microVM.

    Usage::

        from flint.agents import Agent

        # Deploy Hermes agent
        agent = Agent.deploy("hermes", env={"MODEL_API_KEY": "sk-..."})
        print(f"Agent running in sandbox {agent.sandbox.id}")

        # Run a command inside the agent's VM
        result = agent.exec("hermes --version")
        print(result.stdout)

        # Inject API credentials via network policy
        agent.set_credentials({
            "api.openai.com": {"Authorization": "Bearer sk-..."},
        })

        # Stop the agent
        agent.stop()

    You can also list available agents::

        for defn in Agent.catalog():
            print(f"{defn.name}: {defn.description}")
    """

    def __init__(
        self,
        definition: AgentDefinition,
        sandbox: Sandbox,
        template_info: TemplateInfo,
    ) -> None:
        self._definition = definition
        self._sandbox = sandbox
        self._template_info = template_info

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._definition.name

    @property
    def sandbox(self) -> Sandbox:
        return self._sandbox

    @property
    def definition(self) -> AgentDefinition:
        return self._definition

    @property
    def template_info(self) -> TemplateInfo:
        return self._template_info

    # ── Agent Operations ───────────────────────────────────────────────────

    def exec(self, cmd: str, timeout: float = 60) -> CommandResult:
        """Run a command inside the agent's sandbox."""
        return self._sandbox.run_command(cmd, timeout=timeout)

    def status(self) -> AgentStatus:
        """Get the current status of this agent deployment."""
        return AgentStatus(
            agent_name=self._definition.name,
            sandbox_id=self._sandbox.id,
            sandbox_state=self._sandbox.state,
            template_id=self._template_info.template_id,
        )

    def set_credentials(self, credentials: dict[str, dict[str, str]]) -> None:
        """Inject API credentials via Flint's network policy.

        Example::

            agent.set_credentials({
                "api.openai.com": {"Authorization": "Bearer sk-..."},
                "api.anthropic.com": {"x-api-key": "sk-ant-..."},
            })
        """
        allow: dict[str, list] = {}
        for domain, headers in credentials.items():
            allow[domain] = [{"transform": [{"headers": headers}]}]
        self._sandbox.update_network_policy({"allow": allow})

    def stop(self) -> None:
        """Kill the agent's sandbox."""
        self._sandbox.kill()

    def pause(self) -> None:
        """Pause the agent (snapshot to disk)."""
        self._sandbox.pause()

    def resume(self) -> None:
        """Resume a paused agent."""
        self._sandbox.resume()

    def logs(self, lines: int = 100) -> str:
        """Fetch recent logs from the agent process."""
        result = self._sandbox.run_command(
            f"tail -n {lines} /var/log/agent.log 2>/dev/null || "
            f"journalctl -n {lines} --no-pager 2>/dev/null || "
            f"echo 'No logs available'"
        )
        return result.stdout

    # ── Class Methods ──────────────────────────────────────────────────────

    @classmethod
    def deploy(
        cls,
        agent_name: str,
        *,
        env: dict[str, str] | None = None,
        rootfs_size_mb: int | None = None,
        allow_internet_access: bool = True,
        network_policy: dict | None = None,
    ) -> Agent:
        """Build (if needed) and deploy an agent from the catalog.

        Args:
            agent_name: Name of the agent in the catalog (e.g. "hermes", "openclaw").
            env: Extra environment variables to inject at startup.
            rootfs_size_mb: Override the default rootfs size for the template.
            allow_internet_access: Whether the VM can reach the internet.
            network_policy: Optional network policy with credential injection rules.

        Returns:
            An Agent instance wrapping the running sandbox.
        """
        definition = get_agent(agent_name)
        if definition is None:
            available = ", ".join(a.name for a in list_agents())
            raise ValueError(
                f"Unknown agent '{agent_name}'. Available agents: {available}"
            )

        # Build the template from the agent's Dockerfile
        size = rootfs_size_mb or definition.rootfs_size_mb
        template = Template(f"agent-{definition.name}", rootfs_size_mb=size)
        template_info = template.from_dockerfile(definition.dockerfile).build()

        # Launch a sandbox from the template
        sandbox = Sandbox(
            template_id=template_info.template_id,
            allow_internet_access=allow_internet_access,
            network_policy=network_policy,
        )

        # Inject environment variables
        merged_env = {**definition.default_env, **(env or {})}
        if merged_env:
            env_script = "\n".join(
                f'export {k}="{v}"' for k, v in merged_env.items()
            )
            sandbox.run_command(
                f'cat >> /etc/profile.d/agent-env.sh << \'ENVEOF\'\n{env_script}\nENVEOF'
            )
            # Also export in current shell
            sandbox.run_command(env_script)

        # Run post-start command if defined
        if definition.post_start_cmd:
            sandbox.run_command(
                f"({definition.post_start_cmd}) > /var/log/agent.log 2>&1"
            )

        return cls(definition, sandbox, template_info)

    @classmethod
    def catalog(cls) -> list[AgentDefinition]:
        """List all available agents in the catalog."""
        return list_agents()
