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
    out = [
        "Top matches. INVOCATION RULES:",
        "  1. For matches that show `actions:` below — call plugin_call directly. You have everything you need. Do NOT call plugin_get on these.",
        "  2. For matches marked [legacy] (no manifest) — use plugin_get(mode='full') then bash.",
        "  3. NEVER use bash/curl for an HTTP request that a listed action covers — vault secrets are not in $env, only plugin_call injects them.",
        "",
    ]
    for p, score in matches:
        cats = ", ".join(p.categories()) or "—"
        manifest = p.manifest()
        if manifest:
            effect = manifest.effect
            out.append(
                f"- [{p.id}] (score={score:.1f}, effect={effect}, categories={cats})\n"
                f"    {p.name()}: {p.description() or '(no description)'}\n"
                f"    actions:"
            )
            for a in manifest.actions:
                user_params = [
                    name for name, spec in a.params.items()
                    if spec.const is None
                ]
                param_str = ", ".join(user_params) if user_params else "(none)"
                out.append(f"      {a.id}({param_str}) — {a.description}")
            # Recommended invocation (copy-paste ready for the first action)
            first_action = manifest.actions[0].id if manifest.actions else "ACTION"
            example_params = "{...}" if any(a.params for a in manifest.actions) else "{}"
            out.append(f"    → plugin_call(plugin=\"{p.id}\", action=\"{first_action}\", params={example_params})")
        else:
            out.append(
                f"- [{p.id}] (score={score:.1f}, risk={p.risk()}, categories={cats}) [legacy — use plugin_get + bash, no manifest]\n"
                f"    {p.name()}: {p.description() or '(no description)'}"
            )
    return ToolResult("\n".join(out))
