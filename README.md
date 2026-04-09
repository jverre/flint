<p align="center">
  <h1 align="center">Flint</h1>
  <p align="center">Lightning-fast Firecracker microVM management with a Python SDK, interactive TUI, and REST API</p>
</p>

<p align="center">
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-python-sdk">SDK</a> •
  <a href="#-templates">Templates</a> •
  <a href="#-tui">TUI</a> •
  <a href="#-cli">CLI</a> •
  <a href="#-rest-api">REST API</a> •
  <a href="#-benchmarks">Benchmarks</a> •
  <a href="#-configuration">Configuration</a> •
  <a href="#-macos-apple-silicon">macOS</a> •
  <a href="#-linux-host-setup">Linux Setup</a>
</p>

---

Flint spins up Firecracker microVMs in milliseconds from a pre-built golden snapshot. It runs as a daemon with a REST API, and ships with a Python SDK, terminal UI, and CLI for interactive VM management. Supports Linux (Firecracker) and macOS Apple Silicon (Virtualization.framework).

https://github.com/user-attachments/assets/5fdbf10e-7e7a-4688-9414-5bde4d4ed428

## 🚀 Quick Start

### Prerequisites

- **Linux** host (x86_64 or aarch64) or **macOS** Apple Silicon
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Go 1.24+ (or Docker Desktop as fallback for building the guest agent)

### Install

```bash
git clone https://github.com/jacquesverre/flint.git
cd flint
uv sync
```

### Setup

Run the interactive setup wizard — it detects your platform, installs all dependencies, and lets you pick a storage backend:

```bash
sudo uv run flint setup   # Linux (requires sudo)
uv run flint setup         # macOS
```

The wizard walks you through:
1. **Platform detection** — auto-detects Linux or macOS and installs the right backend
2. **Storage selection** — choose between local filesystem, AWS S3 Files, or Cloudflare R2
3. **Credential collection** — prompts for any required cloud credentials
4. **Dependency installation** — downloads and builds everything with progress indicators
5. **Config file** — writes a `.env` with your storage settings

For CI or scripting, skip the wizard with `--no-interactive`:

```bash
sudo uv run flint setup --no-interactive
```

Verify everything is in place:

```bash
sudo uv run flint setup --check   # Linux
uv run flint setup --check         # macOS
```

### Run

```bash
# Terminal 1 — start the daemon
uv run flint start

# Terminal 2 — launch the TUI
uv run flint app
```

The daemon creates a golden snapshot on startup, pre-warms a rootfs pool, and listens on `localhost:9100`.

## 🐍 Python SDK

Flint provides an E2B-style `Sandbox` class for programmatic VM management:

```python
from flint import Sandbox

# Create a new sandbox
sandbox = Sandbox()

# Run a command
result = sandbox.commands.run("echo hello")
print(result.stdout)     # "hello"
print(result.exit_code)  # 0

# Properties
sandbox.id            # str
sandbox.state         # str
sandbox.is_running()  # bool

# Pause & resume (state is preserved to disk)
sandbox.pause()
sandbox.resume()

# Auto-cleanup timeout (seconds)
sandbox.set_timeout(300)                   # kill after 5 min
sandbox.set_timeout(300, policy="pause")   # pause instead of kill

# List & connect to existing sandboxes
sandboxes = Sandbox.list()
sandbox = Sandbox.connect(vm_id)

# Clean up
sandbox.kill()
```

> **Note:** The daemon must be running (`flint start`) before using the SDK.

### Code Execution

Execute code with auto-detected runtime (Python or Node.js):

```python
# Python (default)
result = sandbox.run_code("print(2 + 2)")
print(result.stdout)  # "4"

# Node.js
result = sandbox.run_code("console.log('hello')", runtime="node")
print(result.stdout)  # "hello"
```

### Filesystem Operations

Read, write, and list files inside the sandbox:

```python
# Write a file
sandbox.write_file("/workspace/hello.py", "print('hello world')")

# Read it back
content = sandbox.read_file("/workspace/hello.py")
print(content)  # b"print('hello world')"

# List directory contents
files = sandbox.list_files("/workspace")
```

### Interactive PTY

```python
terminal = sandbox.pty.create(
    on_data=lambda data: print(data.decode(), end=''),
)
terminal.send_input("ls -la\n")
terminal.kill()
```

