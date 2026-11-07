"""Raindrop CLI entry point.

  raindrop create-team "team-name" --name Matt --focus backend
  raindrop join K7QX-M9TF --name Dante --focus frontend
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
        "expired_code": "Ask your team lead to run `raindrop create-team` for a new code.",
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
