import time

import click

from flint.core import SandboxManager


@click.group()
def cli():
    """Flint - Firecracker VM Manager"""
    pass


@cli.command()
def start():
    """Start the Flint TUI"""
    from flint.tui import FlintApp
    app = FlintApp()
    app.run()


@cli.command()
@click.argument('vm_id')
def stop(vm_id):
    """Stop a VM by ID"""
    manager = SandboxManager()
    manager.kill(vm_id)
    click.echo(f"Stopped VM: {vm_id}")


@cli.command(name="list")
def list_vms():
    """List all VMs"""
    manager = SandboxManager()
    entries = manager.list()
    if not entries:
        click.echo("No VMs found.")
        return
    for sb in entries:
        short_id = sb.id[:8]
        age = time.time() - sb.created_at
        age_str = f"{int(age)}s" if age < 60 else f"{int(age / 60)}m"
        click.echo(f"  {short_id}  pid={sb.pid}  state={sb.state}  age={age_str}")
