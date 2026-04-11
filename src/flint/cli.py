import os
import time

import click


@click.group()
def cli():
    """Flint - Firecracker VM Manager"""
    pass


@cli.command()
@click.option("--port", default=None, type=int, help="Daemon port (default: 9100)")
@click.option("--data-dir", default=None, type=str, help="Data directory for VMs (default: /microvms)")
@click.option("--state-dir", default=None, type=str, help="State directory for daemon files (default: /tmp/flint)")
@click.option("--backend", default=None, type=str, help="Backend plugin to use (e.g. firecracker, cloud-hypervisor, macos-vz)")
def start(port, data_dir, state_dir, backend):
    """Start the Flint manager daemon."""
    if port is not None:
        os.environ["FLINT_PORT"] = str(port)
    if data_dir is not None:
        os.environ["FLINT_DATA_DIR"] = data_dir
    if state_dir is not None:
        os.environ["FLINT_STATE_DIR"] = state_dir
    if backend is not None:
        os.environ["FLINT_BACKEND"] = backend
        from flint.core.backends import get_backend, names, BackendNotFound
        try:
            get_backend(backend)
        except BackendNotFound:
            click.echo(
                f"Error: unknown backend {backend!r}. Available: {', '.join(names()) or '(none installed)'}",
                err=True,
            )
            raise SystemExit(2)
    # On macOS, ensure the Python binary has the virtualization entitlement.
    # This may os.execv() — the call never returns in that case.
    import platform
    if platform.system() == "Darwin":
        from flint.core._install import ensure_vz_entitlement
        ensure_vz_entitlement()
    from flint.daemon.server import FlintDaemon
    FlintDaemon().run()


@cli.group()
def backends():
    """Inspect available backend plugins."""
    pass


@backends.command("list")
def backends_list():
    """List installed backend plugins and their preflight status."""
    from flint.core.backends import available
    infos = available()
    if not infos:
        click.echo("No backend plugins installed.")
        return
    for info in infos:
        status = "ok" if info.preflight_ok else "not-ready"
        display = info.display_name or info.name
        click.echo(f"  {info.name:20s} {display:40s} [{status}]")
        if not info.preflight_ok:
            for problem in info.preflight_problems:
                click.echo(f"      - {problem}")


@cli.command()
def app():
    """Start the Flint TUI (requires running daemon)."""
    from flint.sandbox import Sandbox
    if not Sandbox.is_daemon_running():
        click.echo("Error: Flint daemon is not running. Run 'flint start' first.", err=True)
        raise SystemExit(1)
    from flint.tui import FlintApp
    FlintApp().run()


@cli.command()
@click.argument('vm_id')
def stop(vm_id):
    """Stop a VM by ID."""
    from flint.sandbox import Sandbox
    sandbox = Sandbox.connect(vm_id)
    sandbox.kill()
    click.echo(f"Stopped VM: {vm_id}")


@cli.command("install-deps")
@click.option("--version", "fc_version", default="latest", help="Firecracker version to install (default: latest)")
@click.option("--install-dir", default="/usr/local/bin", help="Directory to install binaries (default: /usr/local/bin)")
@click.option("--kernel-dir", default="/root/firecracker-vm", help="Directory to store vmlinux (default: /root/firecracker-vm)")
@click.option("--kernel-version", default="6.1", help="Kernel major version for S3 URL (default: 6.1)")
@click.option("--skip-kernel", is_flag=True, default=False, help="Skip vmlinux download")
@click.option("--check", is_flag=True, default=False, help="Print installed versions and exit")
def install_deps(fc_version, install_dir, kernel_dir, kernel_version, skip_kernel, check):
    """Install firecracker, jailer, and vmlinux kernel."""
    from flint.core._install import check_deps, install_deps as _install_deps
    if check:
        all_present = check_deps(install_dir=install_dir, kernel_dir=kernel_dir)
        raise SystemExit(0 if all_present else 1)
    _install_deps(
        fc_version=fc_version,
        install_dir=install_dir,
        kernel_dir=kernel_dir,
        kernel_version=kernel_version,
        skip_kernel=skip_kernel,
    )


@cli.command("setup-macos")
@click.option("--vz-dir", default=None, type=str, help="Directory to store macOS VZ assets (default: Flint data dir)")
@click.option("--alpine-version", default="3.21.3", help="Alpine minirootfs version to use (default: 3.21.3)")
@click.option("--kernel-version", default="1.12", help="Firecracker CI kernel release prefix (default: 1.12)")
@click.option("--kernel-patch", default="6.1.128", help="Kernel build suffix for Firecracker CI download (default: 6.1.128)")
@click.option("--rootfs-size-mb", default=1024, type=int, help="Size of the macOS guest rootfs image in MB (default: 1024)")
@click.option("--force", is_flag=True, default=False, help="Rebuild assets even if they already exist")
@click.option("--check", is_flag=True, default=False, help="Print macOS guest asset status and exit")
def setup_macos(vz_dir, alpine_version, kernel_version, kernel_patch, rootfs_size_mb, force, check):
    """Prepare macOS Virtualization.framework guest assets for Flint."""
    from flint.core._install import check_macos_vz_assets, setup_macos_vz
    if check:
        all_present = check_macos_vz_assets(vz_dir=vz_dir)
        raise SystemExit(0 if all_present else 1)
    setup_macos_vz(
        vz_dir=vz_dir,
        alpine_version=alpine_version,
        kernel_version=kernel_version,
        kernel_patch=kernel_patch,
        rootfs_size_mb=rootfs_size_mb,
        force=force,
    )


