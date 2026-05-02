from __future__ import annotations

from typing import Any

from ..context import ToolContext, ToolResult
from ..helpers import _truncate


def t_web_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from xli.server_tools import web_search
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult("web_search: 'query' is required", is_error=True)
    try:
        text = web_search(
            ctx.clients, ctx.cfg, query,
            allowed_domains=args.get("allowed_domains") or None,
            excluded_domains=args.get("excluded_domains") or None,
            sink=ctx,
        )
    except Exception as e:
        return ToolResult(f"web_search failed: {type(e).__name__}: {e}", is_error=True)
    return ToolResult(_truncate(text))
