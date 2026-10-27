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
    """Call the daemon or control plane at FLINT_URL; exit with its error."""
    from flint._client.client import auth_headers
    from flint._client.transport import http_client
    from flint.core.config import DAEMON_URL
    _require_daemon()
    with http_client(DAEMON_URL, headers=auth_headers(), timeout=60.0) as c:
        resp = c.request(method, path, **kwargs)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise click.ClickException(f"{resp.status_code}: {detail}")
    return resp.json()


def _require_daemon() -> None:
    """Exit with a useful message when the daemon can't be reached."""
    from flint._client.client import DaemonClient
    from flint._client.transport import socket_path
    from flint.core.config import DAEMON_URL
    if DaemonClient.is_daemon_running():
        return
    path = socket_path(DAEMON_URL)
    if path and os.path.exists(path) and not os.access(path, os.R_OK | os.W_OK):
        raise click.ClickException(
            f"can't open the daemon's socket {path}: permission denied. Run as the daemon's user (e.g. with "
            "sudo), join the `flint` group, or use TCP: FLINT_URL=http://127.0.0.1:9100 FLINT_API_KEY=<token>")
    raise click.ClickException(f"Flint daemon is not running at {DAEMON_URL}. Run 'flint start' first.")


# ── API tokens and the web UI ──────────────────────────────────────────────

@cli.group()
def token():
    """Manage API tokens (TCP clients need one; the daemon's socket doesn't)."""


@token.command("create")
@click.option("--kind", type=click.Choice(["service", "admin"]), default="service")
@click.option("--name", default="")
@click.option("--tenant", "tenants", multiple=True, help="Pin a service token to these tenants (repeatable)")
def token_create(kind, name, tenants):
    """Create a token. It is printed once and can't be shown again."""
    click.echo(_cp("POST", "/v1/tokens", json={"kind": kind, "name": name, "tenants": list(tenants)})["token"])


@token.command("list")
def token_list():
    """List tokens (never their secrets)."""
    for t in _cp("GET", "/v1/tokens")["tokens"]:
        click.echo(f"{t['id']:14s} {t['kind']:8s} {t['name'][:24]:24s} {','.join(t['tenants']) or '(any tenant)'}")


@token.command("revoke")
@click.argument("token_id")
def token_revoke(token_id):
    """Revoke a token by its ID."""
    _cp("DELETE", f"/v1/tokens/{token_id}")
    click.echo(f"{token_id} revoked")


def _event_line(e: dict) -> str:
    when = time.strftime("%H:%M:%S", time.localtime(e.get("ts", 0)))
    vm = (e.get("vm_id") or (e.get("vm") or {}).get("vm_id") or "")[:8]
    who = e.get("actor_name") or e.get("actor", "")
    detail = ""
    if e.get("type") == "vm.exec":
        detail = f"{e.get('cmd', '')!r} -> {e.get('exit_code')} ({e.get('duration_ms')} ms)"
    elif e.get("type") == "vm.state_changed":
        detail = f"{e.get('from')} -> {e.get('to')}"
    return f"{when}  {e.get('seq', ''):>6}  {e.get('type', ''):20s} {vm:8s}  {e.get('tenant', ''):12s} {who:24s} {detail}"


@cli.command()
@click.option("--vm", "vm_id", default="", help="Only this VM's events")
@click.option("--tenant", default="")
@click.option("--type", "type_", default="", help="e.g. vm.exec")
@click.option("--since", default=0, type=int, help="After this seq")
@click.option("-f", "--follow", is_flag=True, help="Keep printing new events")
@click.option("-o", "--output", type=click.Choice(["table", "json"]), default="table")
def events(vm_id, tenant, type_, since, follow, output):
    """Stored events: lifecycle changes, execs and more, with who caused them."""
    import json as _json
    while True:
        page = _cp("GET", "/v1/events", params={"since": since, "vm_id": vm_id, "tenant": tenant, "type": type_})
        for e in page["events"]:
            click.echo(_json.dumps(e) if output == "json" else _event_line(e))
        if page["next"] > since:
            since = page["next"]
            continue
        if not follow:
            return
        time.sleep(1)