### Network Policy / Credential Injection

Inject HTTP headers into outbound requests based on domain rules. A transparent HTTPS proxy intercepts traffic from the sandbox and adds headers — sandboxes never see raw credentials:

```python
sandbox.update_network_policy({
    "allow": {
        "api.openai.com": [
            {"transform": [{"headers": {"Authorization": "Bearer sk-..."}}]}
        ],
        "*.anthropic.com": [
            {"transform": [{"headers": {"x-api-key": "sk-ant-..."}}]}
        ],
    }
})

# Retrieve current policy
policy = sandbox.get_network_policy()
```

You can also pass `network_policy=` when creating a sandbox via the REST API.

### Boot Timings

```python
# Total boot time
print(sandbox.ready_time_ms)  # e.g. 42

# Per-step breakdown
print(sandbox.timings)
# {'rootfs_copy_ms': 3, 'netns_setup_ms': 5, 'firecracker_popen_ms': 8, ...}
```

## 📦 Templates

Build custom VM templates with a fluent builder API:

```python
from flint import Template, Sandbox

template = (
    Template("python-data-science")
    .from_ubuntu_image("22.04")
    .apt_install("python3", "python3-pip")
    .pip_install("numpy", "pandas")
    .set_workdir("/workspace")
    .build()
)

# Create a sandbox from the template
sandbox = Sandbox(template_id=template.template_id)
```

**Base image methods:**

| Method | Description |
|--------|-------------|
| `.from_ubuntu_image(tag)` | Ubuntu base image |
| `.from_python_image(tag)` | Python base image |
| `.from_node_image(tag)` | Node.js base image |
| `.from_alpine_image(tag)` | Alpine base image |
| `.from_image(image)` | Any Docker image |
| `.from_dockerfile(dockerfile)` | Raw Dockerfile string |

**Operation methods:**

| Method | Description |
|--------|-------------|
| `.apt_install(*packages)` | Install apt packages |
| `.pip_install(*packages)` | Install pip packages |
| `.npm_install(*packages)` | Install npm packages |
| `.run_cmd(cmd)` | Run a shell command |
| `.copy(src, dest)` | Copy files into the image |
| `.set_workdir(path)` | Set the working directory |
| `.set_envs(**envs)` | Set environment variables |
| `.git_clone(repo, dest)` | Clone a git repository |

Call `.build()` to build the template via the daemon. It blocks until the template is ready and returns a `TemplateInfo` with `template_id`, `name`, and `status`.

## 💻 TUI

The TUI connects to the daemon and gives you an interactive terminal into each VM.

| Key | Action |
|-----|--------|
| `+` | New VM |
| `Ctrl+D` | Delete VM |
| `Ctrl+Up` | Previous VM |
| `Ctrl+Down` | Next VM |
| `Ctrl+B` | Run benchmark |
| `Ctrl+K` | Show keybindings |

> **Tip:** The sidebar auto-refreshes. You can also manage VMs from the CLI while the TUI is running.

## 🖥️ CLI

| Command | Description |
|---------|-------------|
| `flint setup` | Interactive setup wizard — installs deps, configures storage backend |
| `flint setup --check` | Verify that all dependencies are installed |
| `flint setup --force` | Rebuild assets even if they already exist |
| `flint setup --no-interactive` | Non-interactive setup (local storage defaults, for CI) |
| `flint start` | Start the daemon (`--port`, `--data-dir`, `--state-dir`) |
| `flint app` | Launch the interactive TUI |
| `flint list` | List running VMs |
| `flint stop <vm_id>` | Kill a VM by ID |
| `flint install-deps` | Install Firecracker, jailer, and vmlinux kernel (Linux) |
| `flint install-deps --check` | Verify installed dependencies |
| `flint setup-macos` | Prepare macOS Virtualization.framework guest assets |
| `flint setup-macos --check` | Verify macOS guest assets |

## 📡 REST API

The daemon exposes a REST API and WebSocket endpoint on `localhost:9100`.

