<p align="center">
  <h1 align="center">Flint</h1>
  <p align="center">Lightning-fast Firecracker microVM management with an interactive TUI</p>
</p>

<p align="center">
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-python-sdk">SDK</a> •
  <a href="#-tui">TUI</a> •
  <a href="#-api">API</a> •
  <a href="#-benchmarks">Benchmarks</a> •
  <a href="#-architecture">Architecture</a> •
  <a href="#-host-setup">Host Setup</a>
</p>

---

Flint spins up Firecracker microVMs in milliseconds from a pre-built golden snapshot. It runs as a daemon with a REST API, and ships with a terminal UI for interactive VM management.

<p align="center">
https://github.com/user-attachments/assets/5fdbf10e-7e7a-4688-9414-5bde4d4ed428
</p>

## 🚀 Quick Start

### Prerequisites

- Linux host with [Firecracker](https://github.com/firecracker-microvm/firecracker) installed
- A rootfs image and vmlinux kernel at `/root/firecracker-vm/`
- Python 3.12+

### Install

```bash
git clone https://github.com/jacquesverre/flint.git
cd flint
uv sync
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

# Interactive PTY
terminal = sandbox.pty.create(
    on_data=lambda data: print(data.decode(), end=''),
)
terminal.send_input("ls -la\n")
terminal.kill()

# Properties
sandbox.id            # str
sandbox.state         # str
sandbox.is_running()  # bool

# List & connect to existing sandboxes
sandboxes = Sandbox.list()
sandbox = Sandbox.connect(vm_id)

# Clean up
sandbox.kill()
```

> **Note:** The daemon must be running (`flint start`) before using the SDK.

## 💻 TUI

The TUI connects to the daemon and gives you an interactive terminal into each VM.

| Key | Action |
|-----|--------|
| `s` | Start a new VM |
| `Backspace` / `Delete` | Kill selected VM |
| `Tab` | Toggle focus between sidebar and terminal |
| `b` | Run benchmark |

> **Tip:** The sidebar auto-refreshes. You can also manage VMs from the CLI while the TUI is running.

### CLI

```bash
uv run flint list              # List running VMs
uv run flint stop <vm_id>      # Kill a VM by ID
```

## 📡 API

The daemon exposes a REST API and WebSocket endpoint on `localhost:9100`:

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check + golden snapshot status |
| `POST` | `/vms` | Create a new VM |
| `GET` | `/vms` | List all VMs |
| `GET` | `/vms/{vm_id}` | Get VM details |
| `DELETE` | `/vms/{vm_id}` | Kill a VM |
| `WS` | `/vms/{vm_id}/terminal` | Interactive terminal (binary frames) |

```bash
# Create a VM
curl -X POST localhost:9100/vms

# List VMs
curl localhost:9100/vms

# Kill a VM
curl -X DELETE localhost:9100/vms/<vm_id>
```

## ⚡ Benchmarks

Flint includes a built-in benchmark mode that boots N VMs sequentially, measures time-to-interactive for each, and breaks down per-step timings.

<!-- TODO: replace with actual screenshot -->
<p align="center">
  <img src="assets/benchmark-screenshot.png" alt="Flint benchmark — booting 16 VMs with per-step timing breakdown" width="800">
</p>

Press `b` in the TUI to run a benchmark. Results include:

- **Per-VM TTI** (time-to-interactive) with min/avg/median/p95/p99/max
- **Per-step breakdown**: rootfs copy, netns setup, Firecracker popen, API ready wait, snapshot load, drive patch, VM resume, TCP connect, first command
- **Throughput**: VMs/second

## 🏗️ Architecture

Flint is split into two processes with a strict separation:

```
┌─────────────┐
│  flint app  │  TUI with terminal emulation
│  (TUI)      │
└──────┬──────┘
       │
       │         Sandbox SDK
       ▼         (HTTP + WebSocket)
┌──────────────┐                    ┌─────────────────┐
│   Sandbox    │ ◄────────────────► │  flint start    │
│ (Python SDK) │  localhost:9100    │  (daemon)       │
└──────────────┘                    │                 │
       ▲                            │  FastAPI        │
       │                            │  SandboxManager │
┌──────┴──────┐                     │  Rootfs pool    │
│  flint list │  CLI commands       └─────────────────┘
│  flint stop │
└─────────────┘
```

- **Daemon** (`flint start`): Manages VM lifecycle — golden snapshot creation, rootfs pool, Firecracker process management, TCP connections. Exposes REST + WebSocket API.
- **SDK** (`Sandbox`): E2B-style Python SDK for programmatic VM management. Used by both the TUI and CLI internally — also available for your own applications.
- **TUI** (`flint app`): Interactive terminal UI built on the SDK. Handles terminal emulation (pyte) client-side.
- **CLI** (`flint list`, `flint stop`): Stateless commands built on the SDK.

### Key Design Decisions

- **Golden snapshot**: A single pre-booted VM snapshot that all new VMs are cloned from, enabling millisecond boot times
- **Rootfs pool**: Pre-copied rootfs images ready to go, eliminating copy latency at VM creation time
- **WebSocket terminal**: Raw binary frames over WebSocket for terminal I/O — no base64, no polling
- **Atomic state file**: Daemon writes `/tmp/flint/state.json` after every state change for crash diagnostics

## 📁 Project Structure

```
src/flint/
├── cli.py              # Click CLI commands
├── daemon/
│   └── server.py       # FastAPI daemon + lifecycle
├── sandbox.py          # Public Sandbox SDK
├── _client/
│   └── client.py       # Internal HTTP + WebSocket client
├── core/
│   ├── manager.py      # VM lifecycle (create/kill)
│   ├── types.py        # _SandboxEntry dataclass
│   ├── config.py       # Constants and paths
│   ├── _boot.py        # Firecracker boot sequence
│   ├── _snapshot.py    # Golden snapshot creation
│   ├── _pool.py        # Rootfs pool management
│   └── _tcp.py         # TCP output reader
└── tui/
    ├── app.py           # Textual app
    ├── screens/         # Home + Benchmark screens
    └── widgets/         # Sidebar, Terminal, etc.
```

## 🔧 Host Setup

Flint expects a Linux host with Firecracker, a kernel, and a rootfs image. Follow these steps to set everything up.

### 1. Install Firecracker

Download the latest release from GitHub:

```bash
ARCH="$(uname -m)"
release_url="https://github.com/firecracker-microvm/firecracker/releases"
latest=$(basename $(curl -fsSLI -o /dev/null -w %{url_effective} ${release_url}/latest))
curl -L ${release_url}/download/${latest}/firecracker-${latest}-${ARCH}.tgz | tar -xz

sudo mv release-${latest}-${ARCH}/firecracker-${latest}-${ARCH} /usr/local/bin/firecracker
rm -rf release-${latest}-${ARCH}

firecracker --version
```

### 2. Get a Linux kernel

Firecracker needs an uncompressed `vmlinux` kernel. You can grab one from the Firecracker CI builds:

```bash
sudo mkdir -p /root/firecracker-vm
KERNEL_VERSION="6.1"
ARCH="$(uname -m)"
curl -fsSL -o /root/firecracker-vm/vmlinux \
  "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v${KERNEL_VERSION}/${ARCH}/vmlinux-5.10.217"
```

> **Note:** Check the [Firecracker docs](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md) for the latest recommended kernel version.

### 3. Build the rootfs image

Flint uses an Alpine-based rootfs with `socat` for the in-guest TCP shell server. The `setup-rootfs.sh` script builds it:

```bash
# Download Alpine minirootfs
curl -fsSL -o /root/firecracker-vm/alpine-minirootfs-3.21.3-x86_64.tar.gz \
  "https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/x86_64/alpine-minirootfs-3.21.3-x86_64.tar.gz"

# Build the rootfs (requires root)
sudo ./setup-rootfs.sh
```

This creates a 200 MB ext4 image at `/root/firecracker-vm/rootfs.ext4` containing Alpine with `socat` and a network init script that starts the TCP shell on port 5000.

### 4. Verify

You should have two files:

```
/root/firecracker-vm/
├── vmlinux        # Uncompressed Linux kernel
└── rootfs.ext4    # Alpine rootfs with socat + init script
```

Once these are in place, `flint start` will create the golden snapshot automatically on first run.
