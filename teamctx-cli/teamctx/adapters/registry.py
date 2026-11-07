"""teamctx/adapters/registry.py

Maps tool names to adapter classes.

Adapters are referenced by "module:ClassName" strings and imported lazily, so
this file works even if another teammate's adapter file doesn't exist yet or
fails to import.

To register a new adapter, add one line to ADAPTERS.
"""

from __future__ import annotations

from importlib import import_module

from teamctx.adapters.base import Adapter
from teamctx.errors import UnknownToolError

ADAPTERS: dict[str, str] = {
    "claude-code": "teamctx.adapters.claude_code:ClaudeCodeAdapter",
    # "cursor": "teamctx.adapters.cursor:CursorAdapter",
}


def all_names() -> list[str]:
    return sorted(ADAPTERS)


def _load_class(name: str) -> type[Adapter]:
    module_path, class_name = ADAPTERS[name].split(":")
    return getattr(import_module(module_path), class_name)


def get_adapter(name: str) -> Adapter:
    if name not in ADAPTERS:
        raise UnknownToolError(
            f"Unknown tool '{name}'. Valid tools: {', '.join(all_names())}"
        )
    return _load_class(name)()


def detect_installed() -> list[Adapter]:
    """Adapters whose tool is installed on this machine (used by the join picker)."""
    found: list[Adapter] = []
    for name in all_names():
        try:
            adapter = get_adapter(name)
        except ImportError:
            continue  # adapter file missing or broken; don't break the picker
        if adapter.detect():
            found.append(adapter)
    return found