### Sandbox Lifecycle

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/vms` | Create a new VM (`template_id`, `allow_internet_access` query params; optional `network_policy` body) |
| `GET` | `/vms` | List all VMs |
| `GET` | `/vms/{vm_id}` | Get VM details |
| `DELETE` | `/vms/{vm_id}` | Kill a VM |
| `POST` | `/vms/{vm_id}/pause` | Pause a VM (snapshot to disk) |
| `POST` | `/vms/{vm_id}/resume` | Resume a paused VM |
| `PATCH` | `/vms/{vm_id}` | Set timeout / timeout policy |

### Command Execution

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/vms/{vm_id}/exec` | Execute a command (`{cmd, timeout}`) |

### Filesystem

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/vms/{vm_id}/files?path=` | Read a file |
| `POST` | `/vms/{vm_id}/files?path=` | Write a file |
| `DELETE` | `/vms/{vm_id}/files?path=` | Delete a file or directory |
| `GET` | `/vms/{vm_id}/files/stat?path=` | Stat a file |
| `GET` | `/vms/{vm_id}/files/list?path=` | List directory contents |
| `POST` | `/vms/{vm_id}/files/mkdir?path=` | Create a directory |

### Process Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/vms/{vm_id}/processes` | Start a new process |
| `GET` | `/vms/{vm_id}/processes` | List processes |
| `POST` | `/vms/{vm_id}/processes/{pid}/input` | Send stdin to a process |
| `POST` | `/vms/{vm_id}/processes/{pid}/signal` | Send a signal to a process |
| `POST` | `/vms/{vm_id}/processes/{pid}/resize` | Resize process PTY |

### Network Policy

| Method | Endpoint | Description |
|--------|----------|-------------|
| `PUT` | `/vms/{vm_id}/network-policy` | Update credential injection rules |
| `GET` | `/vms/{vm_id}/network-policy` | Get current network policy |

### Templates

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/templates/build` | Build a template from a Dockerfile |
| `GET` | `/templates` | List all templates |
| `GET` | `/templates/{template_id}` | Get template details |
| `DELETE` | `/templates/{template_id}` | Delete a template |

### Terminal / Health

| Protocol | Endpoint | Description |
|----------|----------|-------------|
| `WS` | `/vms/{vm_id}/terminal` | Interactive terminal (binary WebSocket frames) |
| `GET` | `/health` | Health check + golden snapshot status + backend info |

### Examples

```bash
# Create a VM
curl -X POST localhost:9100/vms

# List VMs
curl localhost:9100/vms

# Execute a command
curl -X POST localhost:9100/vms/<vm_id>/exec \
  -H 'Content-Type: application/json' \
  -d '{"cmd": "echo hello", "timeout": 30}'

# Read a file
curl localhost:9100/vms/<vm_id>/files?path=/etc/hostname

# Pause / resume
curl -X POST localhost:9100/vms/<vm_id>/pause
curl -X POST localhost:9100/vms/<vm_id>/resume

# Set auto-timeout (seconds)
curl -X PATCH localhost:9100/vms/<vm_id> -H 'Content-Type: application/json' \
  -d '{"timeout_seconds": 300, "timeout_policy": "kill"}'

