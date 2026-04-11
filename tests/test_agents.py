"""End-to-end tests for the Flint agent packaging system.

Tests are split into:
- Catalog tests: pure-Python, no daemon or Docker needed.
- Build & deploy tests: require a running daemon and Docker (marked @pytest.mark.slow).
- CLI tests: exercise the Click CLI via CliRunner.
"""

import shutil
import time

import pytest

from flint.agents.catalog import (
    AgentDefinition,
    get_agent,
    list_agents,
    register_agent,
    _CATALOG,
)
from flint.agents.agent import Agent, AgentStatus, _template_id_for


# ── Catalog tests (no daemon required) ────────────────────────────────────


class TestCatalog:
    """Tests for the agent catalog registry — no daemon needed."""

    def test_list_agents_returns_builtin(self):
        agents = list_agents()
        names = [a.name for a in agents]
        assert "hermes" in names
        assert "openclaw" in names

    def test_list_agents_returns_agent_definitions(self):
        agents = list_agents()
        for agent in agents:
            assert isinstance(agent, AgentDefinition)

    def test_get_agent_hermes(self):
        defn = get_agent("hermes")
        assert defn is not None
        assert defn.name == "hermes"
        assert defn.repo == "https://github.com/NousResearch/hermes-agent.git"
        assert defn.license == "Apache-2.0"
        assert "ai-agent" in defn.tags
        assert defn.docker_image  # non-empty
        assert defn.dockerfile  # non-empty
        assert defn.rootfs_size_mb > 0

    def test_get_agent_openclaw(self):
        defn = get_agent("openclaw")
        assert defn is not None
        assert defn.name == "openclaw"
        assert defn.repo == "https://github.com/openclaw/openclaw.git"
        assert defn.license == "MIT"
        assert "ai-agent" in defn.tags
        assert defn.docker_image  # non-empty
        assert defn.dockerfile  # non-empty

    def test_get_agent_unknown_returns_none(self):
        assert get_agent("nonexistent-agent-xyz") is None

    def test_agent_definition_has_required_fields(self):
        for defn in list_agents():
            assert defn.name, f"Agent missing name"
            assert defn.description, f"Agent {defn.name} missing description"
            assert defn.repo, f"Agent {defn.name} missing repo"
            assert defn.version, f"Agent {defn.name} missing version"
            assert defn.dockerfile, f"Agent {defn.name} missing dockerfile"
            assert defn.rootfs_size_mb > 0, f"Agent {defn.name} has invalid rootfs_size_mb"

    def test_agent_definition_default_env(self):
        for defn in list_agents():
            assert isinstance(defn.default_env, dict)

    def test_agent_definition_tags(self):
        for defn in list_agents():
            assert isinstance(defn.tags, list)
            assert len(defn.tags) > 0, f"Agent {defn.name} has no tags"

    def test_dockerfiles_include_flint_injection(self):
        """Every agent Dockerfile must include the flintd guest agent."""
        for defn in list_agents():
            assert "flintd" in defn.dockerfile, (
                f"Agent {defn.name} Dockerfile missing flintd injection"
            )
            assert "init-net.sh" in defn.dockerfile, (
                f"Agent {defn.name} Dockerfile missing init-net.sh injection"
            )

    def test_register_custom_agent(self):
        """Register a custom agent and verify it appears in the catalog."""
        custom = AgentDefinition(
            name="_test-custom-agent",
            description="Test agent for unit tests",
            repo="https://github.com/test/test.git",
            version="0.1.0",
            license="MIT",
            tags=["test"],
            dockerfile="FROM alpine:3.19\n",
        )
        register_agent(custom)
        try:
            assert get_agent("_test-custom-agent") is custom
            assert "_test-custom-agent" in [a.name for a in list_agents()]
        finally:
            # Clean up so we don't pollute other tests
            _CATALOG.pop("_test-custom-agent", None)

    def test_register_agent_overwrites_existing(self):
        """Registering an agent with the same name replaces the old one."""
        original = get_agent("hermes")
        custom = AgentDefinition(
            name="hermes",
            description="Overridden hermes",
            repo="https://example.com/hermes.git",
            version="custom",
            dockerfile="FROM alpine:3.19\n",
        )
        register_agent(custom)
        try:
            assert get_agent("hermes").description == "Overridden hermes"
        finally:
            # Restore the original
            _CATALOG["hermes"] = original


