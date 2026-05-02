"""Shared helpers extracted from xli/tools.py for the new modular registry.

These are used by read-only tools (glob, grep, read_file, summarize, etc.)
and keep the same ignore / truncation / safety behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import re

from .context import ToolContext


MAX_OUTPUT_BYTES = 10_000


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_BYTES:
        return text
    keep = MAX_OUTPUT_BYTES // 2
    return (
        text[:keep]
        + f"\n\n... [truncated {len(text) - MAX_OUTPUT_BYTES} bytes] ...\n\n"
        + text[-keep:]
    )


def _resolve_in_project(ctx: ToolContext, relpath: str) -> Path:
    """Resolve a relpath, refusing anything that escapes the project root."""
    root = ctx.project.project_root.resolve()
    target = (root / relpath).resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"path escapes project root: {relpath}")
    return target


def _mark_dirty(ctx: ToolContext, path: Path) -> None:
    rel = path.resolve().relative_to(ctx.project.project_root.resolve()).as_posix()
    ctx.dirty_paths.add(rel)


def _ignore_spec(ctx: ToolContext):
    """Lazily load + cache the project's ignore spec on the ToolContext.

    Used by the read tools (read_file, grep, glob) so that .env, secrets,
    .gitignored content, build outputs, etc. are not silently exposed to the
    agent — same surface that the sync engine excludes from the Collection.
    """
    spec = getattr(ctx, "_ignore_spec_cache", None)
    if spec is None:
        from xli.ignore import load_ignore_spec
        extras = list(getattr(ctx.project, "extra_ignores", None) or [])
        spec = load_ignore_spec(ctx.project.project_root, extras)
        # Stash on the context so subsequent tools in the same turn reuse it.
        ctx._ignore_spec_cache = spec
    return spec


def _is_ignored(ctx: ToolContext, relpath: str) -> bool:
    """Check whether a project-relative path matches the ignore spec.
    Tests both file form ("foo/bar.env") and dir form ("foo/bar/") so that
    directory rules like ".env/" cover their contents too."""
    spec = _ignore_spec(ctx)
    return spec.match_file(relpath) or spec.match_file(relpath + "/")

# --- edit_file helpers (whitespace-tolerant matching) ---

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
