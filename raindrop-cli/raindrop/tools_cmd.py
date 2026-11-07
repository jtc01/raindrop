"""`raindrop tools add|remove|list`: install the MCP + hook into a coding tool.

Generic over adapters (raindrop/adapters/registry.py). Registered on the main
app in cli.py with `app.add_typer(tools_cmd.app, name="tools")`.
"""
from __future__ import annotations

import typer

from raindrop import config
from raindrop.adapters import registry
from raindrop.adapters.base import Adapter
from raindrop.errors import NotJoinedError, RaindropError, ToolNotDetectedError

app = typer.Typer(help="Install or remove Raindrop in your coding tools.", no_args_is_help=True)


def _require_config() -> dict:
    cfg = config.load()
    if not cfg:
        raise NotJoinedError()
    return cfg


def _summary(adapter: Adapter, status) -> str:
    parts = [label for label, on in (("MCP", status.mcp), ("auto-sync hook", status.hook),
                                      ("rules", status.rules)) if on]
    return f"{adapter.display_name} [ok] " + " + ".join(parts)


def _run(fn):
    try:
        fn()
    except RaindropError as e:
        typer.secho(f"Error: {e}", fg="red", err=True)
        raise typer.Exit(1)


@app.command("add")
def add(
    tool: str = typer.Argument(..., help=f"One of: {', '.join(registry.all_names())}."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the changes without applying."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    rules: bool = typer.Option(False, "--rules", help="Also add the usage block to the tool's rules file."),
):
    """Install the Raindrop MCP server and auto-sync hook into TOOL."""
    def go():
        cfg = _require_config()
        adapter = registry.get_adapter(tool)
        if not adapter.detect():
            raise ToolNotDetectedError(f"{adapter.display_name} doesn't appear to be installed.")
        changes = adapter.plan_install(rules=rules)

        typer.secho(f"Planned changes for {adapter.display_name}:", bold=True)
        for c in changes:
            typer.echo(f"\n- {c.description}")
            typer.echo("\n".join("    " + ln for ln in c.preview.rstrip().splitlines()))
        if dry_run:
            typer.echo("\n(dry run: nothing changed)")
            return
        if not yes and not typer.confirm("\nApply these changes?"):
            raise typer.Exit(1)

        status = adapter.apply(changes)
        cfg.setdefault("tools", {})[adapter.name] = status.to_tool_record()
        config.save(cfg)
        typer.secho(_summary(adapter, status), fg="green")
        if not status.hook:
            typer.echo(f"Note: auto-sync isn't available for {adapter.display_name} yet. "
                       "It can read your team's context, but won't share your own sessions.")

    _run(go)


@app.command("remove")
def remove(tool: str = typer.Argument(..., help="Tool to uninstall Raindrop from.")):
    """Undo what `tools add` installed for TOOL."""
    def go():
        cfg = _require_config()
        adapter = registry.get_adapter(tool)
        record = cfg.get("tools", {}).get(adapter.name)
        if not record:
            typer.echo(f"{adapter.display_name} isn't installed via Raindrop.")
            return
        adapter.uninstall(record)
        del cfg["tools"][adapter.name]
        config.save(cfg)
        typer.secho(f"Removed Raindrop from {adapter.display_name}.", fg="green")

    _run(go)


@app.command("list")
def list_tools():
    """Show tools detected on this machine and which have Raindrop installed."""
    installed = (config.load() or {}).get("tools", {})
    for name in registry.all_names():
        try:
            adapter = registry.get_adapter(name)
        except ImportError:
            continue
        state = "installed" if name in installed else ("detected" if adapter.detect() else "not found")
        typer.echo(f"{name:<14} {state}")
