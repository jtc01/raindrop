"""raindrop/errors.py

Exception hierarchy. Everything raindrop raises on purpose inherits from
RaindropError, so the CLI can catch one type and print a clean message
instead of a traceback.
"""

from __future__ import annotations


class RaindropError(Exception):
    """Base class for all expected raindrop errors."""


class NotJoinedError(RaindropError):
    """~/.raindrop/config.json is missing. The user must run `raindrop join`."""

    def __init__(
        self, message: str = "Not joined to a team. Run `raindrop join <code>` first."
    ):
        super().__init__(message)


class UnknownToolError(RaindropError):
    """Requested tool name isn't registered. Message should list valid names."""


class ToolNotDetectedError(RaindropError):
    """The tool isn't installed on this machine."""


class ConfigParseError(RaindropError):
    """A config file (ours or a tool's) is unreadable or invalid."""

    def __init__(self, path: object, detail: str = ""):
        self.path = path
        msg = f"Could not parse config file: {path}"
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)


class TranscriptError(RaindropError):
    """A transcript file is missing or can't be parsed."""


class PushError(RaindropError):
    """The API rejected or failed a push_messages request."""

    def __init__(self, status_code: int | None = None, detail: str = ""):
        self.status_code = status_code
        msg = "Push failed"
        if status_code is not None:
            msg += f" with status {status_code}"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class InstallError(RaindropError):
    """Install or uninstall failed partway through.

    step: which change failed (e.g. "hook").
    backup_path: where the original file was backed up, if one was made.
    """

    def __init__(self, step: str, detail: str = "", backup_path: object | None = None):
        self.step = step
        self.backup_path = backup_path
        msg = f"Install failed at step '{step}'"
        if detail:
            msg += f": {detail}"
        if backup_path is not None:
            msg += f" (original backed up at {backup_path})"
        super().__init__(msg)
