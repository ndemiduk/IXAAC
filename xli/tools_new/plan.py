"""
Plan-mode scratchpad tools (parallel port).

Hard-scoped append-only notes for long-running investigations.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .context import ToolContext, ToolResult

PLAN_NOTES_FILENAME = "plan-notes.md"
PLANS_ARCHIVE_DIR = "plans"


def t_plan_note(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Append a timestamped note to the plan-mode scratchpad.

    Hard-scoped: the path is fixed (<project>/.xli/plan-notes.md), there is
    no path argument, no edit, no delete. Append-only is load-bearing — it
    means the planner cannot accidentally clobber its own earlier notes.
    Optional `return_notes_after` returns the full current scratchpad content
    after the append (useful for staying coherent without a separate call).
    """
    text = (args.get("text") or "").strip()
    if not text:
        return ToolResult("plan_note: 'text' is required", is_error=True)

    return_notes = bool(args.get("return_notes_after", False))

    notes_path = ctx.project.xli_dir / PLAN_NOTES_FILENAME
    notes_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    block = f"\n## {ts}\n\n{text}\n"
    with notes_path.open("a", encoding="utf-8") as f:
        f.write(block)
    if return_notes:
        try:
            content = notes_path.read_text(encoding="utf-8").strip()
            return ToolResult(content or "(empty)")
        except OSError as e:
            return ToolResult(f"read error: {e}", is_error=True)
    return ToolResult("note appended")


def t_read_plan_notes(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Return the current content of .xli/plan-notes.md.

    PLAN-MODE ONLY. Lets the agent re-read its own scratchpad after several
    plan_note calls instead of only seeing the snapshot injected at turn start.
    """
    notes_path = ctx.project.xli_dir / PLAN_NOTES_FILENAME
    if not notes_path.exists():
        return ToolResult("(empty — first plan_note call will create it)")
    try:
        content = notes_path.read_text(encoding="utf-8").strip()
        return ToolResult(content or "(empty)")
    except OSError as e:
        return ToolResult(f"read error: {e}", is_error=True)


__all__ = ["t_plan_note", "t_read_plan_notes", "PLAN_NOTES_FILENAME", "PLANS_ARCHIVE_DIR"]
