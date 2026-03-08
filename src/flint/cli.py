import time

import click


@click.group()
def cli():
    """Flint - Firecracker VM Manager"""
    pass


@cli.command()
def start():
    """Start the Flint manager daemon."""
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
