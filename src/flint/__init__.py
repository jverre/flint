__all__ = ["Sandbox", "CommandResult", "Template", "TemplateInfo", "Volume", "configure", "main"]

# Imported on first use, so `flint start`/`flint agent` can set FLINT_* before
# flint.core.config reads them (the CLI is imported through this package).
_EXPORTS = {
    "Sandbox": "flint.sandbox",
    "CommandResult": "flint.sandbox",
    "Template": "flint.template",
    "TemplateInfo": "flint.template",
    "Volume": "flint.volume",
    "configure": "flint.sandbox",
}


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'flint' has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(__all__)


def main() -> None:
    from flint.cli import cli
    cli()
