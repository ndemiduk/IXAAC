"""
summarize_file tool — structural summary using AST for Python files.
Ported to tools_new; uses shared helpers for safety/ignore/truncate.
"""

from __future__ import annotations

import ast
from typing import Any

from .context import ToolContext, ToolResult
from .helpers import _truncate, _resolve_in_project, _is_ignored


def t_summarize_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Compact structural summary of a file instead of dumping raw source.

    Greatly reduces token usage during investigation. Use `focus` to get
    exactly what you need (e.g. "signatures", "imports", "classes", "functions").
    Much lower token cost than read_file.
    """
    path = _resolve_in_project(ctx, args["path"])
    if not path.exists() or not path.is_file():
        return ToolResult(f"file not found: {args['path']}", is_error=True)

    rel = path.relative_to(ctx.project.project_root.resolve()).as_posix()
    if _is_ignored(ctx, rel):
        return ToolResult(
            f"refused: {args['path']} is in the project's ignore list",
            is_error=True,
        )

    focus = (args.get("focus") or "all").lower().strip()

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return ToolResult(f"read error: {e}", is_error=True)

    lines = text.splitlines()
    total_lines = len(lines)

    if path.suffix == ".py":
        summary = _summarize_python_file(rel, text, total_lines, focus)
    else:
        summary = _summarize_generic(rel, lines, total_lines)

    return ToolResult(_truncate(summary))


def _summarize_python_file(rel: str, text: str, total_lines: int, focus: str = "all") -> str:
    """Python-specific summary using AST.

    Returns a navigation index — every class, method, and top-level function
    annotated with its starting line number and length. The model can use
    this output to call read_file with a precise (offset, limit), eliminating
    the "peek → decide → peek" cycle on large modules. Adding line numbers
    is what turns summarize_file from a partial sketch into a usable map.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _summarize_generic(rel, text.splitlines(), total_lines)

    def _signature(fn) -> str:
        """Render a callable's argument list compactly. Skips type annotations
        to keep tokens low — the goal is locating, not type-checking."""
        parts: list[str] = []
        for arg in fn.args.args:
            parts.append(arg.arg)
        if fn.args.vararg:
            parts.append("*" + fn.args.vararg.arg)
        if fn.args.kwonlyargs:
            if not fn.args.vararg:
                parts.append("*")
            parts.extend(a.arg for a in fn.args.kwonlyargs)
        if fn.args.kwarg:
            parts.append("**" + fn.args.kwarg.arg)
        return f"{fn.name}({', '.join(parts)})"

    def _doc_first_line(node) -> str:
        doc = ast.get_docstring(node) or ""
        return doc.split("\n", 1)[0].strip()[:80]

    def _span(node) -> tuple[int, int]:
        """(start_line, length_in_lines). Uses ast.end_lineno (Python 3.8+)."""
        start = node.lineno
        end = getattr(node, "end_lineno", None) or start
        return start, max(end - start + 1, 1)

    imports: list[str] = []
    classes: list[dict] = []
    functions: list[dict] = []
    constants: list[dict] = []  # uppercase top-level names; module-level state

    func_types = (ast.FunctionDef, ast.AsyncFunctionDef)

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = ", ".join(a.name for a in node.names)
            imports.append(f"from {mod} import {names}")
        elif isinstance(node, ast.ClassDef):
            line, length = _span(node)
            bases = [b.id if isinstance(b, ast.Name) else ast.unparse(b) for b in node.bases]
            methods: list[dict] = []
            for child in node.body:
                if isinstance(child, func_types):
                    m_line, m_len = _span(child)
                    methods.append({
                        "sig": _signature(child),
                        "line": m_line,
                        "len": m_len,
                        "doc": _doc_first_line(child),
                    })
            classes.append({
                "name": node.name,
                "bases": bases,
                "line": line,
                "len": length,
                "doc": _doc_first_line(node),
                "methods": methods,
            })
        elif isinstance(node, func_types):
            line, length = _span(node)
            functions.append({
                "sig": _signature(node),
                "line": line,
                "len": length,
                "doc": _doc_first_line(node),
            })
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    constants.append({
                        "name": target.id,
                        "line": node.lineno,
                    })

    # Build output based on focus
    out: list[str] = [f"File: {rel} ({total_lines} lines)"]

    if focus in ("all", "imports") and imports:
        out.append("\nImports:")
        for imp in imports:
            out.append(f"  {imp}")

    if focus in ("all", "constants") and constants:
        out.append("\nConstants:")
        for c in constants:
            out.append(f"  {c['name']} @ L{c['line']}")

    if focus in ("all", "classes") and classes:
        out.append("\nClasses:")
        for cls in classes:
            bases = f"({', '.join(cls['bases'])})" if cls["bases"] else ""
            c_doc = f" — {cls['doc']}" if cls["doc"] else ""
            out.append(f"  {cls['name']}{bases} @ L{cls['line']} ({cls['len']} lines){c_doc}")
            for m in cls.get("methods", []):
                m_doc = f" — {m['doc']}" if m["doc"] else ""
                out.append(f"    {m['sig']} @ L{m['line']} ({m['len']} lines){m_doc}")

    if focus in ("all", "functions") and functions:
        out.append("\nFunctions:")
        for fn in functions:
            f_doc = f" — {fn['doc']}" if fn["doc"] else ""
            out.append(f"  {fn['sig']} @ L{fn['line']} ({fn['len']} lines){f_doc}")

    if focus in ("all", "signatures"):
        # signatures only mode: just list all callables with lines
        out = [f"File: {rel} ({total_lines} lines)"]
        if classes:
            out.append("\nClasses + methods:")
            for cls in classes:
                out.append(f"  {cls['name']} @ L{cls['line']} ({cls['len']} lines)")
                for m in cls.get("methods", []):
                    out.append(f"    {m['sig']} @ L{m['line']} ({m['len']} lines)")
        if functions:
            out.append("\nTop-level functions:")
            for fn in functions:
                out.append(f"  {fn['sig']} @ L{fn['line']} ({fn['len']} lines)")

    # Defensive: if a focus filter blanked everything, fall back to full layout.
    if len(out) == 1:
        return _summarize_python_file(rel, text, total_lines, focus="all")

    return "\n".join(out)


def _summarize_generic(rel: str, lines: list[str], total_lines: int) -> str:
    """Generic fallback for non-Python or when AST fails."""
    head = "\n".join(f"{i+1:4}: {ln}" for i, ln in enumerate(lines[:25]))
    tail_start = max(1, total_lines - 7)
    tail = "\n".join(f"{i+1:4}: {ln}" for i, ln in enumerate(lines[-8:], start=tail_start))
    return f"File: {rel} ({total_lines} lines)\n\n--- head ---\n{head}\n\n--- tail ---\n{tail}"
