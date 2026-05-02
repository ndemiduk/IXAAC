"""t_locate_then_read implementation (pattern search + contextual read in one call)."""

from __future__ import annotations

import fnmatch
import re
from typing import Any

from ..context import ToolContext, ToolResult
from ..helpers import _is_ignored, _truncate


def t_locate_then_read(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Pattern search + targeted contextual read in one call.

    Collapses the "grep + multiple read_file" pattern into a single
    tool round-trip. Returns each match annotated with line number plus
    a window of surrounding context. Default: up to 6 matches, 5 lines
    before and 25 after each. Output is bounded by _truncate (10kb).

    The win is structural rather than informational: even when the
    model has the right info to act, it tends to do "one more peek"
    cycles. This tool returns the peeks pre-batched so the cycle
    can't spawn — a single call delivers what 1 grep + N reads would.
    """
    pattern = args["pattern"]
    glob_pat = args.get("glob")
    case_insensitive = bool(args.get("case_insensitive", False))
    context_before = max(0, int(args.get("context_before", 5)))
    context_after = max(0, int(args.get("context_after", 25)))
    max_matches = max(1, int(args.get("max_matches", 6)))

    flags = re.IGNORECASE if case_insensitive else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return ToolResult(f"invalid regex: {e}", is_error=True)

    root = ctx.project.project_root
    matches: list[tuple[str, int]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if glob_pat and not fnmatch.fnmatch(rel, glob_pat):
            continue
        if _is_ignored(ctx, rel):
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if rx.search(line):
                        matches.append((rel, i))
                        # Cap at 3× requested so we can report "truncated"
                        if len(matches) >= max_matches * 3:
                            break
        except OSError:
            continue
        if len(matches) >= max_matches * 3:
            break

    if not matches:
        return ToolResult("(no matches)")

    truncated_total = len(matches)
    matches = matches[:max_matches]

    # One read per unique file, even if it has multiple matches.
    file_lines_cache: dict[str, list[str]] = {}
    for rel, _ in matches:
        if rel not in file_lines_cache:
            try:
                file_lines_cache[rel] = (root / rel).read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                file_lines_cache[rel] = []

    out: list[str] = []
    if truncated_total > max_matches:
        out.append(
            f"{truncated_total}+ matches found, showing first {max_matches}. "
            f"Narrow `pattern` / `glob` or raise `max_matches` for more."
        )
    else:
        out.append(f"{len(matches)} match{'es' if len(matches) != 1 else ''}.")
    out.append("")

    for rel, line_no in matches:
        file_lines = file_lines_cache.get(rel) or []
        if not file_lines:
            out.append(f"=== {rel}:{line_no} (could not read file) ===")
            continue
        start = max(1, line_no - context_before)
        end = min(len(file_lines), line_no + context_after)
        out.append(f"=== {rel}:{line_no}  (showing L{start}-{end}) ===")
        for i in range(start, end + 1):
            marker = "→" if i == line_no else " "
            out.append(f"{i:5}{marker} {file_lines[i - 1]}")
        out.append("")

    return ToolResult(_truncate("\n".join(out)))
