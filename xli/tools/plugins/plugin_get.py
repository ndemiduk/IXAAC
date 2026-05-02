from __future__ import annotations

from typing import Any

from ..context import ToolContext, ToolResult
from ..helpers import _truncate


def t_plugin_get(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from xli.plugin import Plugin
    name = (args.get("name") or "").strip()
    mode = (args.get("mode") or "full").strip().lower()
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
        raw = p.read_raw()
    except OSError as e:
        return ToolResult(f"read failed: {e}", is_error=True)

    if mode == "manifest":
        # Return only metadata + structured actions (token-efficient)
        meta = p.metadata()
        manifest = p.manifest()
        if manifest:
            actions = "\n".join(
                f"  - {a.id}: {a.description}\n    params: {list(a.params.keys())}"
                for a in manifest.actions
            )
            content = f"---\nname: {meta.get('name')}\ndescription: {meta.get('description')}\nactions:\n{actions}\n---\n(manifest only; use plugin_call for execution)"
        else:
            content = "(no actions manifest — this is a legacy plugin; use mode=full)"
        return ToolResult(content)

    if mode == "condensed":
        # Metadata + first ~300 chars of body
        meta = p.metadata()
        body = p.body()[:300] + ("..." if len(p.body()) > 300 else "")
        content = f"Plugin: {meta.get('name', name)}\nDescription: {meta.get('description', '')}\n\n{body}"
        return ToolResult(_truncate(content))

    # default: full raw (backward compatible)
    return ToolResult(_truncate(raw))
