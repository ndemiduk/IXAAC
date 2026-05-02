from __future__ import annotations

from typing import Any

from ..context import ToolContext, ToolResult
from ..helpers import _truncate


def t_plugin_get(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from xli.plugin import Plugin
    name = (args.get("name") or "").strip()
    if not name:
        return ToolResult("plugin_get: 'name' is required", is_error=True)
    if name not in ctx.subscribed_plugins:
        return ToolResult(
            f"plugin {name!r} is not subscribed for this project. "
            f"Subscribed: {ctx.subscribed_plugins or '(none)'}. "
            f"Use plugin_search first or ask the user to /lib subscribe {name}.",
            is_error=True,
        )
    p = Plugin(id=name)
    if not p.exists():
        return ToolResult(
            f"plugin {name!r} is subscribed but the file is missing on disk "
            "(orphan subscription). Tell the user — do NOT fabricate.",
            is_error=True,
        )
    try:
        text = p.read_raw()
    except OSError as e:
        return ToolResult(f"read failed: {e}", is_error=True)
    return ToolResult(_truncate(text))