@cli.command()
@click.option("--since", default=0, type=int)
@click.option("--verify", is_flag=True, help="Recompute the hash chain instead of listing")
def audit(since, verify):
    """The audit log (admin): every API call that changed something, hash-chained."""
    if verify:
        r = _cp("GET", "/v1/audit/verify")
        if not r["ok"]:
            raise click.ClickException(f"audit log broken at entry {r['broken_at']}: {r['why']} ({r['count']} entries checked)")
        click.echo(f"audit log intact: {r['count']} entries, last hash {r['last_hash'][:16]}")
        return
    while True:
        page = _cp("GET", "/v1/audit", params={"since": since})
        for a in page["entries"]:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a["ts_ms"] / 1000))
            click.echo(f"{when}  {a['seq']:>6}  {a['status']}  {a['method']:6s} {a['path']:40s} {a['actor_name'] or a['actor']}")
        if not page["entries"] or page["next"] == since:
            return
        since = page["next"]


@cli.group()
def quota():
    """Per-tenant limits: VMs, vCPUs, memory and volume space."""


def _show_quota(q: dict) -> None:
    for label, used, cap, unit in (("VMs", q["vms"], q["max_vms"], ""), ("vCPUs", q["vcpus"], q["max_vcpus"], ""),
                                   ("Memory", q["memory_mib"], q["max_memory_mib"], " MiB"),
                                   ("Volumes", q["volume_gib"], q["max_volume_gib"], " GiB")):
        click.echo(f"  {label:8s} {used}{unit} of {f'{cap}{unit}' if cap else 'no limit'}")


@quota.command("show")
@click.argument("tenant")
def quota_show(tenant):
    """Show a tenant's limits and use."""
    _show_quota(_cp("GET", f"/v1/tenants/{tenant}/quota"))


@quota.command("set")
@click.argument("tenant")
@click.option("--max-vms", type=int)
@click.option("--max-vcpus", type=int)
@click.option("--max-memory-mib", type=int)
@click.option("--max-volume-gib", type=int)
def quota_set(tenant, **limits):
    """Set some of a tenant's limits (0 = no limit; the others stay)."""
    changes = {k: v for k, v in limits.items() if v is not None}
    if not changes:
        raise click.UsageError("give at least one limit")
    _show_quota(_cp("PUT", f"/v1/tenants/{tenant}/quota", json=changes))