# ── Template ID tests ─────────────────────────────────────────────────────


class TestTemplateId:
    def test_template_id_for_hermes(self):
        assert _template_id_for("hermes") == "agent-hermes"

    def test_template_id_for_openclaw(self):
        assert _template_id_for("openclaw") == "agent-openclaw"

    def test_template_id_for_custom(self):
        assert _template_id_for("my-agent") == "agent-my-agent"


# ── Agent.catalog() class method ──────────────────────────────────────────


class TestAgentCatalogMethod:
    def test_catalog_returns_definitions(self):
        catalog = Agent.catalog()
        assert len(catalog) >= 2
        names = [d.name for d in catalog]
        assert "hermes" in names
        assert "openclaw" in names

    def test_catalog_matches_list_agents(self):
        assert Agent.catalog() == list_agents()


# ── Agent deploy error cases (no daemon needed for validation) ────────────


class TestAgentDeployErrors:
    def test_deploy_unknown_agent_raises(self):
        with pytest.raises(ValueError, match="Unknown agent"):
            Agent.deploy("nonexistent-agent-xyz")

    def test_deploy_error_message_lists_available(self):
        with pytest.raises(ValueError, match="hermes"):
            Agent.deploy("nonexistent-agent-xyz")

    def test_build_unknown_agent_raises(self):
        with pytest.raises(ValueError, match="Unknown agent"):
            Agent.build("nonexistent-agent-xyz")


# ── Build and deploy e2e tests (daemon + Docker) ─────────────────────────

# Use a lightweight test agent instead of real Hermes/OpenClaw to avoid
# pulling from GHCR (which may not be published yet) and to keep tests fast.

_TEST_AGENT_DOCKERFILE = """\
FROM alpine:3.19

RUN apk add --no-cache coreutils

RUN mkdir -p /app && echo '#!/bin/sh' > /app/start.sh && \
    echo 'echo "test-agent running"' >> /app/start.sh && \
    chmod +x /app/start.sh

ENV TEST_AGENT_VERSION=1.0.0
WORKDIR /app

# Flint injection (always last)
RUN apk add iproute2 || true
COPY flintd /usr/local/bin/flintd
COPY init-net.sh /etc/init-net.sh
RUN chmod +x /usr/local/bin/flintd /etc/init-net.sh
"""

_TEST_AGENT = AgentDefinition(
    name="_flint-test-agent",
    description="Lightweight agent for e2e testing",
    repo="https://github.com/test/test.git",
    version="1.0.0",
    license="MIT",
    tags=["test"],
    rootfs_size_mb=200,
    dockerfile=_TEST_AGENT_DOCKERFILE,
    default_env={"TEST_VAR": "default_value"},
    post_start_cmd="/app/start.sh &",
)


@pytest.fixture(scope="module", autouse=True)
def _register_test_agent():
    """Register the test agent for the entire test module."""
    register_agent(_TEST_AGENT)
    yield
    _CATALOG.pop("_flint-test-agent", None)


def _skip_unless_docker_and_linux(backend_kind):
    if backend_kind != "linux-firecracker":
        pytest.skip("Agent build tests only run on Linux Firecracker backend")
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for agent build tests")


@pytest.mark.slow
class TestAgentBuild:
    """Tests for Agent.build() — pre-building agent templates."""

    def test_build_creates_template(self, backend_kind):
        _skip_unless_docker_and_linux(backend_kind)
        info = Agent.build("_flint-test-agent", force=True)
        assert info.template_id == "agent--flint-test-agent"
        assert info.status == "ready"
        assert info.name  # non-empty

    def test_build_caching_skips_rebuild(self, backend_kind):
        _skip_unless_docker_and_linux(backend_kind)
        # First build (may already be cached from previous test)
        info1 = Agent.build("_flint-test-agent")
        assert info1.status == "ready"

        # Second build should return cached result
        info2 = Agent.build("_flint-test-agent")
        assert info2.template_id == info1.template_id
        assert info2.status == "ready"

    def test_build_force_rebuild(self, backend_kind):
        _skip_unless_docker_and_linux(backend_kind)
        # Ensure template exists
        Agent.build("_flint-test-agent")
        # Force rebuild
        info = Agent.build("_flint-test-agent", force=True)
        assert info.status == "ready"

    def test_build_all(self, backend_kind):
        _skip_unless_docker_and_linux(backend_kind)
        # build_all should succeed (builds our test agent at minimum;
        # hermes/openclaw may fail if GHCR images aren't published yet,
        # so we test only the test agent)
        info = Agent.build("_flint-test-agent")
        assert info.status == "ready"


