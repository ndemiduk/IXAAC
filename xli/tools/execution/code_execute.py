from __future__ import annotations

from typing import Any

from ..context import ToolContext, ToolResult
from ..helpers import _truncate


def t_code_execute(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from xli.server_tools import code_execute
    task = (args.get("task") or args.get("code") or "").strip()
    if not task:
        return ToolResult("code_execute: 'task' is required", is_error=True)
    try:
        text = code_execute(ctx.clients, ctx.cfg, task, sink=ctx)
    except Exception as e:
        return ToolResult(f"code_execute failed: {type(e).__name__}: {e}", is_error=True)
    return ToolResult(_truncate(text))
