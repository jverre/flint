from flint.sandbox import Sandbox, CommandResult
from flint.template import Template, TemplateInfo

__all__ = ["Sandbox", "CommandResult", "Template", "TemplateInfo", "main"]


def main() -> None:
    from flint.cli import cli
    cli()