@cli.command(name="list")
def list_vms():
    """List all VMs."""
    from flint.sandbox import Sandbox
    if not Sandbox.is_daemon_running():
        click.echo("Error: Flint daemon is not running. Run 'flint start' first.", err=True)
        raise SystemExit(1)
    sandboxes = Sandbox.list()
    if not sandboxes:
        click.echo("No VMs found.")
        return
    for sb in sandboxes:
        age = time.time() - sb.created_at
        age_str = f"{int(age)}s" if age < 60 else f"{int(age / 60)}m"
        click.echo(f"  {sb.id[:8]}  pid={sb.pid}  state={sb.state}  age={age_str}")


# ── Agent commands ─────────────────────────────────────────────────────────

@cli.group()
def agents():
    """Manage pre-packaged AI agents."""
    pass


@agents.command(name="list")
def agents_list():
    """List all available agents in the catalog."""
    from flint.agents.catalog import list_agents
    catalog = list_agents()
    if not catalog:
        click.echo("No agents available.")
        return
    click.echo("Available agents:\n")
    for defn in catalog:
        click.echo(f"  {defn.name:<12} {defn.description}")
        click.echo(f"  {'':12} repo: {defn.repo}")
        click.echo(f"  {'':12} license: {defn.license}  tags: {', '.join(defn.tags)}")
        click.echo()


@agents.command()
@click.argument("name")
def info(name):
    """Show detailed information about an agent."""
    from flint.agents.catalog import get_agent
    defn = get_agent(name)
    if defn is None:
        click.echo(f"Error: Unknown agent '{name}'. Run 'flint agents list' to see available agents.", err=True)
        raise SystemExit(1)
    click.echo(f"Name:         {defn.name}")
    click.echo(f"Description:  {defn.description}")
    click.echo(f"Repository:   {defn.repo}")
    click.echo(f"Version:      {defn.version}")
    click.echo(f"Homepage:     {defn.homepage}")
    click.echo(f"License:      {defn.license}")
    click.echo(f"Tags:         {', '.join(defn.tags)}")
    click.echo(f"Rootfs size:  {defn.rootfs_size_mb} MB")
    if defn.default_env:
        click.echo(f"Default env:")
        for k, v in defn.default_env.items():
            click.echo(f"  {k}={v}")


@agents.command()
@click.argument("name")
@click.option("--env", "-e", multiple=True, help="Environment variables (KEY=VALUE)")
@click.option("--rootfs-size", type=int, default=None, help="Override rootfs size in MB")
@click.option("--no-internet", is_flag=True, default=False, help="Disable internet access")
def deploy(name, env, rootfs_size, no_internet):
    """Build and deploy an agent in a Flint microVM."""
    from flint.sandbox import Sandbox
    if not Sandbox.is_daemon_running():
        click.echo("Error: Flint daemon is not running. Run 'flint start' first.", err=True)
        raise SystemExit(1)

    from flint.agents.catalog import get_agent
    defn = get_agent(name)
    if defn is None:
        click.echo(f"Error: Unknown agent '{name}'. Run 'flint agents list' to see available agents.", err=True)
        raise SystemExit(1)

    # Parse environment variables
    env_dict: dict[str, str] = {}
    for entry in env:
        if "=" not in entry:
            click.echo(f"Error: Invalid env format '{entry}'. Use KEY=VALUE.", err=True)
            raise SystemExit(1)
        k, v = entry.split("=", 1)
        env_dict[k] = v

    click.echo(f"Deploying {defn.name}...")
    click.echo(f"  Building template (this may take a few minutes on first run)...")

    from flint.agents.agent import Agent
    agent = Agent.deploy(
        name,
        env=env_dict or None,
        rootfs_size_mb=rootfs_size,
        allow_internet_access=not no_internet,
    )

    click.echo(f"  Agent '{agent.name}' deployed successfully!")
    click.echo(f"  Sandbox ID: {agent.sandbox.id}")
    click.echo(f"  Template:   {agent.template_info.template_id}")
    click.echo()
    click.echo(f"  Run commands:  flint agents exec {agent.sandbox.id[:8]} '<command>'")
    click.echo(f"  Stop agent:    flint stop {agent.sandbox.id[:8]}")
