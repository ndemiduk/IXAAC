"""t_glob implementation (read-only file listing via pattern)."""

from __future__ import annotations

import fnmatch
from typing import Any

from ..context import ToolContext, ToolResult
from ..helpers import _is_ignored, _truncate


def t_glob(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pattern = args["pattern"]
    root = ctx.project.project_root
    matches = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if not fnmatch.fnmatch(rel, pattern):
            continue
        # Same ignore filter as sync — keeps secrets / build outputs out of
        # results even though glob is read-only.
        if _is_ignored(ctx, rel):
            continue
        matches.append(rel)
    matches.sort()
    if not matches:
        return ToolResult("(no matches)")
    return ToolResult(_truncate("\n".join(matches)))