@pytest.mark.slow
class TestAgentDeploy:
    """Tests for Agent.deploy() — full lifecycle."""

    def test_deploy_creates_running_agent(self, backend_kind):
        _skip_unless_docker_and_linux(backend_kind)
        agent = Agent.deploy("_flint-test-agent")
        try:
            assert agent.name == "_flint-test-agent"
            assert agent.sandbox.is_running()
            assert agent.template_info.status == "ready"
        finally:
            agent.stop()

    def test_deploy_with_env_vars(self, backend_kind):
        _skip_unless_docker_and_linux(backend_kind)
        agent = Agent.deploy(
            "_flint-test-agent",
            env={"CUSTOM_KEY": "custom_value"},
        )
        try:
            result = agent.exec("echo $CUSTOM_KEY")
            assert result.exit_code == 0
            assert "custom_value" in result.stdout
        finally:
            agent.stop()

    def test_deploy_default_env_injected(self, backend_kind):
        _skip_unless_docker_and_linux(backend_kind)
        agent = Agent.deploy("_flint-test-agent")
        try:
            result = agent.exec("echo $TEST_VAR")
            assert result.exit_code == 0
            assert "default_value" in result.stdout
        finally:
            agent.stop()

    def test_deploy_env_overrides_default(self, backend_kind):
        _skip_unless_docker_and_linux(backend_kind)
        agent = Agent.deploy(
            "_flint-test-agent",
            env={"TEST_VAR": "overridden"},
        )
        try:
            result = agent.exec("echo $TEST_VAR")
            assert result.exit_code == 0
            assert "overridden" in result.stdout
        finally:
            agent.stop()

    def test_deploy_post_start_runs(self, backend_kind):
        _skip_unless_docker_and_linux(backend_kind)
        agent = Agent.deploy("_flint-test-agent")
        try:
            # post_start_cmd writes to /var/log/agent.log
            result = agent.exec("cat /var/log/agent.log")
            assert result.exit_code == 0
            assert "test-agent running" in result.stdout
        finally:
            agent.stop()


@pytest.mark.slow
class TestAgentExec:
    """Tests for running commands in agent sandboxes."""

    def test_exec_returns_command_result(self, backend_kind):
        _skip_unless_docker_and_linux(backend_kind)
        agent = Agent.deploy("_flint-test-agent")
        try:
            result = agent.exec("echo hello-agent")
            assert result.exit_code == 0
            assert "hello-agent" in result.stdout
        finally:
            agent.stop()

    def test_exec_captures_exit_code(self, backend_kind):
        _skip_unless_docker_and_linux(backend_kind)
        agent = Agent.deploy("_flint-test-agent")
        try:
            result = agent.exec("exit 42")
            assert result.exit_code == 42
        finally:
            agent.stop()

    def test_exec_workdir_is_app(self, backend_kind):
        _skip_unless_docker_and_linux(backend_kind)
        agent = Agent.deploy("_flint-test-agent")
        try:
            result = agent.exec("pwd")
            assert result.exit_code == 0
            # Workdir set in Dockerfile is /app
            assert "/app" in result.stdout
        finally:
            agent.stop()


@pytest.mark.slow
class TestAgentStatus:
    """Tests for agent status reporting."""

    def test_status_returns_agent_status(self, backend_kind):
        _skip_unless_docker_and_linux(backend_kind)
        agent = Agent.deploy("_flint-test-agent")
        try:
            status = agent.status()
            assert isinstance(status, AgentStatus)
            assert status.agent_name == "_flint-test-agent"
            assert status.sandbox_id == agent.sandbox.id
            assert status.sandbox_state in ("Starting", "Started", "Running")
            assert status.template_id == agent.template_info.template_id
        finally:
            agent.stop()