@cli.command()
@click.option("--provider", default=None, help="Identity provider ID (default: the only one)")
@click.option("--open/--no-open", "open_browser", default=True, help="Open the sign-in page in a browser")
def login(provider, open_browser):
    """Sign in to the Flint at FLINT_URL with its identity provider (SSO)."""
    import http.server
    import json as _json
    import secrets
    import threading
    import webbrowser
    import httpx
    from urllib.parse import parse_qs, urlencode, urlsplit
    from flint.core.config import CREDENTIALS_PATH, DAEMON_URL
    from flint.daemon.sso import pkce_challenge
    if DAEMON_URL.startswith("unix://"):
        raise click.ClickException("the daemon's socket needs no sign-in; set FLINT_URL to sign in to a server over HTTP")
    providers = httpx.get(f"{DAEMON_URL}/auth/providers", timeout=10).json().get("providers", [])
    if not providers:
        raise click.ClickException(f"{DAEMON_URL} has no identity provider set up; use an API token (FLINT_API_KEY)")
    ids = [p["id"] for p in providers]
    if provider is None:
        if len(ids) > 1:
            raise click.ClickException(f"choose a provider with --provider: {', '.join(ids)}")
        provider = ids[0]
    elif provider not in ids:
        raise click.ClickException(f"no provider {provider!r}; there are: {', '.join(ids)}")

    verifier = secrets.token_urlsafe(48)
    got: dict = {}
    done = threading.Event()

    class _Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = parse_qs(urlsplit(self.path).query)
            if urlsplit(self.path).path == "/callback" and q.get("code"):
                got["code"] = q["code"][0]
                done.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<!doctype html><title>Flint</title><p>Signed in. You can close this tab.</p>")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Callback)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    query = urlencode({"cli_port": server.server_port, "cli_challenge": pkce_challenge(verifier)})
    url = f"{DAEMON_URL}/auth/login/{provider}?{query}"
    click.echo(f"Sign in at: {url}")
    if open_browser:
        webbrowser.open(url)
    if not done.wait(300):
        server.shutdown()
        raise click.ClickException("no sign-in within 5 minutes")
    server.shutdown()
    resp = httpx.post(f"{DAEMON_URL}/auth/cli/exchange", json={"code": got["code"], "verifier": verifier}, timeout=10)
    if resp.status_code != 200:
        raise click.ClickException(f"sign-in failed: {resp.text}")
    session = resp.json()
    try:
        with open(CREDENTIALS_PATH) as f:
            creds = _json.load(f)
    except (OSError, ValueError):
        creds = {}
    creds[DAEMON_URL] = {"token": session["token"], "expires_at": session.get("expires_at"), "identity": session.get("identity")}
    os.makedirs(os.path.dirname(CREDENTIALS_PATH), mode=0o700, exist_ok=True)
    fd = os.open(CREDENTIALS_PATH + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        _json.dump(creds, f)
    os.replace(CREDENTIALS_PATH + ".tmp", CREDENTIALS_PATH)
    scope = f" for {', '.join(session['tenants'])}" if session.get("tenants") else ""
    click.echo(f"Signed in to {DAEMON_URL} as {session.get('identity')} ({session.get('kind')}{scope})")


@cli.command()
def logout():
    """End the sign-in session `flint login` started for FLINT_URL."""
    import json as _json
    import httpx
    from flint.core.config import CREDENTIALS_PATH, DAEMON_URL, stored_token
    tok = stored_token(DAEMON_URL)
    if tok:
        try:
            httpx.post(f"{DAEMON_URL}/auth/logout", headers={"Authorization": f"Bearer {tok}"}, timeout=10)
        except httpx.HTTPError:
            pass
    try:
        with open(CREDENTIALS_PATH) as f:
            creds = _json.load(f)
        creds.pop(DAEMON_URL, None)
        with open(CREDENTIALS_PATH, "w") as f:
            _json.dump(creds, f)
    except (OSError, ValueError):
        pass
    click.echo(f"Signed out of {DAEMON_URL}")


@cli.command()
@click.option("--open/--no-open", "open_browser", default=True, help="Open the URL in a browser")
def ui(open_browser):
    """Sign in to the local daemon's web UI with its admin token."""
    from flint.core.config import ADMIN_TOKEN_PATH, DAEMON_PORT
    try:
        with open(ADMIN_TOKEN_PATH) as f:
            tok = f.read().strip()
    except PermissionError:
        raise click.ClickException(f"can't read {ADMIN_TOKEN_PATH}: run as the daemon's user (e.g. with sudo)")
    except FileNotFoundError:
        raise click.ClickException(f"no admin token at {ADMIN_TOKEN_PATH}: start the daemon first ('flint start')")
    url = f"http://127.0.0.1:{DAEMON_PORT}/ui/#token={tok}"
    click.echo(url)
    if open_browser:
        import webbrowser
        webbrowser.open(url)


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


@cli.command(name="migration-gateway")
def migration_gateway():
    """Run the stable migration gateway after provisioning its identity and underlay."""
    from flint.cluster.gateway import main
    main()


@cli.command(name="init-rootfs-pool")
@click.argument("directory",type=click.Path(exists=True,file_okay=False))
def init_rootfs_pool(directory):
    """Initialise an already mounted shared POSIX root disk pool once."""
    from flint.core._shared_rootfs import SharedRootfs
    click.echo(SharedRootfs.initialize(directory).pool_id)


@cli.command(name="agent-handover")
def agent_handover():
    """Hand bounded upgrade lease renewal to snapd before stopping the agent."""
    from flint.cluster.snapd import Snapd
    from flint.core import config
    Snapd(config.SNAPD_SOCKET)._call("POST", "/v1/handover", timeout=10)
    click.echo("snapd owns lease renewal for at most 120 seconds")


@cli.command()
def app():
    """Start the Flint TUI (requires running daemon)."""
    _require_daemon()
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
    vms = _daemon_client().list()
    if not vms:
        click.echo("No VMs found.")
        return
    for vm in vms:
        age = time.time() - vm.get("created_at", time.time())
        age_str = f"{int(age)}s" if age < 60 else f"{int(age / 60)}m"
        click.echo(f"  {vm['vm_id'][:8]}  tenant={vm.get('tenant') or '-'}  state={vm.get('state')}  age={age_str}")


# ── Volume commands ────────────────────────────────────────────────────────

def _daemon_client():
    from flint._client.client import DaemonClient
    _require_daemon()
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
        click.echo(f"  {v['id']}  {v.get('tenant', ''):<16} {v['name']:<24} {v.get('kind', 'dir'):<5} {size:>9}{flags}")


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


# ── Templates ──────────────────────────────────────────────────────────────

@cli.group()
def templates():
    """Templates: container images sandboxes boot from (import and delete need an admin)."""


@templates.command("list")
def templates_list():
    """List templates."""
    tpls = _volume_call(_daemon_client().list_templates)
    for t in tpls:
        image = (t.get("image") or "-").removeprefix("index.docker.io/library/")
        status = t.get("status", "")
        click.echo(f"  {t['template_id']:<24} {status:<9} {t.get('tenant') or 'all tenants':<16} {image}")
        if status in ("failed", "error") and t.get("error"):
            click.echo(f"  {'':<24} {t['error'].splitlines()[-1]}")


@templates.command("import")
@click.argument("name")
@click.argument("image")
@click.option("--disk", "rootfs_size_mb", type=int, default=2048, show_default=True,
              help="Disk size in MiB; the image must fit, with room for what the VMs write")
@click.option("--tenant", default=None, help="Only this tenant's VMs may boot it (default: every tenant)")
@click.option("--registry-user", default=None, help="Log in to a private registry as this user")
@click.option("--registry-password-stdin", is_flag=True, default=False,
              help="Read the registry password or token from stdin (otherwise you are prompted)")
@click.option("--no-wait", is_flag=True, default=False, help="Return once the import has started")
def templates_import(name, image, rootfs_size_mb, tenant, registry_user, registry_password_stdin, no_wait):
    """Import a container image as a template: NAME IMAGE, like `python python:3.12-slim`.

    Build and push the image with your usual tooling first. Flint pulls it
    once, pins the tag to its digest and snapshots it; importing to an
    existing NAME replaces that template once the new image is ready.
    """
    auth = None
    if registry_user:
        password = (click.get_text_stream("stdin").read().strip() if registry_password_stdin
                    else click.prompt("Registry password or token", hide_input=True))
        auth = {"username": registry_user, "password": password}
    elif registry_password_stdin:
        raise click.UsageError("--registry-password-stdin needs --registry-user")
    client = _daemon_client()
    started = _volume_call(client.import_template, name, image, rootfs_size_mb=rootfs_size_mb, tenant=tenant,
                           registry_auth=auth)
    template_id = started["template_id"]
    if no_wait:
        click.echo(f"Importing {image} as {template_id}; check with `flint templates list`")
        return
    click.echo(f"Importing {image} as {template_id}...")
    import time as _time
    while True:
        t = _volume_call(client.get_template, template_id)
        status = (t or {}).get("status")
        if status == "ready":
            click.echo(f"Ready: {template_id} ({t.get('digest', '')})")
            click.echo(f"  Boot it with Sandbox(template_id={template_id!r})")
            return
        if status in ("failed", "error"):
            raise click.ClickException(f"import failed: {t.get('error') or 'see the daemon log'}")
        _time.sleep(2)


@templates.command("delete")
@click.argument("template_id")
def templates_delete(template_id):
    """Delete a template. Sandboxes already running from it keep running."""
    _volume_call(_daemon_client().delete_template, template_id)
    click.echo(f"Deleted template {template_id}")
