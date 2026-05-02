"""
Core types for tool execution.

ToolContext and ToolResult live here. This version matches the original
contract used by agent.py and the tool implementations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from xli.client import Clients
from xli.config import GlobalConfig, ProjectConfig


@dataclass
class ToolResult:
    """Result returned by every tool."""
    content: str
    is_error: bool = False


@dataclass
class ToolContext:
    """Execution context passed to all tools.

    This must match the original ToolContext contract exactly so that
    agent.py and the individual tool functions continue to work without
    AttributeError.
    """
    project: ProjectConfig
    clients: Clients
    cfg: GlobalConfig
    pool: Any = None        # ClientPool when running in main agent; None for workers
    console: Any = None     # rich.Console for ad-hoc UI; None in workers
    dirty_paths: set[str] = field(default_factory=set)
    yolo: bool = False      # skip per-intent confirmation gate when True
    is_worker: bool = False # workers cannot run intent > read-only
    # Additional collection_ids to include in search_project alongside the
    # project's own collection. Populated from Agent.attached_refs.
    extra_collection_ids: list[str] = field(default_factory=list)
    # Subscribed plugin IDs for this session.
    subscribed_plugins: list[str] = field(default_factory=list)
    # Server-tool sub-call accounting (used by agent for cost tracking)
    server_prompt_tokens: int = 0
    server_completion_tokens: int = 0
    server_cost: float = 0.0
    server_calls: int = 0
    # Simple per-turn result cache for read-only / deterministic tools.
    _tool_result_cache: dict = field(default_factory=dict)
    # Per-file consecutive edit_file failure counter. Reset on success.
    # NOTE: must be named _edit_failures (not _edit_failure_counts) because
    # edit_file.py still references ctx._edit_failures.
    _edit_failures: dict = field(default_factory=dict)

    def record_server_usage(
        self, model: str, prompt_tokens: int, completion_tokens: int
    ) -> None:
        from xli.cost import estimate_cost
        self.server_prompt_tokens += prompt_tokens
        self.server_completion_tokens += completion_tokens
        c = estimate_cost(self.cfg.pricing, model, prompt_tokens, completion_tokens)
        if c is not None:
            self.server_cost += c
        self.server_calls += 1

    def drain_server_usage(self) -> tuple[int, int, float, int]:
        """Return accumulated server-tool usage and reset the counters."""
        out = (
            self.server_prompt_tokens,
            self.server_completion_tokens,
            self.server_cost,
            self.server_calls,
        )
        self.server_prompt_tokens = 0
        self.server_completion_tokens = 0
        self.server_cost = 0.0
        self.server_calls = 0
        return out

    def _get_cached_result(self, name: str, args: dict) -> Optional[str]:
        """Return cached result text for a deterministic read-only tool call, or None."""
        if name not in {"read_file", "list_dir", "glob", "grep", "search_project",
                        "read_plan_notes", "plugin_search", "plugin_get"}:
            return None
        try:
            key = (name, json.dumps(args, sort_keys=True))
        except Exception:
            return None
        return self._tool_result_cache.get(key)

    def _cache_result(self, name: str, args: dict, result_text: str) -> None:
        """Store result for future identical calls in this turn."""
        if name not in {"read_file", "list_dir", "glob", "grep", "search_project",
                        "read_plan_notes", "plugin_search", "plugin_get"}:
            return
        try:
            key = (name, json.dumps(args, sort_keys=True))
            self._tool_result_cache[key] = result_text
        except Exception:
            pass


__all__ = ["ToolContext", "ToolResult"]