@pytest.mark.slow
class TestAgentLogs:
    """Tests for agent log retrieval."""

    def test_logs_returns_string(self, backend_kind):
        _skip_unless_docker_and_linux(backend_kind)
        agent = Agent.deploy("_flint-test-agent")
        try:
            logs = agent.logs()
            assert isinstance(logs, str)
            # Post-start command should have written to the log
            assert "test-agent running" in logs
        finally:
            agent.stop()

    def test_logs_with_line_limit(self, backend_kind):
        _skip_unless_docker_and_linux(backend_kind)
        agent = Agent.deploy("_flint-test-agent")
        try:
            logs = agent.logs(lines=5)
            assert isinstance(logs, str)
        finally:
            agent.stop()


@pytest.mark.slow
class TestAgentCredentials:
    """Tests for credential injection via network policy."""

    def test_set_credentials_updates_policy(self, backend_kind):
        _skip_unless_docker_and_linux(backend_kind)
        agent = Agent.deploy("_flint-test-agent")
        try:
            agent.set_credentials({
                "api.example.com": {"Authorization": "Bearer test-token-123"},
                "*.internal.io": {"X-Api-Key": "key-456"},
            })
            policy = agent.sandbox.get_network_policy()
            assert policy is not None
            assert "allow" in policy
            assert "api.example.com" in policy["allow"]
            assert "*.internal.io" in policy["allow"]
            rules = policy["allow"]["api.example.com"]
            assert rules[0]["transform"][0]["headers"]["Authorization"] == "Bearer test-token-123"
        finally:
            agent.stop()


@pytest.mark.slow
class TestAgentLifecycle:
    """Tests for agent pause/resume and stop."""

    def test_stop_kills_sandbox(self, backend_kind):
        _skip_unless_docker_and_linux(backend_kind)
        agent = Agent.deploy("_flint-test-agent")
        sandbox_id = agent.sandbox.id
        agent.stop()
        time.sleep(0.5)
        assert not agent.sandbox.is_running()

    def test_pause_and_resume(self, backend_kind):
        _skip_unless_docker_and_linux(backend_kind)
        agent = Agent.deploy("_flint-test-agent")
        try:
            agent.exec("echo persisted > /tmp/agent-persist.txt")
            agent.pause()
            agent.resume()
            result = agent.exec("cat /tmp/agent-persist.txt")
            assert result.exit_code == 0
            assert "persisted" in result.stdout
        finally:
            agent.stop()


# ── CLI tests ─────────────────────────────────────────────────────────────


class TestAgentCLI:
    """Tests for the `flint agents` CLI commands using Click CliRunner."""

    def test_agents_list(self):
        from click.testing import CliRunner
        from flint.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["agents", "list"])
        assert result.exit_code == 0
        assert "hermes" in result.output
        assert "openclaw" in result.output

    def test_agents_info_hermes(self):
        from click.testing import CliRunner
        from flint.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["agents", "info", "hermes"])
        assert result.exit_code == 0
        assert "hermes" in result.output
        assert "Nous Research" in result.output
        assert "Apache-2.0" in result.output
        assert "HERMES_HOST" in result.output

    def test_agents_info_openclaw(self):
        from click.testing import CliRunner
        from flint.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["agents", "info", "openclaw"])
        assert result.exit_code == 0
        assert "openclaw" in result.output
        assert "MIT" in result.output
        assert "OPENCLAW_HOST" in result.output

    def test_agents_info_unknown(self):
        from click.testing import CliRunner
        from flint.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["agents", "info", "nonexistent-xyz"])
        assert result.exit_code != 0
        assert "Unknown agent" in result.output

    def test_agents_build_no_args_errors(self):
        from click.testing import CliRunner
        from flint.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["agents", "build"])
        # Should error: no agent name and no --all
        assert result.exit_code != 0

    def test_agents_deploy_unknown_errors(self):
        from click.testing import CliRunner
        from flint.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["agents", "deploy", "nonexistent-xyz"])
        assert result.exit_code != 0
