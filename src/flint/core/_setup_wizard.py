"""Interactive setup wizard with Rich rendering."""

from __future__ import annotations

import os
import platform
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text
from rich.rule import Rule

# ── Branding colours (from tui/palette.py) ──────────────────────────────────
PRIMARY = "#7d96c3"
ACCENT = "#8eb5d9"
SUCCESS = "#8fbf8f"
WARNING = "#d6b16f"
ERROR = "#d38a94"
MUTED = "#97a0b3"

LOGO = r"""
     _____ _ _       _
    |  ___| (_)_ __ | |_
    | |_  | | | '_ \| __|
    |  _| | | | | | | |_
    |_|   |_|_|_| |_|\__|
"""

# ── Storage backend metadata ────────────────────────────────────────────────

STORAGE_BACKENDS = [
    {
        "key": "local",
        "name": "Local filesystem",
        "desc": "Files stored on the VM's ext4 rootfs. No external services needed.",
        "fields": [],
    },
    {
        "key": "s3_files",
        "name": "AWS S3 Files (NFS)",
        "desc": "Mount an S3 File Gateway as NFS inside each sandbox.",
        "fields": [
            {
                "env": "FLINT_S3_FILES_NFS_ENDPOINT",
                "label": "S3 Files NFS endpoint",
                "placeholder": "fs-abc123.s3-files.us-east-1.amazonaws.com:/",
                "secret": False,
            },
        ],
    },
    {
        "key": "r2",
        "name": "Cloudflare R2",
        "desc": "Object storage with NFS overlay via the r2nfs service.",
        "fields": [
            {"env": "FLINT_R2_ACCOUNT_ID", "label": "R2 Account ID", "placeholder": "", "secret": False},
            {"env": "FLINT_R2_ACCESS_KEY_ID", "label": "R2 Access Key ID", "placeholder": "", "secret": False},
            {"env": "FLINT_R2_SECRET_ACCESS_KEY", "label": "R2 Secret Access Key", "placeholder": "", "secret": True},
            {"env": "FLINT_R2_BUCKET", "label": "R2 Bucket name", "placeholder": "flint-storage", "secret": False},
        ],
    },
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _step(console: Console, msg: str) -> None:
    console.print(f"  [{SUCCESS}]✓[/] {msg}")


def _warn(console: Console, msg: str) -> None:
    console.print(f"  [{WARNING}]![/] {msg}")


def _fail(console: Console, msg: str) -> None:
    console.print(f"  [{ERROR}]✗[/] {msg}")


# ── Wizard ───────────────────────────────────────────────────────────────────

def run_wizard(*, force: bool = False, fc_version: str = "latest") -> None:
    """Run the interactive setup wizard."""
    console = Console()
    env_vars: dict[str, str] = {}

    # ── 1. Welcome ───────────────────────────────────────────────────────
    console.print()
    console.print(
        Panel(
            Text.from_markup(
                f"[bold {PRIMARY}]{LOGO}[/]\n"
                f"[bold]Setup Wizard[/]\n\n"
                f"[{MUTED}]This wizard installs platform dependencies and\n"
                f"configures your storage backend — one step at a time.[/]"
            ),
            border_style=PRIMARY,
            padding=(0, 4),
        )
    )
    console.print()

    # ── 2. Platform detection ────────────────────────────────────────────
    system = platform.system()
    arch = platform.machine()

    console.print(Rule(f"[bold {PRIMARY}]Platform[/]", style=MUTED))
    console.print()

    if system == "Darwin":
        _step(console, f"Detected [bold]macOS[/] on [bold]{arch}[/]  →  Virtualization.framework backend")
        if arch not in ("arm64", "aarch64"):
            _fail(console, "macOS Flint requires Apple Silicon (arm64).")
            raise SystemExit(1)
    elif system == "Linux":
        _step(console, f"Detected [bold]Linux[/] on [bold]{arch}[/]  →  Firecracker backend")
        if arch not in ("x86_64", "aarch64"):
            _fail(console, f"Unsupported architecture: {arch}")
            raise SystemExit(1)
        if os.geteuid() != 0:
            console.print()
            _fail(console, "Linux setup requires root. Re-run with [bold]sudo[/].")
            raise SystemExit(1)
    else:
        _fail(console, f"Unsupported platform: {system}")
        raise SystemExit(1)

    console.print()

    # ── 3. Storage backend selection ─────────────────────────────────────
    console.print(Rule(f"[bold {PRIMARY}]Storage Backend[/]", style=MUTED))
    console.print()

    table = Table(
        show_header=True,
        header_style=f"bold {PRIMARY}",
        border_style=MUTED,
        pad_edge=True,
        expand=True,
    )
    table.add_column("#", style="bold", width=3, justify="right")
    table.add_column("Backend", style="bold", min_width=20)
    table.add_column("Description", style=MUTED)

    for i, b in enumerate(STORAGE_BACKENDS, 1):
        table.add_row(str(i), b["name"], b["desc"])

    console.print(table)
    console.print()

    choice = Prompt.ask(
        f"  [{PRIMARY}]Select a storage backend[/]",
        choices=["1", "2", "3"],
        default="1",
    )
    backend = STORAGE_BACKENDS[int(choice) - 1]

    _step(console, f"Selected [bold]{backend['name']}[/]")
    env_vars["FLINT_STORAGE_BACKEND"] = backend["key"]
    console.print()

    # ── 4. Collect backend-specific config ───────────────────────────────
    if backend["fields"]:
        console.print(Rule(f"[bold {PRIMARY}]{backend['name']} Configuration[/]", style=MUTED))
        console.print()

        for field in backend["fields"]:
            default = field["placeholder"] or None
            value = Prompt.ask(
                f"  [{PRIMARY}]{field['label']}[/]",
                default=default,
                password=field["secret"],
            )
            if value:
                env_vars[field["env"]] = value

        console.print()

    # ── 5. Confirm & install ─────────────────────────────────────────────
    console.print(Rule(f"[bold {PRIMARY}]Install Dependencies[/]", style=MUTED))
    console.print()

    if system == "Darwin":
        steps_preview = [
            "Download Linux arm64 kernel",
            "Build flintd guest agent (Go → Docker)",
            "Build Alpine rootfs image",
        ]
    else:
        steps_preview = [
            f"Download Firecracker + jailer ({fc_version})",
            "Download vmlinux kernel",
            "Download Alpine minirootfs",
            "Build flintd guest agent",
            "Build ext4 rootfs image",
        ]

    for s in steps_preview:
        console.print(f"  [{MUTED}]•[/] {s}")
    console.print()

    if not Confirm.ask(f"  [{PRIMARY}]Proceed with installation?[/]", default=True):
        console.print()
        _warn(console, "Setup cancelled.")
        raise SystemExit(0)

    console.print()

    # ── 6. Run installation ──────────────────────────────────────────────
    _run_install(console, system=system, force=force, fc_version=fc_version)

    # ── 7. Write .env file ───────────────────────────────────────────────
    env_path = _write_env_file(console, env_vars)

    # ── 8. Summary ───────────────────────────────────────────────────────
    console.print()
    console.print(Rule(f"[bold {PRIMARY}]Summary[/]", style=MUTED))
    console.print()

    summary = Table(
        show_header=False,
        border_style=MUTED,
        pad_edge=True,
        expand=True,
    )
    summary.add_column("Key", style=f"bold {PRIMARY}", min_width=16)
    summary.add_column("Value")

    summary.add_row("Platform", f"{system} {arch}")
    summary.add_row("Storage", backend["name"])

    if system == "Linux":
        summary.add_row("Firecracker", "/usr/local/bin/firecracker")
        summary.add_row("Jailer", "/usr/local/bin/jailer")
        summary.add_row("Kernel", "/root/firecracker-vm/vmlinux")
        summary.add_row("Rootfs", "/root/firecracker-vm/rootfs.ext4")
    else:
        from .config import VZ_KERNEL_PATH, VZ_ROOTFS_PATH
        summary.add_row("VZ Kernel", VZ_KERNEL_PATH)
        summary.add_row("VZ Rootfs", VZ_ROOTFS_PATH)

    if env_path:
        summary.add_row("Config", str(env_path))

    console.print(summary)
    console.print()

    # ── Next steps ───────────────────────────────────────────────────────
    next_steps = []
    if env_path:
        next_steps.append(f"[{MUTED}]1.[/] Source your config:  [bold]source {env_path}[/]")
        next_steps.append(f"[{MUTED}]2.[/] Start the daemon:   [bold]flint start[/]")
        next_steps.append(f"[{MUTED}]3.[/] Launch the TUI:     [bold]flint app[/]")
    else:
        next_steps.append(f"[{MUTED}]1.[/] Start the daemon:  [bold]flint start[/]")
        next_steps.append(f"[{MUTED}]2.[/] Launch the TUI:    [bold]flint app[/]")

    console.print(
        Panel(
            "\n".join(next_steps),
            title=f"[bold {SUCCESS}]Next Steps[/]",
            border_style=SUCCESS,
            padding=(1, 2),
        )
    )
    console.print()


def _run_install(
    console: Console,
    *,
    system: str,
    force: bool,
    fc_version: str,
) -> None:
    """Execute the actual installation with live status spinners."""
    if system == "Darwin":
        _run_macos_install(console, force=force)
    else:
        _run_linux_install(console, force=force, fc_version=fc_version)


def _run_macos_install(console: Console, *, force: bool) -> None:
    from ._install import setup_macos_vz

    with console.status(f"[{ACCENT}]Setting up macOS Virtualization.framework assets...[/]", spinner="dots"):
        setup_macos_vz(force=force)

    _step(console, "macOS guest assets installed")
    console.print()


def _run_linux_install(console: Console, *, force: bool, fc_version: str) -> None:
    from ._install import install_deps, build_linux_rootfs

    with console.status(f"[{ACCENT}]Installing Firecracker, jailer, and kernel...[/]", spinner="dots"):
        install_deps(fc_version=fc_version)

    _step(console, "Firecracker + jailer + kernel installed")

    with console.status(f"[{ACCENT}]Building Alpine rootfs with flintd guest agent...[/]", spinner="dots"):
        build_linux_rootfs(force=force)

    _step(console, "Rootfs image built")
    console.print()


def _write_env_file(console: Console, env_vars: dict[str, str]) -> Path | None:
    """Write collected config to a .env file. Returns the path, or None if skipped."""
    # Only write if there's something beyond the default local backend
    if not env_vars or (len(env_vars) == 1 and env_vars.get("FLINT_STORAGE_BACKEND") == "local"):
        return None

    env_path = Path.cwd() / ".env"
    existing_lines: list[str] = []
    existing_keys: set[str] = set()

    if env_path.exists():
        existing_lines = env_path.read_text().splitlines()
        for line in existing_lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                existing_keys.add(stripped.split("=", 1)[0])

    new_lines: list[str] = []
    for key, value in env_vars.items():
        if key not in existing_keys:
            new_lines.append(f"{key}={value}")

    if not new_lines:
        return None

    console.print()
    console.print(Rule(f"[bold {PRIMARY}]Configuration[/]", style=MUTED))
    console.print()

    for line in new_lines:
        k, v = line.split("=", 1)
        # Mask secrets
        display_v = v if "SECRET" not in k else v[:4] + "****"
        console.print(f"  [{MUTED}]{k}[/]={display_v}")

    console.print()

    if not Confirm.ask(f"  [{PRIMARY}]Write to {env_path}?[/]", default=True):
        _warn(console, "Skipped writing .env — set these variables manually before running Flint.")
        return None

    with open(env_path, "a") as f:
        if existing_lines and existing_lines[-1].strip():
            f.write("\n")
        f.write("# Flint storage configuration\n")
        for line in new_lines:
            f.write(line + "\n")

    _step(console, f"Configuration written to [bold]{env_path}[/]")
    return env_path
