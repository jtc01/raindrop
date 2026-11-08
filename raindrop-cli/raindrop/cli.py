"""Raindrop CLI entry point.

  raindrop create-team "team-name" --name Matt --focus backend
  raindrop join K7QX-M9TF --name Dante --focus frontend
  raindrop invite        # new join code for your team
  raindrop teammates     # who's on the team
  raindrop doctor        # check your setup
  raindrop rotate-key    # replace a leaked key
  raindrop leave         # revoke your key and remove local config
  raindrop remove Dante  # (lead) remove a teammate
  raindrop codes         # list join codes; `codes revoke <code>` cancels one
  raindrop share [--repo] / unshare [--repo]  # opt this repo (or everything) in/out of sharing
  raindrop pause [--repo] / resume [--repo]   # temporarily stop/start sharing context
  raindrop whoami
  raindrop help         # every command, grouped; `help <command>` for details

Other owners add their commands (sync, tools add, proxy) to `app` below.
"""
from __future__ import annotations

import os

import typer

from raindrop import api_client
from raindrop import config
from raindrop import proxy_cmd
from raindrop import share_prompt
from raindrop import sync_cmd
from raindrop import tools_cmd

DEFAULT_API_URL = "https://ze93w3lhua.execute-api.us-east-1.amazonaws.com"

app = typer.Typer(help="Raindrop: share agent context with your team.", no_args_is_help=True)
app.add_typer(tools_cmd.app, name="tools")
app.command("sync")(sync_cmd.sync)
app.command("proxy")(proxy_cmd.proxy)


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
    name: str = typer.Option(..., "--name", prompt="Your display name", help="Your display name."),
    focus: str = typer.Option("", "--focus", help="What you work on, e.g. 'backend'."),
    api_url: str = typer.Option(None, "--api-url", help="Override the API URL."),
    force: bool = typer.Option(False, "--force", help="Replace an existing config."),
    tools: str = typer.Option(None, "--tools", help=tools_cmd.TOOLS_OPTION_HELP),
    share_choice: str = typer.Option(None, "--share", help=share_prompt.SHARE_OPTION_HELP),
):
    """Create a team, join it as the lead, and print a join code for teammates."""
    selection = tools_cmd.validate_tools_option(tools)  # a typo fails here, before anything is created
    share_pick = share_prompt.validate_share_option(share_choice)
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
    tools_cmd.offer_tools(selection)
    share_prompt.offer_sharing(share_pick)


@app.command("join")
def join(
    code: str = typer.Argument(..., help="Join code from your team lead, e.g. K7QX-M9TF."),
    name: str = typer.Option(..., "--name", prompt="Your display name", help="Your display name."),
    focus: str = typer.Option("", "--focus", help="What you work on, e.g. 'frontend'."),
    api_url: str = typer.Option(None, "--api-url", help="Override the API URL."),
    force: bool = typer.Option(False, "--force", help="Replace an existing config."),
    tools: str = typer.Option(None, "--tools", help=tools_cmd.TOOLS_OPTION_HELP),
    share_choice: str = typer.Option(None, "--share", help=share_prompt.SHARE_OPTION_HELP),
):
    """Join a team with a join code. Saves your key to ~/.raindrop/config.json, then offers to
    set Raindrop up in the coding tools found on this machine."""
    selection = tools_cmd.validate_tools_option(tools)  # a typo fails here, before anything is joined
    share_pick = share_prompt.validate_share_option(share_choice)
    existing = _guard_existing(force)
    url = _api_url(api_url)
    try:
        resp = api_client.join(url, code, name, focus)
    except api_client.ApiError as e:
        _fail(e)
    path = config.save(config.build(url, resp, name, focus, existing))
    typer.secho(f"Joined team '{resp.get('team_name') or resp['team_id']}' as {name}", fg="green")
    typer.echo(f"Saved credentials to {path}")
    tools_cmd.offer_tools(selection)
    share_prompt.offer_sharing(share_pick)


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
        tags = []
        if m.get("lead"):
            tags.append(typer.style("lead", fg="yellow"))
        if m["you"]:
            tags.append(typer.style("you", fg="green"))
        suffix = f"  ({', '.join(tags)})" if tags else ""
        typer.echo(f"  {m['display_name']:<{width}}  {m['focus'] or '-'}{suffix}")


