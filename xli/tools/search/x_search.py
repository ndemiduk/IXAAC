from __future__ import annotations

from typing import Any

from ..context import ToolContext, ToolResult
from ..helpers import _truncate


def t_x_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from xli.server_tools import x_search
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult("x_search: 'query' is required", is_error=True)
    try:
        text = x_search(
            ctx.clients, ctx.cfg, query,
            allowed_x_handles=args.get("allowed_x_handles") or None,
            excluded_x_handles=args.get("excluded_x_handles") or None,
            from_date=args.get("from_date") or None,
            to_date=args.get("to_date") or None,
            enable_image_understanding=args.get("enable_image_understanding"),
            enable_video_understanding=args.get("enable_video_understanding"),
            sink=ctx,
        )
    except Exception as e:
        return ToolResult(f"x_search failed: {type(e).__name__}: {e}", is_error=True)
    return ToolResult(_truncate(text))
