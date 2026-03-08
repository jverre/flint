# Flint

Firecracker VM management tool with a Python SDK and optional TUI.

## Quick Start

```bash
# Install
uv sync

# Launch the TUI
uv run flint start

# CLI commands
uv run flint list
uv run flint stop <vm_id>
```

## SDK

```python
from flint import Sandbox, SandboxManager

manager = SandboxManager()

# Create a sandbox
sandbox = manager.create()

# Run a command
result = sandbox.commands.run("ls -la")
print(result.stdout)
print(result.exit_code)

# Fire-and-forget command
sandbox.commands.send("sleep 100")

# File operations
content = sandbox.files.read("/etc/hosts")
sandbox.files.write("/tmp/hello.txt", "world")
entries = sandbox.files.list("/tmp")

# Interactive PTY session
pty = sandbox.pty.create(cols=120, rows=40)
pty.send_input("ls -la\n")
pty.on_data(lambda data: print(data.decode()))
pty.resize(cols=200, rows=50)
pty.kill()

# List and connect
sandboxes = manager.list()
sandbox = manager.get(sandbox_id)

# Cleanup
sandbox.kill()
```
