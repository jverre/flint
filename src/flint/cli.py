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
    _set_daemon_env(port, data_dir, state_dir, backend)
    _check_backend(backend)
    from flint.daemon.server import FlintDaemon
    FlintDaemon().run()


def _set_daemon_env(port, data_dir, state_dir, backend) -> None:
    if port is not None:
        os.environ["FLINT_PORT"] = str(port)
    if data_dir is not None:
        os.environ["FLINT_DATA_DIR"] = data_dir
    if state_dir is not None:
        os.environ["FLINT_STATE_DIR"] = state_dir
    if backend is not None:
        os.environ["FLINT_BACKEND"] = backend


def _check_backend(backend):
    """Resolve the backend, installing its dependencies if preflight fails. Returns the plugin."""
    from flint.core.backends import resolve_backend, names, BackendNotFound
    try:
        plugin = resolve_backend()
    except BackendNotFound:
        click.echo(
            f"Error: unknown backend {backend!r}. Available: {', '.join(names()) or '(none installed)'}",
            err=True,
        )
        raise SystemExit(2)
    except RuntimeError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(2)
    problems = plugin.preflight()
    if problems:
        click.echo(f"Backend {plugin.name!r} preflight failed:", err=True)
        for p in problems:
            click.echo(f"  - {p}", err=True)
        click.echo(f"Attempting to install missing dependencies for {plugin.name!r}...", err=True)
        try:
            plugin.install_dependencies()
        except NotImplementedError:
            click.echo(
                f"Error: backend {plugin.name!r} has no install_dependencies hook. "
                f"Fix the problems above and re-run.",
                err=True,
            )
            raise SystemExit(2)
        except Exception as exc:
            click.echo(f"Error: install_dependencies failed: {exc}", err=True)
            raise SystemExit(2)
        problems = plugin.preflight()
        if problems:
            click.echo("Still not ready after install:", err=True)
            for p in problems:
                click.echo(f"  - {p}", err=True)
            raise SystemExit(2)
        click.echo(f"Backend {plugin.name!r} ready.", err=True)
    # On macOS, ensure the Python binary has the virtualization entitlement.
    # This may os.execv() — the call never returns in that case.
    import platform
    if platform.system() == "Darwin":
        from flint.core._install import ensure_vz_entitlement
        ensure_vz_entitlement()
    return plugin


def _cluster_binary() -> str:
    import shutil
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.environ.get("FLINT_CLUSTER_BIN", ""), shutil.which("flint-cluster") or "",
                 os.path.join(here, "..", "..", "cluster", "flint-cluster")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return os.path.abspath(cand)
    raise click.ClickException("flint-cluster binary not found (set FLINT_CLUSTER_BIN or build cluster/)")


@cli.command()
@click.option("--join", "token", default=None, envvar="FLINT_JOIN_TOKEN", help="Join token from the control plane (first start only)")
@click.option("--advertise", required=True, envvar="FLINT_ADVERTISE_ADDR", help="Address the control plane and other hosts reach this host on ('auto': the default route's source address)")
@click.option("--listen", default="0.0.0.0", envvar="FLINT_HOST", help="Address the agent API binds to")
@click.option("--port", default=9400, type=int, envvar="FLINT_PORT", help="Agent API port (mTLS)")
@click.option("--snapd-port", default=9501, type=int, envvar="FLINT_SNAPD_PORT", help="Port of this host's snapd peer server")
@click.option("--name", default=None, envvar="FLINT_HOST_NAME", help="Host name (default: hostname)")
@click.option("--zone", default="", envvar="FLINT_ZONE", help="Failure domain, e.g. the cloud zone")
@click.option("--label", "labels", multiple=True, help="key=value host label (repeatable)")
@click.option("--data-dir", default=None, type=str)
@click.option("--state-dir", default=None, type=str)
@click.option("--backend", default=None, type=str)
def agent(token, advertise, listen, port, snapd_port, name, zone, labels, data_dir, state_dir, backend):
    """Run this machine as a cluster host agent.

    The first start enrols the host with --join (a token from
    `flint cluster join-token`); later starts reuse the stored identity. Run
    `flint-cluster snapd` next to it (the systemd units in deploy/ do both).
    """
    import socket
    import subprocess
    if advertise == "auto":
        # The source address of the default route: the host's primary address.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            advertise = s.getsockname()[0]
    _set_daemon_env(port, data_dir, state_dir, backend)
    os.environ["FLINT_HOST"] = listen
    os.environ["FLINT_CLUSTER"] = "1"
    plugin = _check_backend(backend)
    from flint.core import config
    identity = config.CLUSTER_IDENTITY_DIR
    if not os.path.exists(os.path.join(identity, "host.json")):
        if not token:
            raise click.ClickException(f"this host hasn't joined a cluster ({identity}); pass --join <token>")
        from flint.cluster.agent import host_class
        cmd = [_cluster_binary(), "join", "--token", token, "--identity", identity,
               "--name", name or socket.gethostname(),
               "--agent-url", f"https://{advertise}:{port}", "--snapd-url", f"https://{advertise}:{snapd_port}",
               "--zone", zone, "--backend", plugin.kind, "--host-class", host_class(plugin)]
        for label in labels:
            cmd += ["--label", label]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise click.ClickException(f"join failed: {result.stderr.strip() or result.stdout.strip()}")
        click.echo(f"Joined the cluster as {result.stdout.strip()}", err=True)
    from flint.daemon.server import FlintDaemon
    FlintDaemon().run()


