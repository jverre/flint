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
def start(port, data_dir, state_dir):
    """Start the Flint manager daemon."""
    if port is not None:
        os.environ["FLINT_PORT"] = str(port)
    if data_dir is not None:
        os.environ["FLINT_DATA_DIR"] = data_dir
    if state_dir is not None:
        os.environ["FLINT_STATE_DIR"] = state_dir
    from flint.daemon.server import FlintDaemon
    FlintDaemon().run()


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
