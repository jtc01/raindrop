"""The sharing question asked right after `raindrop join` / `raindrop create-team`
(after the coding-tool picker). Sharing is off until the person says yes; this only turns on
what they choose, the same way `raindrop share` / `raindrop share --repo` do.
"""
from __future__ import annotations

import typer

from raindrop import config

SHARE_OPTION_HELP = (
    "Whether to share your agent context with the team right away: 'repo' (the git repo you are in), "
    "'all' (every repo on this machine) or 'none'. Without this option you are asked in a terminal. "
    "Sharing stays off until you choose."
)

_CHOICES = {"repo": "repo", "this": "repo", "all": "all", "none": "none", "skip": "none", "no": "none", "n": "none"}


def validate_share_option(text: str | None) -> str | None:
    """Check a --share value before anything else happens. Returns None (not given), or
    'repo' / 'all' / 'none'."""
    if text is None:
        return None
    choice = _CHOICES.get(text.strip().lower())
    if choice is None:
        raise typer.BadParameter("use 'repo', 'all' or 'none'", param_hint="--share")
    return choice


def _current_repo() -> str | None:
    """The git repo the person is in, or None when the current folder isn't in one."""
    root = config.repo_root()
    return str(root) if (root / ".git").exists() else None


def _apply(choice: str, repo: str | None) -> None:
    cfg = config.load()
    if not cfg:
        return
    sharing = cfg.setdefault("sharing", {})
    sharing.setdefault("mode", config.DEFAULT_SHARING_MODE)
    if choice == "all":
        sharing["mode"] = "all"
        typer.secho("Sharing on for every repo on this machine.", fg="green")
    elif choice == "repo" and repo:
        repos = sharing.setdefault("shared_repos", [])
        if repo not in repos:
            repos.append(repo)
        typer.secho(f"Sharing on for {repo}.", fg="green")
    config.save(cfg)
    typer.echo("Change it any time with `raindrop share`, `raindrop unshare` or `raindrop pause`.")


def _interactive() -> bool:
    import sys

    return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())


def offer_sharing(choice: str | None = None) -> None:
    """After joining: ask whether to share agent context with the team.

    `choice` is a validated --share value. Without it a terminal gets a prompt (Enter means no),
    and anything else (a script, a pipe) just gets a hint, so this never waits for input nobody
    can give."""
    repo = _current_repo()
    if choice == "repo" and not repo:
        typer.secho("Not sharing: you are not inside a git repo. Run `raindrop share --repo` from one.",
                    fg="yellow")
        return
    if choice is not None:
        if choice == "none":
            typer.echo("Sharing is off. Turn it on any time with `raindrop share`.")
            return
        _apply(choice, repo)
        return

    if not _interactive():
        typer.echo("Sharing is off until you turn it on: `raindrop share --repo` (this repo) or "
                   "`raindrop share` (every repo).")
        return

    typer.echo("\nShare your coding sessions with the team?")
    typer.echo("Teammates' AI assistants can then find what you worked on. Secrets are redacted before "
               "anything leaves your machine. Nothing is shared until you say yes.")
    options: list[tuple[str, str]] = []
    if repo:
        options.append(("repo", f"This repo only ({repo})"))
    options.append(("all", "Every repo on this machine"))
    for i, (_, label) in enumerate(options, 1):
        typer.echo(f"  {i}) {label}")
    typer.echo("  Enter) Not now")
    while True:
        try:
            answer = typer.prompt("Share", default="", show_default=False).strip().lower()
        except typer.Abort:
            typer.echo("\nNot sharing. You can run `raindrop share` any time.")
            return
        if answer in ("", "n", "no", "none", "skip"):
            typer.echo("Not sharing. You can run `raindrop share` any time.")
            return
        picked = None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            picked = options[int(answer) - 1][0]
        elif answer in ("all", "repo") and any(k == answer for k, _ in options):
            picked = answer
        if picked:
            _apply(picked, repo)
            return
        typer.secho(f"Choose 1-{len(options)} or press Enter to skip.", fg="yellow", err=True)