# Kill a VM
curl -X DELETE localhost:9100/vms/<vm_id>
```

## ⚡ Benchmarks

Flint includes a built-in benchmark mode that boots N VMs sequentially, measures time-to-interactive for each, and breaks down per-step timings.

<!-- TODO: replace with actual screenshot -->
<p align="center">
  <img src="assets/benchmark-screenshot.png" alt="Flint benchmark — booting 16 VMs with per-step timing breakdown" width="800">
</p>

Press `Ctrl+B` in the TUI to run a benchmark. Results include:

- **Per-VM TTI** (time-to-interactive) with min/avg/median/p95/p99/max
- **Per-step breakdown**: rootfs copy, netns setup, Firecracker popen, API ready wait, snapshot load, drive patch, VM resume, TCP connect, first command
- **Throughput**: VMs/second

## ⚙️ Configuration

Flint is configured via environment variables.

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `FLINT_PORT` | `9100` | Daemon listen port |
| `FLINT_DATA_DIR` | `/microvms` (Linux), `~/Library/Application Support/flint/data` (macOS) | Data directory for VMs |
| `FLINT_STATE_DIR` | `/tmp/flint` (Linux), `~/Library/Application Support/flint/state` (macOS) | State directory for daemon files |

### Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `FLINT_STORAGE_BACKEND` | `local` | Storage backend: `local`, `s3_files`, or `r2` |
| `FLINT_WORKSPACE_DIR` | `/workspace` | Workspace path inside sandboxes |

### S3 Files

| Variable | Default | Description |
|----------|---------|-------------|
| `FLINT_S3_FILES_NFS_ENDPOINT` | — | S3 Files NFS endpoint |

### Cloudflare R2

| Variable | Default | Description |
|----------|---------|-------------|
| `FLINT_R2_ACCOUNT_ID` | — | R2 account ID |
| `FLINT_R2_ACCESS_KEY_ID` | — | R2 access key ID |
| `FLINT_R2_SECRET_ACCESS_KEY` | — | R2 secret access key |
| `FLINT_R2_BUCKET` | `flint-storage` | R2 bucket name |
| `FLINT_R2_CACHE_DIR` | `{STATE_DIR}/r2-cache` | Local cache directory |
| `FLINT_R2_CACHE_SIZE_MB` | `1024` | Local cache size in MB |

### Firecracker / Jailer

| Variable | Default | Description |
|----------|---------|-------------|
| `FLINT_FIRECRACKER_BINARY` | `/usr/local/bin/firecracker` | Path to Firecracker binary |
| `FLINT_JAILER_BINARY` | `jailer` | Path to jailer binary |
| `FLINT_JAILER_BASE_DIR` | `/srv/jailer` | Jailer base directory |
| `FLINT_JAILER_UID` | `1000` | Jailer UID |
| `FLINT_JAILER_GID` | `1000` | Jailer GID |

### macOS Virtualization.framework

| Variable | Default | Description |
|----------|---------|-------------|
| `FLINT_VZ_KERNEL_PATH` | `{DATA_DIR}/vz/vmlinux` | VZ kernel path |
| `FLINT_VZ_ROOTFS_PATH` | `{DATA_DIR}/vz/rootfs.img` | VZ rootfs path |
| `FLINT_VZ_CPU_COUNT` | `2` | vCPUs per VM |
| `FLINT_VZ_MEMORY_BYTES` | `2147483648` (2 GB) | Memory per VM |
| `FLINT_VZ_READY_TIMEOUT` | `60` | Seconds to wait for VM readiness |

## 🍎 macOS Apple Silicon

Flint supports macOS on Apple Silicon via the Virtualization.framework backend.

### Setup

The easiest way to set up is with the unified setup command:

```bash
# Downloads kernel and builds Alpine rootfs for macOS (requires Docker Desktop)
flint setup

# Verify assets are in place
flint setup --check
```

You can also use the platform-specific command directly for more options:

```bash
flint setup-macos --alpine-version 3.21.3 --kernel-version 1.12 --rootfs-size-mb 1024
```

Once setup is complete, `flint start` auto-detects the macOS backend and starts the daemon.

## 🔧 Linux Host Setup

Flint expects a Linux host with Firecracker, a kernel, and a rootfs image.

### Recommended: interactive setup

`flint setup` launches a wizard that installs Firecracker + jailer + kernel, builds the rootfs, and configures your storage backend:

```bash
sudo flint setup
```

The wizard will prompt you to choose a storage backend (local, S3 Files, or R2) and collect any required credentials. For CI or scripting, use `--no-interactive`:

```bash
sudo flint setup --no-interactive                 # defaults to local storage
sudo flint setup --no-interactive --version v1.10.1  # pin Firecracker version
```

Verify everything is in place:

```bash
sudo flint setup --check
```

### Manual setup (advanced)

If you prefer to run each step individually:

#### 1. Install Firecracker, jailer, and kernel

```bash
sudo flint install-deps
```

Or without Flint installed yet:

```bash
curl -fsSL https://raw.githubusercontent.com/jacquesverre/flint/main/scripts/install-deps.sh | sudo sh
```

#### 2. Build the rootfs image

Flint uses an Alpine-based rootfs with the `flintd` guest agent. The `setup-rootfs.sh` script builds it:

```bash
sudo ./setup-rootfs.sh
```

This creates a 200 MB ext4 image at `/root/firecracker-vm/rootfs.ext4`.

#### 3. Verify

You should have:

```
/root/firecracker-vm/
├── vmlinux        # Uncompressed Linux kernel
└── rootfs.ext4    # Alpine rootfs with flintd guest agent
```

Once these are in place, `flint start` will create the golden snapshot automatically on first run.

---

Built on [Firecracker](https://jacquesverre.com/firecracker).