@app.command("remove")
def remove(
    who: str = typer.Argument(..., help="Teammate's display name or agent id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
):
    """(Team lead) Remove a teammate: their key stops working. Shared context stays."""
    cfg = _require_config()
    members = _authed(api_client.teammates, cfg)["members"]
    matches = [m for m in members if m["agent_id"] == who] or \
              [m for m in members if m["display_name"].lower() == who.lower()]
    if not matches:
        typer.secho(f"No teammate named '{who}'. See `raindrop teammates`.", fg="red", err=True)
        raise typer.Exit(1)
    if len(matches) > 1:
        typer.secho(f"Several teammates are named '{who}'. Use an agent id:", fg="yellow", err=True)
        for m in matches:
            typer.echo(f"  {m['agent_id']}  {m['display_name']}  {m['focus'] or '-'}", err=True)
        raise typer.Exit(1)
    target = matches[0]
    if not yes:
        typer.confirm(f"Remove {target['display_name']} from the team? Their key will stop working.",
                      abort=True)
    try:
        api_client.remove(cfg["api_url"], cfg["api_key"], target["agent_id"])
    except api_client.ApiError as e:
        _fail(e)
    typer.secho(f"Removed {target['display_name']}.", fg="green")
    typer.echo("Their key may keep working for up to a minute while server caches expire.")


codes_app = typer.Typer(help="List or revoke your team's join codes.", invoke_without_command=True)
app.add_typer(codes_app, name="codes")


@codes_app.callback()
def codes(ctx: typer.Context):
    """List active join codes. Codes are stored hashed, so each is shown by a short id."""
    if ctx.invoked_subcommand:
        return
    import datetime
    cfg = _require_config()
    rows = _authed(api_client.list_codes, cfg)["codes"]
    if not rows:
        typer.echo("No active join codes. Create one with `raindrop invite`.")
        return
    typer.secho(f"{len(rows)} active join code(s)", bold=True)
    for c in rows:
        exp = datetime.datetime.fromtimestamp(c["expires_at"]).strftime("%a %H:%M")
        typer.echo(f"  {c['id']}  by {c['created_by']:<14} uses {c['uses']:<3} expires {exp}")
    typer.echo("Revoke one with `raindrop codes revoke <code-or-id>`.")


@codes_app.command("revoke")
def codes_revoke(code: str = typer.Argument(..., help="The join code (AWB3-V69V) or its id from `raindrop codes`.")):
    """Cancel a join code so nobody else can use it."""
    cfg = _require_config()
    looks_like_code = len(code.replace("-", "").replace(" ", "")) == 8  # ids are 10 chars
    try:
        if looks_like_code:
            resp = api_client.revoke_code(cfg["api_url"], cfg["api_key"], code=code)
        else:
            resp = api_client.revoke_code(cfg["api_url"], cfg["api_key"], code_id=code)
    except api_client.ApiError as e:
        _fail(e)
    typer.secho(f"Revoked join code {resp['revoked']}.", fg="green")


@app.command("share")
def share(repo: bool = typer.Option(False, "--repo", help="Share only the git repo you're in.")):
    """Opt in to sharing agent context: this repo (--repo), or every repo on this machine."""
    cfg = _require_config()
    sharing = cfg.setdefault("sharing", {})
    sharing.setdefault("mode", config.DEFAULT_SHARING_MODE)
    if repo:
        root = str(config.repo_root())
        repos = sharing.setdefault("shared_repos", [])
        if root not in repos:
            repos.append(root)
        msg = f"Sharing on for {root}."
        if sharing["mode"] == "all":
            msg += " (Mode is 'all', so every repo is shared anyway.)"
    else:
        sharing["mode"] = "all"
        msg = "Sharing on for every repo on this machine."
    config.save(cfg)
    typer.secho(msg, fg="green")
    if sharing.get("paused"):
        typer.secho("Sharing is still paused everywhere; run `raindrop resume` to start uploading.", fg="yellow")
    elif repo and config.sharing_paused(cfg=cfg):
        typer.secho("This repo is still paused; run `raindrop resume --repo` here to start uploading.", fg="yellow")


@app.command("unshare")
def unshare(repo: bool = typer.Option(False, "--repo", help="Stop sharing only the git repo you're in.")):
    """Opt out of sharing: remove this repo (--repo), or switch to opt-in so only shared repos upload."""
    cfg = _require_config()
    sharing = cfg.setdefault("sharing", {})
    sharing.setdefault("mode", config.DEFAULT_SHARING_MODE)
    if repo:
        root = str(config.repo_root())
        sharing["shared_repos"] = [r for r in sharing.get("shared_repos", []) if r != root]
        msg = f"Sharing off for {root}."
        if sharing["mode"] == "all":
            msg += (" Mode is 'all', so this repo is still shared; run `raindrop unshare` "
                    "to switch to opt-in, or `raindrop pause --repo` to block just this repo.")
    else:
        sharing["mode"] = config.DEFAULT_SHARING_MODE
        n = len(sharing.get("shared_repos", []))
        msg = f"Sharing is now opt-in: only the {n} repo(s) added with `raindrop share --repo` upload."
    config.save(cfg)
    typer.secho(msg, fg="yellow")
    typer.echo("Conversations in unshared repos are skipped, not queued: they won't upload if you share later.")


@app.command("pause")
def pause(repo: bool = typer.Option(False, "--repo", help="Pause only the git repo you're in.")):
    """Stop sharing your agent context with the team (everywhere, or for one repo)."""
    cfg = _require_config()
    sharing = cfg.setdefault("sharing", {})
    if repo:
        root = str(config.repo_root())
        repos = sharing.setdefault("paused_repos", [])
        if root not in repos:
            repos.append(root)
        msg = f"Sharing paused for {root}."
    else:
        sharing["paused"] = True
        msg = "Sharing paused everywhere."
    config.save(cfg)
    typer.secho(msg, fg="yellow")
    typer.echo("Conversations while paused are skipped, not queued: they won't upload when you resume.")


@app.command("resume")
def resume(repo: bool = typer.Option(False, "--repo", help="Resume only the git repo you're in.")):
    """Resume sharing your agent context with the team."""
    cfg = _require_config()
    sharing = cfg.setdefault("sharing", {})
    if repo:
        root = str(config.repo_root())
        sharing["paused_repos"] = [r for r in sharing.get("paused_repos", []) if r != root]
        msg = f"Sharing resumed for {root}."
        if sharing.get("paused"):
            msg += " (Still paused globally; run `raindrop resume` to resume everywhere.)"
    else:
        sharing["paused"] = False
        msg = "Sharing resumed."
        if sharing.get("paused_repos"):
            msg += f" Still paused for {len(sharing['paused_repos'])} repo(s); use `raindrop resume --repo` in them."
    config.save(cfg)
    typer.secho(msg, fg="green")


@app.command("rotate-key")
def rotate_key(yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation.")):
    """Replace your API key with a new one and revoke the old one (use this if it leaked)."""
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


@app.command("disband")
def disband(yes: bool = typer.Option(False, "--yes", "-y", help="Skip typing the team name.")):
    """(Team lead) Permanently disband your team: deletes all shared context and revokes everyone's key."""
    cfg = _require_config()
    team = cfg.get("team_name") or ""
    typer.secho("This permanently deletes everything your team has shared and revokes every "
                "teammate's key. It cannot be undone.", fg="red", bold=True)
    if yes and team:
        confirm = team
    else:
        confirm = typer.prompt(f"Type the team name{f' ({team})' if team else ''} to confirm")
        if team and confirm.strip() != team:
            typer.secho("Name didn't match. Nothing was changed.", fg="yellow", err=True)
            raise typer.Exit(1)
    try:
        r = api_client.disband(cfg["api_url"], cfg["api_key"], confirm.strip())
    except api_client.ApiError as e:
        _fail(e)
    config.delete()
    typer.secho(f"Disbanded '{r.get('team_name') or team}': {r.get('keys_revoked', 0)} keys revoked, "
                f"{r.get('exchanges_deleted', 0)} shared exchanges deleted.", fg="green")
    typer.echo(f"Removed {config.CONFIG_PATH}. Teammates' keys may keep working for up to a minute "
               "while server caches expire. Run `raindrop tools remove <tool>` to uninstall hooks.")


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
        mark = typer.style("[ok]", fg="green") if ok else typer.style("[x]", fg="red")
        typer.echo(f"  {mark}  {label}")
        if not ok and fix:
            typer.echo(f"       -> {fix}")

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

    report(None, f"Sharing: {_sharing_summary(cfg)}")
    if (config.sharing_mode(cfg) == "opt_in" and not config.sharing_paused(cfg=cfg)
            and not config.should_share(cfg=cfg)):
        typer.secho(f"       ! This repo ({config.repo_root()}) isn't shared. Run `raindrop share --repo` "
                    "here to share it, or `raindrop share` to share every repo.", fg="yellow")

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
        typer.secho("Some checks failed. Fix the items marked [x] above.", fg="yellow")
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
    typer.echo(f"Sharing: {_sharing_summary(cfg)}")


def _sharing_summary(cfg: dict) -> str:
    """One line for whoami/doctor: mode, counts, and what applies to the current repo."""
    sharing = cfg.get("sharing", {})
    if sharing.get("paused"):
        return "paused everywhere"
    if config.sharing_mode(cfg) == "all":
        base = "all repos"
    else:
        base = f"opt-in, {len(sharing.get('shared_repos', []))} repo(s) shared"
    paused_repos = sharing.get("paused_repos", [])
    if paused_repos:
        base += f", paused for {len(paused_repos)} repo(s)"
    if config.sharing_paused(cfg=cfg):
        here = "this repo is paused"
    elif config.should_share(cfg=cfg):
        here = "this repo is shared"
    else:
        here = "this repo is NOT shared"
    return f"{base} ({here})"



# ---------- raindrop help ----------

# Which section each command appears under in `raindrop help`. Anything not
# listed here still shows up (under "More"), so new commands never go missing.
HELP_SECTIONS: list[tuple[str, list[str]]] = [
    ("Get started", ["create-team", "join", "tools", "doctor"]),
    ("Your team", ["teammates", "invite", "codes", "remove", "leave", "disband"]),
    ("Sharing and privacy", ["share", "unshare", "pause", "resume"]),
    ("Your account", ["whoami", "rotate-key"]),
]
# Run by your coding tool, not by you. Listed last so people know what they are.
HELP_AUTOMATIC = ["sync", "proxy"]


def _is_group(cmd) -> bool:
    # Typer ships its own copy of Click, so check for subcommands instead of the class.
    return hasattr(cmd, "list_commands") and hasattr(cmd, "get_command")


def _sub_ctx(parent, cmd, name: str):
    return type(parent)(cmd, info_name=name, parent=parent)


def _short(cmd) -> str:
    return cmd.get_short_help_str(limit=200)


def _help_rows(group, ctx, name: str) -> list[tuple[str, str]]:
    """One row per command; groups expand to one row per subcommand."""
    cmd = group.get_command(ctx, name)
    if cmd is None or cmd.hidden:
        return []
    if not _is_group(cmd):
        return [(name, _short(cmd))]
    rows = []
    if cmd.invoke_without_command:  # e.g. `raindrop codes` does something on its own
        rows.append((name, _short(cmd)))
    sub_ctx = _sub_ctx(ctx, cmd, name)
    for sub in cmd.list_commands(sub_ctx):
        subcmd = cmd.get_command(sub_ctx, sub)
        if subcmd is not None and not subcmd.hidden:
            rows.append((f"{name} {sub}", _short(subcmd)))
    return rows


@app.command("help")
def help_command(
    ctx: typer.Context,
    command: list[str] = typer.Argument(None, help="A command to explain, e.g. `share` or `tools add`."),
):
    """List every Raindrop command, or explain one: `raindrop help share`."""
    root = ctx.find_root()
    group = root.command

    if command:  # raindrop help <command> [subcommand]
        cmd, cmd_ctx = group, root
        for part in command:
            sub = cmd.get_command(cmd_ctx, part) if _is_group(cmd) else None
            if sub is None:
                typer.secho(f"No command '{' '.join(command)}'. Run `raindrop help` to see them all.",
                            fg="red", err=True)
                raise typer.Exit(1)
            cmd, cmd_ctx = sub, _sub_ctx(cmd_ctx, sub, part)
        typer.echo(cmd.get_help(cmd_ctx))
        return

    names = [n for n in group.list_commands(root) if n != "help"]
    listed = {n for _, ns in HELP_SECTIONS for n in ns} | set(HELP_AUTOMATIC)
    sections = [(title, [n for n in ns if n in names]) for title, ns in HELP_SECTIONS]
    extra = [n for n in names if n not in listed]
    if extra:
        sections.append(("More", extra))
    sections.append(("Run automatically by your coding tool", [n for n in HELP_AUTOMATIC if n in names]))

    rows_by_section = [(t, [r for n in ns for r in _help_rows(group, root, n)]) for t, ns in sections]
    width = max((len(c) for _, rows in rows_by_section for c, _ in rows), default=10)

    typer.secho("Raindrop: share your coding agent's context with your team.", bold=True)
    for title, rows in rows_by_section:
        if not rows:
            continue
        typer.echo()
        typer.secho(title, bold=True)
        for name, desc in rows:
            typer.echo(f"  raindrop {name:<{width}}  {desc}")
    typer.echo()
    typer.echo("Details on any command: raindrop help <command>  (or add --help)")


if __name__ == "__main__":
    app()
