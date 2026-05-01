"""write_file tool — ported to tools_new."""

from __future__ import annotations

from typing import Any

from .context import ToolContext, ToolResult
from .helpers import _resolve_in_project, _mark_dirty


def t_write_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = _resolve_in_project(ctx, args["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args["content"], encoding="utf-8")
    _mark_dirty(ctx, path)
    return ToolResult(f"wrote {args['path']} ({len(args['content'])} bytes)")
