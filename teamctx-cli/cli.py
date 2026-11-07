"""Raindrop CLI entry point.

  raindrop create-team "team-name" --name Matt --focus backend
  raindrop join K7QX-M9TF --name Dante --focus frontend
  raindrop invite        # new join code for your team
  raindrop teammates     # who's on the team
  raindrop doctor        # check your setup
  raindrop rotate-key    # replace a leaked key
  raindrop leave         # revoke your key and remove local config
  raindrop whoami

Other owners add their commands (sync, tools add, proxy) to `app` below.
"""
from __future__ import annotations

import os

import typer

import api_client
import config
import sync_cmd
import tools_cmd

DEFAULT_API_URL = "https://ze93w3lhua.execute-api.us-east-1.amazonaws.com"

app = typer.Typer(help="Raindrop: share agent context with your team.", no_args_is_help=True)
app.add_typer(tools_cmd.app, name="tools")
app.command("sync")(sync_cmd.sync)


def _api_url(override: str | None) -> str:
    return override or os.environ.get("RAINDROP_API_URL") or DEFAULT_API_URL


def _guard_existing(force: bool) -> dict | None:
    existing = config.load()
    if existing and not force:
        typer.secho(
            f"Already joined team '{existing.get('team_name') or existing.get('team_id')}' "
            f"as {existing.get('display_name')}. Use --force to replace this config.",
            fg="yellow", err=True)
        raise typer.Exit(1)
    return existing


def _fail(e: api_client.ApiError) -> None:
    hints = {
        "invalid_code": "Check the code with your team lead.",
        "expired_code": "Ask any teammate to run `raindrop invite` for a new code.",
        "network_error": "Check your connection or the API URL.",
    }
    typer.secho(f"Error: {e.message}", fg="red", err=True)
    if e.code in hints:
        typer.echo(hints[e.code], err=True)
    raise typer.Exit(1)


@app.command("create-team")
def create_team(
    team_name: str = typer.Argument(..., help="Name for the new team."),
    name: str = typer.Option(..., "--name", help="Your display name."),
    focus: str = typer.Option("", "--focus", help="What you work on, e.g. 'backend'."),
    api_url: str = typer.Option(None, "--api-url", help="Override the API URL."),
    force: bool = typer.Option(False, "--force", help="Replace an existing config."),
):
    """Create a team, join it as the lead, and print a join code for teammates."""
    existing = _guard_existing(force)
    url = _api_url(api_url)
    try:
        resp = api_client.create_team(url, team_name, name, focus)
    except api_client.ApiError as e:
        _fail(e)
    path = config.save(config.build(url, resp, name, focus, existing))
    typer.secho(f"Created team '{resp['team_name']}'", fg="green")
    typer.echo(f"Join code: {resp['join_code']}  (valid 48h)")
    typer.echo(f"Teammates run: raindrop join {resp['join_code']} --name <name> --focus <area>")
    typer.echo(f"Saved credentials to {path}")
    typer.echo("Next: raindrop tools add claude-code")


@app.command("join")
def join(
    code: str = typer.Argument(..., help="Join code from your team lead, e.g. K7QX-M9TF."),
    name: str = typer.Option(..., "--name", help="Your display name."),
    focus: str = typer.Option("", "--focus", help="What you work on, e.g. 'frontend'."),
    api_url: str = typer.Option(None, "--api-url", help="Override the API URL."),
    force: bool = typer.Option(False, "--force", help="Replace an existing config."),
):
    """Join a team with a join code. Saves your key to ~/.raindrop/config.json."""
    existing = _guard_existing(force)
    url = _api_url(api_url)
    try:
        resp = api_client.join(url, code, name, focus)
    except api_client.ApiError as e:
        _fail(e)
    path = config.save(config.build(url, resp, name, focus, existing))
    typer.secho(f"Joined team '{resp.get('team_name') or resp['team_id']}' as {name}", fg="green")
    typer.echo(f"Saved credentials to {path}")
    typer.echo("Next: raindrop tools add claude-code")


@app.command("invite")
def invite():
    """Get a fresh join code for your team (valid 48h). Any member can run this."""
    cfg = config.load()
    if not cfg:
        typer.echo("Not joined. Run `raindrop join <code> --name <name>` first.")
        raise typer.Exit(1)
    try:
        resp = api_client.invite(cfg["api_url"], cfg["api_key"])
    except api_client.ApiError as e:
        if e.status == 401:
            typer.secho("Your key was rejected. Run `raindrop join <code> --name <name> --force`.",
                        fg="red", err=True)
            raise typer.Exit(1)
        _fail(e)
    typer.secho(f"New join code for '{resp.get('team_name') or resp['team_id']}': "
                f"{resp['join_code']}  (valid 48h)", fg="green")
    typer.echo(f"Teammates run: raindrop join {resp['join_code']} --name <name> --focus <area>")


def _require_config() -> dict:
    cfg = config.load()
    if not cfg:
        typer.echo("Not joined. Run `raindrop join <code> --name <name>` first.")
        raise typer.Exit(1)
    return cfg


