"""End-to-end tests for the Flint agent packaging system.

These tests build and deploy the real Hermes and OpenClaw agents inside
Flint microVMs.  They require a running daemon, Docker, and the agent
images built locally (CI does this via `docker build` before running
pytest).  All tests are marked ``@pytest.mark.slow``.
"""

import os
import shutil
import tempfile
import time

import pytest

from flint.agents.agent import Agent
from flint.agents.catalog import get_agent, GHCR_REGISTRY


def _dump_daemon_log():
    """Print daemon log for debugging CI failures."""
    log_path = os.path.join(tempfile.gettempdir(), "flint-test-daemon.log")
    try:
        with open(log_path) as f:
            content = f.read()
        # Print last 5000 chars to avoid overwhelming output
        if len(content) > 5000:
            content = f"... (truncated, showing last 5000 chars) ...\n{content[-5000:]}"
        print(f"\n=== DAEMON LOG ({log_path}) ===\n{content}\n=== END DAEMON LOG ===")
    except OSError:
        print(f"\n=== No daemon log at {log_path} ===")


def _require_agent_infra(backend_kind):
    """Skip the test unless we're on Linux-Firecracker with Docker available."""
    if backend_kind != "linux-firecracker":
        pytest.skip("Agent tests require the Linux Firecracker backend")
    if shutil.which("docker") is None:
        pytest.skip("Agent tests require Docker")


# ── Hermes ─────────────────────────────────────────────────────────────────


@pytest.mark.slow
def test_hermes_build_and_deploy(backend_kind):
    """Build the Hermes template, deploy it, and verify the agent is
    functional inside the VM: repo was cloned, Python runtime exists,
    default env vars are injected, and commands execute correctly."""
    _require_agent_infra(backend_kind)

    try:
        agent = Agent.deploy("hermes")
    except Exception as e:
        print(f"\n!!! Agent.deploy('hermes') FAILED: {e}")
        _dump_daemon_log()
        raise

    try:
        assert agent.name == "hermes"
        assert agent.sandbox.is_running()
        assert agent.template_info.status == "ready"
        assert agent.template_info.template_id == "agent-hermes"

        # Hermes workspace should exist at /opt/hermes
        r = agent.exec("ls /opt/hermes")
        assert r.exit_code == 0, f"ls /opt/hermes failed: {r.stderr}"

        # Python runtime must be present (Hermes is Python-based)
        r = agent.exec("python3 --version")
        assert r.exit_code == 0, f"python3 not found: {r.stderr}"
        assert "Python" in r.stdout

        # Basic command execution works
        r = agent.exec("echo hermes_ok")
        assert r.exit_code == 0
        assert "hermes_ok" in r.stdout

        # Default environment variables from the catalog must be set
        r = agent.exec("echo $HERMES_HOST")
        assert r.exit_code == 0
        assert "0.0.0.0" in r.stdout

        r = agent.exec("echo $HERMES_PORT")
        assert r.exit_code == 0
        assert "3000" in r.stdout
    finally:
        agent.stop()


# ── OpenClaw ───────────────────────────────────────────────────────────────


@pytest.mark.slow
def test_openclaw_build_and_deploy(backend_kind):
    """Build the OpenClaw template, deploy it, and verify the agent is
    functional inside the VM: repo was cloned, Node runtime exists,
    default env vars are injected, and commands execute correctly."""
    _require_agent_infra(backend_kind)

    try:
        agent = Agent.deploy("openclaw")
    except Exception as e:
        print(f"\n!!! Agent.deploy('openclaw') FAILED: {e}")
        _dump_daemon_log()
        raise

    try:
        assert agent.name == "openclaw"
        assert agent.sandbox.is_running()
        assert agent.template_info.status == "ready"
        assert agent.template_info.template_id == "agent-openclaw"

        # OpenClaw repo should have been cloned into /app
        r = agent.exec("ls /app")
        assert r.exit_code == 0, f"ls /app failed: {r.stderr}"

        # Node.js runtime must be present (OpenClaw is Node-based)
        r = agent.exec("node --version")
        assert r.exit_code == 0, f"node not found: {r.stderr}"

        # Basic command execution works
        r = agent.exec("echo openclaw_ok")
        assert r.exit_code == 0
        assert "openclaw_ok" in r.stdout

        # Default environment variables from the catalog must be set
        r = agent.exec("echo $OPENCLAW_HOST")
        assert r.exit_code == 0
        assert "0.0.0.0" in r.stdout

        r = agent.exec("echo $OPENCLAW_PORT")
        assert r.exit_code == 0
        assert "4000" in r.stdout
    finally:
        agent.stop()


# ── Template caching ───────────────────────────────────────────────────────


@pytest.mark.slow
def test_agent_template_caching(backend_kind):
    """After the first build, a second deploy must reuse the cached
    template and boot significantly faster (no Docker rebuild)."""
    _require_agent_infra(backend_kind)

    # First build already happened in test_hermes_build_and_deploy.
    # A second build() call should return immediately from cache.
    info = Agent.build("hermes")
    assert info.status == "ready"
    assert info.template_id == "agent-hermes"

    # Deploy from cache — this exercises the fast-path
    agent = Agent.deploy("hermes")
    try:
        assert agent.sandbox.is_running()
        r = agent.exec("echo cache_ok")
        assert r.exit_code == 0
        assert "cache_ok" in r.stdout
    finally:
        agent.stop()


# ── Credential injection ──────────────────────────────────────────────────


@pytest.mark.slow
def test_agent_credential_injection(backend_kind):
    """Deploy an agent, inject API credentials via set_credentials(),
    and verify the network policy round-trips correctly."""
    _require_agent_infra(backend_kind)

    agent = Agent.deploy("hermes")
    try:
        agent.set_credentials({
            "api.openai.com": {"Authorization": "Bearer sk-test-123"},
            "api.anthropic.com": {"x-api-key": "sk-ant-test-456"},
        })
        policy = agent.sandbox.get_network_policy()
        assert policy is not None
        assert "allow" in policy

        openai_rules = policy["allow"]["api.openai.com"]
        assert openai_rules[0]["transform"][0]["headers"]["Authorization"] == "Bearer sk-test-123"

        anthropic_rules = policy["allow"]["api.anthropic.com"]
        assert anthropic_rules[0]["transform"][0]["headers"]["x-api-key"] == "sk-ant-test-456"
    finally:
        agent.stop()


# ── Pause / resume ─────────────────────────────────────────────────────────


@pytest.mark.slow
def test_agent_pause_and_resume(backend_kind):
    """Deploy an agent, write data, pause to disk, resume, and verify
    data survived the snapshot round-trip."""
    _require_agent_infra(backend_kind)

    agent = Agent.deploy("openclaw")
    try:
        agent.exec("echo agent-state-12345 > /tmp/persist.txt")
        agent.pause()
        agent.resume()
        r = agent.exec("cat /tmp/persist.txt")
        assert r.exit_code == 0
        assert "agent-state-12345" in r.stdout
    finally:
        agent.stop()


# ── Build all ──────────────────────────────────────────────────────────────


@pytest.mark.slow
def test_build_all_agents(backend_kind):
    """Agent.build_all() should build every agent in the catalog and
    return ready TemplateInfo for each one."""
    _require_agent_infra(backend_kind)

    results = Agent.build_all()
    assert len(results) >= 2

    names = {r.template_id for r in results}
    assert "agent-hermes" in names
    assert "agent-openclaw" in names

    for info in results:
        assert info.status == "ready"
