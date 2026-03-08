from flint.sandbox import Sandbox, CommandResult

__all__ = ["Sandbox", "CommandResult", "main"]


def main() -> None:
    from flint.cli import cli
    cli()
