"""edit_file tool (with fuzzy whitespace fallback) — ported to tools_new."""

from __future__ import annotations

import re
from typing import Any, Optional

from .context import ToolContext, ToolResult
from .helpers import _resolve_in_project, _mark_dirty


def _normalize_indent(text: str, start: int, new: str) -> str:
    """When fuzzy-matching swaps content, drop any leading whitespace the
    model put on `new` if the matched span starts after the line's existing
    indent.

    The bug this fixes: fuzzy regex matches "from xli.cost import X"
    identically whether the file has 8-space, tab, or mixed indentation. The
    model's `new_string` carries its own (possibly wrong) leading whitespace —
    and `text[:m.start()]` already contains the file's actual indent for that
    line. Splicing `new` in verbatim therefore *doubles* the indent
    (8-spaces + tab + content), which is the exact garbage we observed in
    trace-005/006 that broke imports and triggered the recovery cascade.

    The right fix is to strip `new`'s leading whitespace in the "match starts
    at the line's indent" case — the file's indent is already preserved in
    text[:m.start()] so adding more is wrong every time.

    Scope: single-line `new` only. For multi-line `new` we trust the model.
    """
    if "\n" in new:
        return new
    line_start = text.rfind("\n", 0, start) + 1
    matched_indent = text[line_start:start]
    # Strip new's leading whitespace whenever the matched span starts at the
    # logical beginning of a line — either at column 0 or right after pure
    # whitespace indent. In both cases text[:start] already preserves the
    # file's actual indent (empty, tabs, spaces — whatever it is) and any
    # leading whitespace on `new` is additive: it doubles existing indent or
    # invents indent where none belongs. The trace-007 case was a column-0
    # top-level import where the model's `new` had a spurious tab — exactly
    # the bug an empty-indent guard misses.
    if matched_indent.strip() != "":
        # Match starts mid-line after non-whitespace — leave new as-is.
        return new
    return new.lstrip(" \t")


def _whitespace_fuzzy_pattern(old: str) -> Optional[re.Pattern]:
    """Build a regex that matches `old` with whitespace runs treated as `\\s+`.

    Models frequently lose or gain a tab/space when echoing a file fragment
    back into edit_file's old_string — exact matching then fails and triggers
    a cascade of re-reads. Collapsing whitespace runs eliminates the most
    common class of failure without changing semantic intent.

    Returns None when the input is empty or the constructed pattern is
    invalid (defensive — callers fall back to the exact-match failure path).
    """
    if not old or not old.strip():
        return None
    parts = [p for p in re.split(r"\s+", old) if p]
    if not parts:
        return None
    pat = r"\s+".join(re.escape(p) for p in parts)
    try:
        return re.compile(pat, re.DOTALL)
    except re.error:
        return None


def t_edit_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    rel = args["path"]
    path = _resolve_in_project(ctx, rel)
    if not path.exists():
        return ToolResult(f"file not found: {rel}", is_error=True)
    text = path.read_text(encoding="utf-8")
    old = args["old_string"]
    new = args["new_string"]
    replace_all = bool(args.get("replace_all", False))

    # Fast path: exact substring match (the historical behavior).
    if old in text:
        if not replace_all and text.count(old) > 1:
            ctx._edit_failures[rel] = ctx._edit_failures.get(rel, 0) + 1
            return ToolResult(
                f"old_string is not unique ({text.count(old)} occurrences); "
                "use replace_all=true or include more context",
                is_error=True,
            )
        new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        path.write_text(new_text, encoding="utf-8")
        _mark_dirty(ctx, path)
        ctx._edit_failures.pop(rel, None)
        return ToolResult(f"edited {rel}")

    # Fuzzy fallback: whitespace-tolerant match. Cuts the failure cascade
    # we measured (1.16M-tok runaway turn was driven by 6 exact-match
    # failures, each triggering 2-3 diagnostic reads).
    rx = _whitespace_fuzzy_pattern(old)
    matches = list(rx.finditer(text)) if rx is not None else []
    if matches:
        if not replace_all and len(matches) > 1:
            ctx._edit_failures[rel] = ctx._edit_failures.get(rel, 0) + 1
            return ToolResult(
                f"old_string (whitespace-tolerant match) found {len(matches)} occurrences; "
                "use replace_all=true or include more context",
                is_error=True,
            )
        if replace_all:
            new_text = rx.sub(lambda _m: new, text)
        else:
            m = matches[0]
            new_text = text[: m.start()] + _normalize_indent(text, m.start(), new) + text[m.end() :]
        path.write_text(new_text, encoding="utf-8")
        _mark_dirty(ctx, path)
        ctx._edit_failures.pop(rel, None)
        return ToolResult(f"edited {rel} (whitespace-tolerant match)")

    # Both exact and fuzzy failed. Track the failure and, on the second
    # consecutive miss, attach a head of the current file content so the
    # model has ground truth in the same iteration — no read round-trip.
    fails = ctx._edit_failures.get(rel, 0) + 1
    ctx._edit_failures[rel] = fails
    if fails >= 2:
        # Cap at ~1500 chars: agent.py's MAX_TOOL_RESULT_CHARS truncates
        # tool results in history anyway, but the current iteration sees
        # the full payload, which is what matters for breaking the spiral.
        head_lines = text.splitlines()[:30]
        head = "\n".join(f"{i+1:6}\t{ln}" for i, ln in enumerate(head_lines))
        if len(head) > 1500:
            head = head[:1500] + "\n[... truncated; read_file for more]"
        return ToolResult(
            f"old_string not found in {rel} (consecutive failure #{fails}). "
            f"current file head:\n{head}",
            is_error=True,
        )
    return ToolResult(f"old_string not found in {rel}", is_error=True)