def _authed(call, cfg: dict):
    """Runs an authed API call with a friendly message on a rejected key."""
    try:
        return call(cfg["api_url"], cfg["api_key"])
    except api_client.ApiError as e:
        if e.status == 401:
            typer.secho("Your key was rejected (revoked, rotated elsewhere, or you left the team).",
                        fg="red", err=True)
            typer.echo("Get a code with a teammate's `raindrop invite`, then "
                       "`raindrop join <code> --name <name> --force`.", err=True)
            raise typer.Exit(1)
        _fail(e)


@app.command("teammates")
def teammates():
    """List everyone on your team and what they work on."""
    cfg = _require_config()
    resp = _authed(api_client.teammates, cfg)
    members = resp["members"]
    typer.secho(f"{cfg.get('team_name') or resp['team_id']}: {len(members)} member(s)", bold=True)
    width = max((len(m["display_name"]) for m in members), default=4)
    for m in members:
        marker = typer.style(" (you)", fg="green") if m["you"] else ""
        typer.echo(f"  {m['display_name']:<{width}}  {m['focus'] or '-'}{marker}")


@app.command("rotate-key")
def rotate_key(yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation.")):
    """Replace your API key with a new one and revoke the old one (e.g. if it leaked)."""
    cfg = _require_config()
    if not yes:
        typer.confirm("Issue a new key and revoke the current one?", abort=True)
    resp = _authed(api_client.rotate_key, cfg)
    cfg["api_key"] = resp["api_key"]
    config.save(cfg)
    typer.secho("Key rotated. The old key is revoked.", fg="green")
    typer.echo("Note: the old key may keep working for up to a minute while server caches expire.")


@app.command("leave")
def leave(yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation.")):
    """Leave your team: revokes your key and removes your local config."""
    cfg = _require_config()
    team = cfg.get("team_name") or cfg["team_id"]
    if not yes:
        typer.confirm(f"Leave team '{team}'? Your key will stop working. "
                      "Context you already shared stays with the team.", abort=True)
    _authed(api_client.leave, cfg)
    config.delete()
    typer.secho(f"Left team '{team}' and removed {config.CONFIG_PATH}.", fg="green")


@app.command("doctor")
def doctor():
    """Check your Raindrop setup and explain anything that's broken."""
    ok_all = True

    def report(ok: bool | None, label: str, fix: str = ""):
        nonlocal ok_all
        if ok is None:
            typer.echo(f"  -  {label}")
            return
        ok_all &= ok
        mark = typer.style("✓", fg="green") if ok else typer.style("✗", fg="red")
        typer.echo(f"  {mark}  {label}")
        if not ok and fix:
            typer.echo(f"       → {fix}")

    import shutil
    import sys

    typer.secho("Raindrop doctor", bold=True)
    report(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}", "Install Python 3.10 or newer.")

    cfg = config.load()
    report(cfg is not None, f"Config at {config.CONFIG_PATH}",
           "Run `raindrop join <code> --name <name>` (or `raindrop create-team`).")
    if cfg is None:
        raise typer.Exit(1)

    perms = config.permissions_ok()
    if perms is None:
        report(None, "Config permissions (not checked on Windows)")
    else:
        report(perms, "Config readable only by you", f"Run: chmod 600 {config.CONFIG_PATH}")

    url = cfg.get("api_url", "")
    try:
        api_client.health(url)
        reachable = True
    except api_client.ApiError:
        reachable = False
    report(reachable, f"API reachable ({url})", "Check your connection, or the api_url in your config.")

    if reachable:
        try:
            who = api_client.me(url, cfg["api_key"])
            same = who["agent_id"] == cfg.get("agent_id") and who["team_id"] == cfg.get("team_id")
            report(same, f"Key accepted: {who['display_name']} on '{who.get('team_name') or who['team_id']}'",
                   "Config and server disagree. Rejoin with `raindrop join <code> --name <name> --force`.")
        except api_client.ApiError as e:
            report(False, "Key accepted by server",
                   "Key rejected. Ask a teammate for `raindrop invite`, then "
                   "`raindrop join <code> --name <name> --force`." if e.status == 401 else e.message)

    tools = cfg.get("tools", {})
    report(bool(tools), f"Tools installed: {', '.join(tools) or 'none'}",
           "Run `raindrop tools add claude-code`.")
    if "claude-code" in tools or not tools:
        report(shutil.which("claude") is not None, "Claude Code found on PATH",
               "Install Claude Code, or ignore this if you use a different tool.")

    typer.echo()
    if ok_all:
        typer.secho("Everything looks good.", fg="green")
    else:
        typer.secho("Some checks failed. Fix the items marked ✗ above.", fg="yellow")
        raise typer.Exit(1)


@app.command("whoami")
def whoami():
    """Show the team and identity in your local config (never prints the key)."""
    cfg = config.load()
    if not cfg:
        typer.echo("Not joined. Run `raindrop join <code> --name <name>`.")
        raise typer.Exit(1)
    typer.echo(f"Team:   {cfg.get('team_name') or ''} ({cfg['team_id']})")
    typer.echo(f"You:    {cfg['display_name']} ({cfg['agent_id']}), focus: {cfg.get('focus') or '-'}")
    typer.echo(f"API:    {cfg['api_url']}")
    typer.echo(f"Tools:  {', '.join(cfg.get('tools', {})) or 'none installed'}")


if __name__ == "__main__":
    app()
