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

https://github.com/user-attachments/assets/5fdbf10e-7e7a-4688-9414-5bde4d4ed428

## 🚀 Quick Start

### Prerequisites

- Linux host with [Firecracker](https://github.com/firecracker-microvm/firecracker) installed
- A rootfs image and vmlinux kernel at `/root/firecracker-vm/`
- `musl-tools`, `musl-dev`, `binutils` (for building the guest relay — auto-installed by `setup-rootfs.sh`)
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

# Pause & resume (state is preserved to disk)
sandbox.pause()
sandbox.resume()

# Auto-cleanup timeout (seconds)
sandbox.set_timeout(300)              # kill after 5 min
sandbox.set_timeout(300, policy="pause")  # pause instead of kill

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
| `POST` | `/vms/{vm_id}/pause` | Pause a VM (snapshot to disk) |
| `POST` | `/vms/{vm_id}/resume` | Resume a paused VM |
| `PATCH` | `/vms/{vm_id}` | Set timeout / timeout policy |
| `WS` | `/vms/{vm_id}/terminal` | Interactive terminal (binary frames) |

```bash
# Create a VM
curl -X POST localhost:9100/vms

# List VMs
curl localhost:9100/vms

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
- **SQLite state store**: All sandbox state is durably persisted to SQLite (WAL mode) so VMs survive daemon restarts. The daemon detaches from VMs on shutdown and reclaims them on startup.
- **Crash recovery**: On startup the daemon probes for orphaned Firecracker processes from a previous run, reconnects to healthy ones, and cleans up dead ones
- **Health monitoring**: Background thread checks process liveness every 5s and transitions dead VMs to error state
- **Pause/resume**: VMs can be paused to disk (Firecracker snapshot) and resumed later, preserving full guest state

## 📁 Project Structure

```
guest/
└── tcp-relay.c          # Static C relay: pre-spawned PTY shell over TCP
src/flint/
├── cli.py              # Click CLI commands
├── daemon/
│   └── server.py       # FastAPI daemon + lifecycle
├── sandbox.py          # Public Sandbox SDK
├── _client/
│   └── client.py       # Internal HTTP + WebSocket client
├── core/
│   ├── manager.py      # VM lifecycle (create/kill/pause/resume)
│   ├── types.py        # SandboxState enum, _SandboxEntry dataclass
│   ├── config.py       # Constants and paths
│   ├── _boot.py        # Firecracker boot sequence + BootResult
│   ├── _snapshot.py    # Golden snapshot creation
│   ├── _pool.py        # Rootfs pool management
│   ├── _tcp.py         # TCP output reader
│   ├── _state_store.py # SQLite WAL state persistence
│   ├── _state_machine.py # State transition validator
│   ├── _recovery.py    # Crash recovery engine
│   ├── _health.py      # Background health monitor
│   └── _lifecycle.py   # Timeout enforcement + error cleanup
└── tui/
    ├── app.py           # Textual app
    ├── screens/         # Home + Benchmark screens
    └── widgets/         # Sidebar, Terminal, etc.
```

## 🔧 Host Setup

Flint expects a Linux host with Firecracker, a kernel, and a rootfs image.

### 1. Install Firecracker, jailer, and kernel

If Flint is already installed:

```bash
sudo flint install-deps
```

Or without Flint installed yet:

```bash
curl -fsSL https://raw.githubusercontent.com/jacquesverre/flint/main/scripts/install-deps.sh | sudo sh
```

To pin a specific version:

```bash
FC_VERSION=v1.10.1 curl -fsSL https://raw.githubusercontent.com/jacquesverre/flint/main/scripts/install-deps.sh | sudo sh
```

To verify what is installed:

```bash
sudo flint install-deps --check
```

### 2. Build the rootfs image

Flint uses an Alpine-based rootfs with the `flintd` guest agent. The `setup-rootfs.sh` script builds it:

```bash
sudo ./setup-rootfs.sh
```

This creates a 200 MB ext4 image at `/root/firecracker-vm/rootfs.ext4`.

### 3. Verify

You should have:

```
/root/firecracker-vm/
├── vmlinux        # Uncompressed Linux kernel
└── rootfs.ext4    # Alpine rootfs with flintd guest agent
```

Once these are in place, `flint start` will create the golden snapshot automatically on first run.
