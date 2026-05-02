"""
list_dir tool (parallel port from tools.py).

Lists directory contents with / marker for dirs. Hard-scoped to project root.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..context import ToolContext, ToolResult


def _resolve_in_project(ctx: ToolContext, relpath: str) -> Path:
    """Resolve a relpath, refusing anything that escapes the project root."""
    root = ctx.project.project_root.resolve()
    target = (root / relpath).resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"path escapes project root: {relpath}")
    return target


def t_list_dir(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = _resolve_in_project(ctx, args.get("path", "."))
    if not path.is_dir():
        return ToolResult(f"not a directory: {args.get('path', '.')}", is_error=True)
    entries = []
    for child in sorted(path.iterdir()):
        marker = "/" if child.is_dir() else ""
        entries.append(f"{child.name}{marker}")
    return ToolResult("\n".join(entries) or "(empty)")
