from __future__ import annotations

from typing import Any

from .context import ToolContext, ToolResult
from .helpers import _truncate


def t_search_project(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = args["query"]
    limit = int(args.get("limit", 6))
    mode = args.get("retrieval_mode", ctx.cfg.retrieval_mode)

    # Build the collection set: project's own + any /ref-attached personas.
    # Filter empty (local-only projects have empty collection_id) and dedupe
    # while preserving order so the project's own results come first.
    seen: set[str] = set()
    collection_ids: list[str] = []
    for cid in [ctx.project.collection_id, *ctx.extra_collection_ids]:
        if cid and cid not in seen:
            seen.add(cid)
            collection_ids.append(cid)
    if not collection_ids:
        return ToolResult(
            "(no collections to search — local-only project with no /ref attachments)",
            is_error=True,
        )
    try:
        resp = ctx.clients.xai.collections.search(
            query=query,
            collection_ids=collection_ids,
            limit=limit,
            retrieval_mode=mode,
        )
    except Exception as e:
        return ToolResult(f"search failed: {e}", is_error=True)
    chunks = list(getattr(resp, "results", None) or getattr(resp, "chunks", []) or [])
    if not chunks:
        return ToolResult("(no results)")
    out = []
    for i, ch in enumerate(chunks, 1):
        # SearchResponse chunk shape may have .chunk.text and .file_metadata.name
        name = getattr(getattr(ch, "file_metadata", None), "name", "?")
        text = (
            getattr(ch, "text", None)
            or getattr(getattr(ch, "chunk", None), "text", "")
            or ""
        )
        score = getattr(ch, "score", None)
        header = f"[{i}] {name}" + (f"  (score={score:.3f})" if isinstance(score, float) else "")
        chunk_text = text.strip()
        if len(chunk_text) > 900:
            chunk_text = chunk_text[:850] + "\n... [truncated]"
        out.append(header + "\n" + chunk_text)
    return ToolResult(_truncate("\n\n---\n\n".join(out)))