# ── Cluster administration (talks to the control plane: FLINT_URL, FLINT_API_KEY) ──

def _cp(method: str, path: str, **kwargs):
    import httpx
    from flint._client.client import auth_headers
    from flint.core.config import DAEMON_URL
    resp = httpx.request(method, f"{DAEMON_URL}{path}", headers=auth_headers(), timeout=60.0, **kwargs)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise click.ClickException(f"{resp.status_code}: {detail}")
    return resp.json()


@cli.group()
def cluster():
    """Manage a Flint cluster (set FLINT_URL and FLINT_API_KEY to the control plane)."""


@cluster.command("hosts")
@click.option("-o", "--output", type=click.Choice(["table", "json"]), default="table")
def cluster_hosts(output):
    """List hosts."""
    import json as _json
    hosts = _cp("GET", "/v1/hosts")["hosts"]
    if output == "json":
        click.echo(_json.dumps(hosts, indent=2))
        return
    click.echo(f"{'ID':18s} {'NAME':16s} {'ZONE':10s} {'STATE':12s} {'VMS':>5s}  CORDONED")
    for h in hosts:
        cap = h.get("capacity") or {}
        click.echo(f"{h['id']:18s} {h['name'][:16]:16s} {h.get('zone','')[:10]:10s} {h['state']:12s} "
                   f"{h.get('active_vms', 0):>2d}/{cap.get('max_vms', 0):<2d}  {'yes' if h.get('cordoned') else 'no'}")


@cluster.command("join-token")
@click.option("--zone", default="", help="Zone the joining host is in")
@click.option("--ttl", default=3600, type=int, help="Seconds the token is valid")
def cluster_join_token(zone, ttl):
    """Print a single-use token for `flint agent --join`."""
    click.echo(_cp("POST", "/v1/join-tokens", json={"zone": zone, "ttl_seconds": ttl})["token"])


@cluster.command("cordon")
@click.argument("host_id")
def cluster_cordon(host_id):
    """Stop placing new VMs on a host."""
    _cp("POST", f"/v1/hosts/{host_id}/cordon")
    click.echo(f"{host_id} cordoned")


@cluster.command("uncordon")
@click.argument("host_id")
def cluster_uncordon(host_id):
    """Return a host to service."""
    _cp("POST", f"/v1/hosts/{host_id}/uncordon")
    click.echo(f"{host_id} uncordoned")


@cluster.command("drain")
@click.argument("host_id")
@click.option("--wait/--no-wait", default=True)
def cluster_drain(host_id, wait):
    """Move every VM off a host (live state preserved), then mark it drained."""
    _cp("POST", f"/v1/hosts/{host_id}/drain")
    while wait:
        h = _cp("GET", f"/v1/hosts/{host_id}")["host"]
        click.echo(f"\r{h['state']}: {len(h.get('vms') or [])} VMs left   ", nl=False)
        if h["state"] == "drained":
            click.echo()
            break
        time.sleep(2)


@cluster.command("token")
@click.option("--kind", type=click.Choice(["service", "admin"]), default="service")
@click.option("--name", default="")
@click.option("--tenant", "tenants", multiple=True, help="Restrict a service token to these tenants")
def cluster_token(kind, name, tenants):
    """Create an API token (printed once)."""
    click.echo(_cp("POST", "/v1/tokens", json={"kind": kind, "name": name, "tenants": list(tenants)})["token"])


