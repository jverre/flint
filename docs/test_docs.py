"""Docs snippet test suite - catches doc rot as the API evolves.

Two layers:
  Layer 1 - Auto-extracted self-contained snippets from docs/*.mdx, run as
            subprocesses. Parametrized with [filename::snippet-N] IDs.
  Layer 2 - Explicit feature tests using the shared `sandbox` fixture, one
            per major feature area demonstrated in the docs.
"""

import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from flint import Sandbox

DOCS_DIR = Path(__file__).parent


# ── Snippet extraction ────────────────────────────────────────────────────────


def extract_python_blocks(path: Path) -> list[tuple[str, int]]:
    """Return list of (code, block_index) from a .mdx file."""
    text = path.read_text()
    pattern = re.compile(r"```python\n(.*?)```", re.DOTALL)
    blocks = []
    for i, match in enumerate(pattern.finditer(text)):
        code = textwrap.dedent(match.group(1))
        blocks.append((code, i))
    return blocks


def is_self_contained(code: str) -> bool:
    return (
        "from flint import" in code
        and ("Sandbox()" in code or "Sandbox(template_id" in code)
        and "kill()" in code
    )


def is_template_snippet(code: str) -> bool:
    return "Template(" in code and ".build()" in code


def collect_self_contained_snippets() -> list[tuple[str, str]]:
    """Return list of (test_id, code) for all self-contained Python snippets."""
    snippets = []
    for mdx_file in sorted(DOCS_DIR.rglob("*.mdx")):
        rel = mdx_file.relative_to(DOCS_DIR)
        for code, idx in extract_python_blocks(mdx_file):
            if is_self_contained(code):
                test_id = f"{rel}::snippet-{idx}"
                snippets.append((test_id, code))
    return snippets


_SNIPPETS = collect_self_contained_snippets()


def _snippet_marks(code: str) -> list:
    if is_template_snippet(code):
        return [pytest.mark.slow]
    return []


# ── Layer 1: auto-extracted self-contained snippets ──────────────────────────


@pytest.mark.parametrize(
    "test_id,code",
    [
        pytest.param(tid, code, id=tid, marks=_snippet_marks(code))
        for tid, code in _SNIPPETS
    ],
)
def test_docs_snippet(test_id, code):
    """Run a self-contained docs snippet in a subprocess."""
    full_env = {
        **os.environ,
        "FLINT_PORT": "9101",
        "FLINT_DATA_DIR": "/microvms-test",
        "FLINT_STATE_DIR": "/tmp/flint-test",
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        env=full_env,
    )
    assert result.returncode == 0, (
        f"Snippet {test_id!r} failed (exit {result.returncode}):\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


# ── Layer 2: explicit feature tests ──────────────────────────────────────────


# docs: sdk/commands.mdx
def test_docs_commands_run(sandbox):
    result = sandbox.commands.run("echo hello", timeout=60)
    assert result.stdout == "hello\n"
    assert result.stderr == ""
    assert result.exit_code == 0


# docs: sdk/commands.mdx
def test_docs_commands_exit_code(sandbox):
    result = sandbox.commands.run("ls /nonexistent")
    assert result.exit_code != 0
    assert result.stderr != "" or result.stdout != ""


# docs: sdk/commands.mdx
def test_docs_commands_streaming(sandbox):
    lines = []

    def collect(line):
        lines.append(line)

    result = sandbox.commands.run(
        "for i in 1 2 3; do echo $i; done",
        on_stdout=collect,
    )
    assert result.exit_code == 0
    assert len(lines) >= 3


# docs: sdk/commands.mdx
def test_docs_commands_timeout(sandbox):
    result = sandbox.commands.run("sleep 10", timeout=2)
    assert result.exit_code == -1


# docs: sdk/commands.mdx
def test_docs_commands_multiline(sandbox):
    result = sandbox.commands.run("""
set -e
cd /tmp
echo "step 1" > out.txt
echo "step 2" >> out.txt
cat out.txt
""")
    assert result.exit_code == 0
    assert "step 1" in result.stdout
    assert "step 2" in result.stdout


# docs: sdk/sandbox.mdx
def test_docs_sandbox_properties(sandbox):
    assert sandbox.id
    assert sandbox.state
    assert sandbox.pid > 0
    assert sandbox.created_at > 0
    assert isinstance(sandbox.timings, dict)
    assert len(sandbox.timings) > 0


# docs: sdk/sandbox.mdx
def test_docs_sandbox_list_and_connect(sandbox):
    sandboxes = Sandbox.list()
    ids = [s.id for s in sandboxes]
    assert sandbox.id in ids

    connected = Sandbox.connect(sandbox.id)
    assert connected.id == sandbox.id
    assert connected.is_running()


# docs: sdk/sandbox.mdx
def test_docs_sandbox_set_timeout(sandbox):
    sandbox.set_timeout(300, policy="kill")
    sandbox.set_timeout(300, policy="pause")


# docs: sdk/sandbox.mdx
def test_docs_sandbox_file_ops(sandbox):
    sandbox.write_file("/tmp/data.txt", "hello world")
    content = sandbox.read_file("/tmp/data.txt")
    assert b"hello world" in content

    sandbox.write_file("/tmp/script.py", b"print('hi')", mode="0755")

    entries = sandbox.list_files("/tmp")
    names = [e["name"] for e in entries]
    assert "data.txt" in names
    assert "script.py" in names


# docs: sdk/sandbox.mdx
def test_docs_sandbox_run_code(sandbox):
    result = sandbox.run_code("print('hello')")
    assert result.exit_code == 0
    assert "hello" in result.stdout


# docs: sdk/pty.mdx
def test_docs_pty_session(sandbox):
    output: list[bytes] = []

    session = sandbox.pty.create(
        cols=120,
        rows=40,
        on_data=lambda data: output.append(data),
    )
    session.send_input("echo pty-works\n")
    time.sleep(0.5)
    session.kill()

    combined = b"".join(output)
    assert b"pty-works" in combined


# docs: sdk/sandbox.mdx
def test_docs_sandbox_pause_resume(sandbox):
    sandbox.commands.run("touch /tmp/my-file.txt")

    sandbox.pause()
    assert sandbox.state == "Paused"

    sandbox.resume()
    assert sandbox.is_running()

    result = sandbox.commands.run("ls /tmp/my-file.txt")
    assert result.exit_code == 0


# ── Template tests (slow - skip by default) ──────────────────────────────────


@pytest.mark.slow
def test_docs_templates_basic_build():
    """docs: sdk/templates.mdx - build a minimal custom template."""
    from flint import Template

    template = (
        Template("test-alpine-env")
        .from_alpine_image("3.19")
        .build()
    )
    assert template.template_id
    assert template.status == "ready"

    sandbox = Sandbox(template_id=template.template_id)
    result = sandbox.run_command("echo template-works")
    sandbox.kill()

    assert "template-works" in result.stdout
