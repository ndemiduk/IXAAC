from __future__ import annotations

from typing import Any

from ..context import ToolContext, ToolResult


def t_plugin_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from xli.plugin import (
        NO_PLUGIN_MATCH_MARKER,
        Plugin,
        search_plugins,
    )
    intent = (args.get("intent") or "").strip()
    if not intent:
        return ToolResult("plugin_search: 'intent' is required", is_error=True)
    if not ctx.subscribed_plugins:
        return ToolResult(
            f"{NO_PLUGIN_MATCH_MARKER} for intent={intent!r}\n"
            "No plugins are subscribed for this project. The user can install/subscribe "
            "plugins with `xli plugin --new` and `/lib subscribe <id>`. "
            "Tell the user no plugin is available — do NOT fabricate plugin output."
        )
    available = [Plugin(id=pid) for pid in ctx.subscribed_plugins]
    available = [p for p in available if p.exists()]
    matches = search_plugins(intent, available, limit=5)
    if not matches:
        cats = sorted({c for p in available for c in p.categories()})
        cat_line = (
            "Categories of subscribed plugins: " + ", ".join(cats)
            if cats else
            "No plugin categories declared on subscribed plugins."
        )
        return ToolResult(
            f"{NO_PLUGIN_MATCH_MARKER} for intent={intent!r}\n"
            f"{cat_line}\n"
            "Suggest: install/subscribe a plugin that fits, or fall back to "
            "web_search/bash. Do NOT fabricate plugin output."
        )
    out = ["Top matches (call plugin_get for full content of any candidate):"]
    for p, score in matches:
        cats = ", ".join(p.categories()) or "—"
        out.append(
            f"- [{p.id}] (score={score:.1f}, risk={p.risk()}, categories={cats})\n"
            f"    {p.name()}: {p.description() or '(no description)'}"
        )
    return ToolResult("\n".join(out))