@cluster.command("vmsets")
def cluster_vmsets():
    """List VM sets."""
    for vs in _cp("GET", "/v1/vmsets")["vmsets"]:
        st = vs["status"]
        click.echo(f"{vs['id']:18s} {vs['name'][:20]:20s} {vs['tenant'][:12]:12s} "
                   f"{st['running']}/{vs['spec']['replicas']} running, {st['standby']} standby  {st['by_zone']}")


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
@click.option("--backend", default="firecracker", show_default=True, help="Backend whose hypervisor to install (firecracker, cloud-hypervisor)")
@click.option("--version", "version", default="latest", help="Hypervisor version to install (default: latest)")
@click.option("--install-dir", default="/usr/local/bin", help="Directory to install binaries (default: /usr/local/bin)")
@click.option("--kernel-dir", default="/root/firecracker-vm", help="Directory to store vmlinux (default: /root/firecracker-vm)")
@click.option("--kernel-release", default=None, help="Flint guest kernel release tag (default: the one this version of Flint was tested with)")
@click.option("--skip-kernel", is_flag=True, default=False, help="Skip vmlinux download")
@click.option("--check", is_flag=True, default=False, help="Print installed versions and exit")
def install_deps(backend, version, install_dir, kernel_dir, kernel_release, skip_kernel, check):
    """Install a backend's hypervisor and the guest kernel."""
    from flint.core._install import check_deps
    from flint.core.backends import BackendNotFound, get_backend, names
    try:
        plugin = get_backend(backend)
    except BackendNotFound:
        click.echo(f"Error: unknown backend {backend!r}. Available: {', '.join(names())}", err=True)
        raise SystemExit(2)
    if check:
        if plugin.name == "firecracker":
            raise SystemExit(0 if check_deps(install_dir=install_dir, kernel_dir=kernel_dir) else 1)
        problems = plugin.preflight()
        for p in problems:
            click.echo(f"  - {p}")
        raise SystemExit(1 if problems else 0)
    if os.geteuid() != 0:
        click.echo("Error: this command must be run as root (sudo flint install-deps)", err=True)
        raise SystemExit(1)
    try:
        plugin.install_dependencies(
            version=version,
            install_dir=install_dir,
            kernel_dir=kernel_dir,
            kernel_release=kernel_release,
            skip_kernel=skip_kernel,
        )
    except NotImplementedError:
        click.echo(f"Error: backend {plugin.name!r} has no install_dependencies hook", err=True)
        raise SystemExit(2)
    except Exception as exc:
        click.echo(f"Error: installing {plugin.name!r} dependencies failed: {exc}", err=True)
        raise SystemExit(1)


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


# ── Volume commands ────────────────────────────────────────────────────────

def _daemon_client():
    from flint._client.client import DaemonClient
    if not DaemonClient.is_daemon_running():
        click.echo("Error: Flint daemon is not running. Run 'flint start' first.", err=True)
        raise SystemExit(1)
    return DaemonClient()


def _volume_call(fn, *args, **kwargs):
    import httpx
    try:
        return fn(*args, **kwargs)
    except httpx.HTTPStatusError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)


@cli.group()
def volumes():
    """Shared volumes you mount into running sandboxes."""


@volumes.command("list")
def volumes_list():
    """List volumes."""
    vols = _volume_call(_daemon_client().list_volumes)
    if not vols:
        click.echo("No volumes.")
    for v in vols:
        size = f"{v['size_gib']} GiB" if v.get("size_gib") else "-"
        flags = " read-only" if v.get("readonly") else ""
        click.echo(f"  {v['id']}  {v['name']:<24} {v.get('kind', 'dir'):<5} {size:>9}{flags}")


@volumes.command("create")
@click.argument("name")
@click.option("--size", "size_gib", type=int, default=None, help="Size in GiB (sparse; default 10)")
def volumes_create(name, size_gib):
    """Create a volume."""
    v = _volume_call(_daemon_client().create_volume, name, size_gib)
    click.echo(f"Created volume {v['name']} ({v['id']})")


@volumes.command("snapshot")
@click.argument("volume")
@click.argument("name")
def volumes_snapshot(volume, name):
    """Make NAME, a read-only copy of VOLUME as it is now."""
    v = _volume_call(_daemon_client().snapshot_volume, volume, name)
    click.echo(f"Snapshot {v['name']} ({v['id']})")


