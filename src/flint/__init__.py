from flint.core import Sandbox, SandboxManager, CommandResult

__all__ = ["Sandbox", "SandboxManager", "CommandResult", "main"]


def main() -> None:
    from flint.cli import cli
    cli()
