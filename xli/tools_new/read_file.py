"""t_read_file implementation (read-only with offset/limit + numbering)."""

from __future__ import annotations

from typing import Any

from .context import ToolContext, ToolResult
from .helpers import _is_ignored, _resolve_in_project, _truncate


def t_read_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = _resolve_in_project(ctx, args["path"])
    if not path.exists():
        return ToolResult(f"file not found: {args['path']}", is_error=True)
    if not path.is_file():
        return ToolResult(f"not a file: {args['path']}", is_error=True)
    rel = path.relative_to(ctx.project.project_root.resolve()).as_posix()
    if _is_ignored(ctx, rel):
        return ToolResult(
            f"refused: {args['path']} is in the project's ignore list "
            "(.gitignore / .xliignore / built-in defaults). Likely contains "
            "secrets, build artifacts, or non-content data the user does not "
            "want exposed. If you genuinely need this file, ask the user — "
            "they can move it or whitelist it via .xliignore negation rules.",
            is_error=True,
        )
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return ToolResult(f"read error: {e}", is_error=True)
    offset = int(args.get("offset", 0))
    limit = int(args.get("limit", 0)) or None
    lines = text.splitlines()
    if offset or limit:
        end = offset + limit if limit else len(lines)
        lines = lines[offset:end]
    numbered = "\n".join(f"{i+offset+1:6}\t{ln}" for i, ln in enumerate(lines))
    return ToolResult(_truncate(numbered))