@volumes.command("fork")
@click.argument("volume")
@click.argument("name")
def volumes_fork(volume, name):
    """Make NAME, a writable copy of VOLUME as it is now."""
    v = _volume_call(_daemon_client().fork_volume, volume, name)
    click.echo(f"Forked {v['name']} ({v['id']})")


@volumes.command("delete")
@click.argument("volume")
def volumes_delete(volume):
    """Delete a volume (by name or ID) and its files."""
    _volume_call(_daemon_client().delete_volume, volume)
    click.echo(f"Deleted volume {volume}")


@volumes.command("mount")
@click.argument("vm_id")
@click.argument("volume")
@click.argument("path")
@click.option("--subpath", default="", help="Mount only this directory of the volume")
@click.option("--readonly", "--ro", is_flag=True, default=False, help="Mount read-only")
@click.option("--exclusive", is_flag=True, default=False, help="Mount as this sandbox's own disk (fast; nobody else can mount it)")
def volumes_mount(vm_id, volume, path, subpath, readonly, exclusive):
    """Mount VOLUME at PATH inside running sandbox VM_ID."""
    _volume_call(_daemon_client().mount, vm_id, volume, path, subpath=subpath, readonly=readonly, exclusive=exclusive)
    mode = "exclusive" if exclusive else ("ro" if readonly else "rw")
    click.echo(f"Mounted {volume}:/{subpath} at {path} ({mode})")


@volumes.command("unmount")
@click.argument("vm_id")
@click.argument("path")
def volumes_unmount(vm_id, path):
    """Unmount the volume at PATH in sandbox VM_ID."""
    _volume_call(_daemon_client().unmount, vm_id, path)
    click.echo(f"Unmounted {path}")


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
@click.argument("name", required=False, default=None)
@click.option("--all", "build_all", is_flag=True, default=False, help="Build all agents in the catalog")
@click.option("--force", is_flag=True, default=False, help="Rebuild even if already cached")
@click.option("--rootfs-size", type=int, default=None, help="Override rootfs size in MB")
def build(name, build_all, force, rootfs_size):
    """Pre-build agent template(s) for instant deploys.

    Build a single agent by name, or use --all to build every agent in the
    catalog. Subsequent deploys will boot from the cached snapshot in
    milliseconds instead of rebuilding the image from scratch.
    """
    from flint.sandbox import Sandbox
    if not Sandbox.is_daemon_running():
        click.echo("Error: Flint daemon is not running. Run 'flint start' first.", err=True)
        raise SystemExit(1)

    if not build_all and name is None:
        click.echo("Error: Provide an agent name or use --all.", err=True)
        raise SystemExit(1)

    from flint.agents.agent import Agent

    if build_all:
        from flint.agents.catalog import list_agents
        targets = [defn.name for defn in list_agents()]
        click.echo(f"Building {len(targets)} agent template(s)...")
    else:
        from flint.agents.catalog import get_agent
        if get_agent(name) is None:
            click.echo(f"Error: Unknown agent '{name}'. Run 'flint agents list' to see available agents.", err=True)
            raise SystemExit(1)
        targets = [name]

    for agent_name in targets:
        status = "force-rebuilding" if force else "building"
        click.echo(f"  [{status}] {agent_name}...")
        info = Agent.build(agent_name, rootfs_size_mb=rootfs_size, force=force)
        click.echo(f"  [ready]    {agent_name} -> {info.template_id}")

    click.echo(f"\nDone! {len(targets)} agent template(s) ready for instant deploy.")


@agents.command()
@click.argument("name")
@click.option("--env", "-e", multiple=True, help="Environment variables (KEY=VALUE)")
@click.option("--rootfs-size", type=int, default=None, help="Override rootfs size in MB")
@click.option("--no-internet", is_flag=True, default=False, help="Disable internet access")
@click.option("--force-build", is_flag=True, default=False, help="Force template rebuild even if cached")
def deploy(name, env, rootfs_size, no_internet, force_build):
    """Build (if needed) and deploy an agent in a Flint microVM."""
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
        force_build=force_build,
    )

    click.echo(f"  Agent '{agent.name}' deployed successfully!")
    click.echo(f"  Sandbox ID: {agent.sandbox.id}")
    click.echo(f"  Template:   {agent.template_info.template_id}")
    click.echo()
    click.echo(f"  Run commands:  flint agents exec {agent.sandbox.id[:8]} '<command>'")
    click.echo(f"  Stop agent:    flint stop {agent.sandbox.id[:8]}")
