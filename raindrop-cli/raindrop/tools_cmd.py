"""`raindrop tools add|remove|list`: install the MCP + hook into a coding tool.

Generic over adapters (raindrop/adapters/registry.py). Registered on the main
app in cli.py with `app.add_typer(tools_cmd.app, name="tools")`.

Also holds the picker shown right after `raindrop join` / `raindrop create-team`
(offer_tools), which installs into the tools the person chooses.
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


def install_tool(tool: str, *, dry_run: bool = False, yes: bool = False, rules: bool = False,
                 show_diffs: bool = True) -> bool:
    """Install the Raindrop MCP server and auto-sync hook into TOOL (what `tools add` does).

    Returns True if it applied the changes. Raises RaindropError (not joined, tool not found,
    install failure) for the caller to report."""
    cfg = _require_config()
    adapter = registry.get_adapter(tool)
    if not adapter.detect():
        raise ToolNotDetectedError(f"{adapter.display_name} doesn't appear to be installed.")
    changes = adapter.plan_install(rules=rules)

    typer.secho(f"Planned changes for {adapter.display_name}:", bold=True)
    for c in changes:
        typer.echo(f"\n- {c.description}")
        if show_diffs:
            typer.echo("\n".join("    " + ln for ln in c.preview.rstrip().splitlines()))
    if dry_run:
        typer.echo("\n(dry run: nothing changed)")
        return False
    if not yes and not typer.confirm("\nApply these changes?"):
        raise typer.Exit(1)

    status = adapter.apply(changes)
    cfg.setdefault("tools", {})[adapter.name] = status.to_tool_record()
    config.save(cfg)
    typer.secho(_summary(adapter, status), fg="green")
    if not status.hook:
        typer.echo(f"Note: auto-sync isn't available for {adapter.display_name} yet. "
                   "It can read your team's context, but won't share your own sessions.")
    return True


@app.command("add")
def add(
    tool: str = typer.Argument(..., help=f"One of: {', '.join(registry.all_names())}."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the changes without applying."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    rules: bool = typer.Option(False, "--rules", help="Also add the usage block to the tool's rules file."),
):
    """Install the Raindrop MCP server and auto-sync hook into TOOL."""
    _run(lambda: install_tool(tool, dry_run=dry_run, yes=yes, rules=rules))


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


# ---------------------------------------------------------------------------
# The picker shown right after `raindrop join` / `raindrop create-team`
# ---------------------------------------------------------------------------

TOOLS_OPTION_HELP = (
    "Install Raindrop into these coding tools right away: tool names separated by commas, 'all' "
    "(every tool found on this machine) or 'none'. Without this option you are asked in a terminal."
)

_SKIP_WORDS = ("none", "skip", "no", "n")


def _interactive() -> bool:
    """True when a person can answer a prompt (not a script, a pipe or a hook)."""
    import sys

    return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())


def _tokens(text: str) -> list[str]:
    return [x for x in text.replace(",", " ").lower().split() if x]


def parse_tool_selection(text: str, detected: list[str]) -> list[str]:
    """Tool names from what the person typed at the prompt: numbers from the list shown,
    tool names, 'all' (every detected tool), or nothing / 'none' / 'skip'.
    Raises ValueError with a message fit to show them."""
    tokens = _tokens(text)
    if not tokens or any(x in _SKIP_WORDS for x in tokens):
        return []
    known = registry.all_names()
    chosen: list[str] = []
    for tok in tokens:
        if tok == "all":
            names = detected
        elif tok.isdigit():
            i = int(tok)
            if not 1 <= i <= len(detected):
                raise ValueError(f"'{tok}' is not on the list (choose 1-{len(detected)})")
            names = [detected[i - 1]]
        elif tok in known:
            names = [tok]
        else:
            raise ValueError(f"'{tok}' is not a supported tool. Supported: {', '.join(known)}")
        chosen += [n for n in names if n not in chosen]
    return chosen


def validate_tools_option(text: str | None) -> list[str] | None:
    """Check a --tools value before anything else happens, so a typo doesn't waste a join.
    Returns None (option not given), ["all"], or a list of valid tool names ([] means skip)."""
    if text is None:
        return None
    tokens = _tokens(text)
    if not tokens or any(x in _SKIP_WORDS for x in tokens):
        return []
    if "all" in tokens:
        return ["all"]
    known = registry.all_names()
    bad = [x for x in tokens if x not in known]
    if bad:
        raise typer.BadParameter(
            f"'{bad[0]}' is not a supported tool. Supported: {', '.join(known)}", param_hint="--tools")
    return list(dict.fromkeys(tokens))


def _install_chosen(names: list[str]) -> None:
    installed = []
    for name in names:
        try:
            if install_tool(name, yes=True, show_diffs=False):
                installed.append(name)
        except RaindropError as e:  # one tool failing must not stop the others
            typer.secho(f"Could not set up {name}: {e}", fg="red", err=True)
    if installed:
        typer.echo("Restart those tools so they pick up Raindrop.")


def offer_tools(selection: list[str] | None = None) -> None:
    """After joining: show the coding tools found on this machine and install Raindrop into the
    ones the person picks.

    `selection` is a validated --tools value (see validate_tools_option). Without it a terminal
    gets a numbered prompt, and anything else (a script, a pipe) just gets a hint, so this never
    waits for input nobody can give."""
    if selection == []:
        typer.echo("Skipped tool setup. Run `raindrop tools add <tool>` any time.")
        return
    detected = registry.detect_installed()
    names = [a.name for a in detected]
    if not detected:
        typer.echo("No supported coding tools were found on this machine. After you install one, run: "
                   "raindrop tools add <tool>")
        typer.echo(f"Supported: {', '.join(registry.all_names())}")
        return

    if selection is not None:
        _install_chosen(names if selection == ["all"] else selection)
        return

    if not _interactive():
        typer.echo(f"Next: raindrop tools add <tool>   (found on this machine: {', '.join(names)})")
        return

    already = (config.load() or {}).get("tools", {})
    typer.echo("\nRaindrop can be set up in these coding tools found on this machine:")
    for i, a in enumerate(detected, 1):
        typer.echo(f"  {i}) {a.display_name}" + ("   (already installed)" if a.name in already else ""))
    while True:
        try:
            answer = typer.prompt('Install into which? Numbers or names (for example "1,3"), "all", or Enter to skip',
                                  default="", show_default=False)
        except typer.Abort:
            typer.echo("\nSkipped. You can run `raindrop tools add <tool>` any time.")
            return
        try:
            chosen = parse_tool_selection(answer, names)
            break
        except ValueError as e:
            typer.secho(f"{e}. Try again.", fg="yellow", err=True)
    if not chosen:
        typer.echo("Skipped. You can run `raindrop tools add <tool>` any time.")
        return
    _install_chosen(chosen)
