"""t_grep implementation (read-only regex search across files)."""

from __future__ import annotations

import fnmatch
import re
from typing import Any

from ..context import ToolContext, ToolResult
from ..helpers import _is_ignored, _truncate


def t_grep(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pattern = args["pattern"]
    glob_pat = args.get("glob")
    case_insensitive = bool(args.get("case_insensitive", False))
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return ToolResult(f"invalid regex: {e}", is_error=True)
    root = ctx.project.project_root
    out_lines: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if glob_pat and not fnmatch.fnmatch(rel, glob_pat):
            continue
        # Skip ignored paths so grep can't surface .env / build / secrets
        # content the sync engine excludes from the Collection.
        if _is_ignored(ctx, rel):
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if rx.search(line):
                        out_lines.append(f"{rel}:{i}: {line.rstrip()}")
        except OSError:
            continue
    if not out_lines:
        return ToolResult("(no matches)")
    return ToolResult(_truncate("\n".join(out_lines)))
