"""Agent tool implementations.

Each tool is a function that takes (project_root, args_dict, ctx) and returns
ToolResult(content, dirty_paths). Dirty paths are queued for end-of-turn sync.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from xli.client import Clients
from xli.config import GlobalConfig, ProjectConfig

# How much of a long output to keep before truncating, per tool call.
MAX_OUTPUT_BYTES = 30_000


@dataclass
class ToolContext:
    project: ProjectConfig
    clients: Clients
    cfg: GlobalConfig
    pool: Any = None        # ClientPool when running in main agent; None for workers
    console: Any = None     # rich.Console for ad-hoc UI; None in workers
    dirty_paths: set[str] = field(default_factory=set)
    yolo: bool = False      # skip per-intent confirmation gate when True
    is_worker: bool = False # workers cannot run intent > read-only
    # Server-tool sub-call usage (Responses API). Server tools fire one-shot
    # responses.create() calls outside the main chat loop; the agent drains
    # these accumulators after each tool batch into its CallStats.
    server_prompt_tokens: int = 0
    server_completion_tokens: int = 0
    server_cost: float = 0.0
    server_calls: int = 0

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


# Bash intents — declared by the agent on every bash call so the human can
# see what the call is *meant* to do (transparency) and so we gate the
# riskier ones with a y/N prompt (cheap circuit-breaker for hallucinations).
INTENT_READ_ONLY = "read-only"             # ls, cat, grep, find, git status/log/diff, pytest
INTENT_MODIFIES_PROJECT = "modifies-project"  # git add/commit, file rewrites via shell, sed -i in tree
INTENT_MODIFIES_SYSTEM = "modifies-system"    # apt, sudo, anything outside project, system config
INTENT_NETWORK = "network"                  # curl, wget, pip install, npm install, git push/pull/fetch

VALID_INTENTS = {INTENT_READ_ONLY, INTENT_MODIFIES_PROJECT, INTENT_MODIFIES_SYSTEM, INTENT_NETWORK}
GATED_INTENTS = {INTENT_MODIFIES_SYSTEM, INTENT_NETWORK}


@dataclass
class ToolResult:
    content: str
    is_error: bool = False


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


# --------------------------------------------------------------------------- #
#  Tool implementations
# --------------------------------------------------------------------------- #

def t_read_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = _resolve_in_project(ctx, args["path"])
    if not path.exists():
        return ToolResult(f"file not found: {args['path']}", is_error=True)
    if not path.is_file():
        return ToolResult(f"not a file: {args['path']}", is_error=True)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return ToolResult(f"read error: {e}", is_error=True)
    offset = int(args.get("offset", 0))
    limit = int(args.get("limit", 0)) or None
    lines = text.splitlines()
    if offset or limit:
        end = offset + limit if limit else len(lines)
        lines = lines[offset:end]
    numbered = "\n".join(f"{i+offset+1:6}\t{ln}" for i, ln in enumerate(lines))
    return ToolResult(_truncate(numbered))


def t_write_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = _resolve_in_project(ctx, args["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args["content"], encoding="utf-8")
    _mark_dirty(ctx, path)
    return ToolResult(f"wrote {args['path']} ({len(args['content'])} bytes)")


def t_edit_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = _resolve_in_project(ctx, args["path"])
    if not path.exists():
        return ToolResult(f"file not found: {args['path']}", is_error=True)
    text = path.read_text(encoding="utf-8")
    old = args["old_string"]
    new = args["new_string"]
    replace_all = bool(args.get("replace_all", False))
    if old not in text:
        return ToolResult("old_string not found in file", is_error=True)
    if not replace_all and text.count(old) > 1:
        return ToolResult(
            f"old_string is not unique ({text.count(old)} occurrences); "
            "use replace_all=true or include more context",
            is_error=True,
        )
    new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    path.write_text(new_text, encoding="utf-8")
    _mark_dirty(ctx, path)
    return ToolResult(f"edited {args['path']}")


def t_list_dir(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = _resolve_in_project(ctx, args.get("path", "."))
    if not path.is_dir():
        return ToolResult(f"not a directory: {args.get('path', '.')}", is_error=True)
    entries = []
    for child in sorted(path.iterdir()):
        marker = "/" if child.is_dir() else ""
        entries.append(f"{child.name}{marker}")
    return ToolResult("\n".join(entries) or "(empty)")


def t_glob(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pattern = args["pattern"]
    root = ctx.project.project_root
    matches = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and fnmatch.fnmatch(p.relative_to(root).as_posix(), pattern)
    )
    if not matches:
        return ToolResult("(no matches)")
    return ToolResult(_truncate("\n".join(matches)))


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


def t_bash(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    cmd = args["command"]
    intent = args.get("intent", "")
    timeout = int(args.get("timeout", 60))

    # Validate intent (required, must be one of the four)
    if intent not in VALID_INTENTS:
        return ToolResult(
            f"bash refused: missing or invalid `intent` (got {intent!r}). "
            f"Required values: {sorted(VALID_INTENTS)}. Declare what this command is meant to do.",
            is_error=True,
        )

    # Workers may only run read-only commands.
    if ctx.is_worker and intent != INTENT_READ_ONLY:
        return ToolResult(
            f"bash refused: workers may only run intent={INTENT_READ_ONLY!r} commands; "
            f"got {intent!r}. Report this back to the orchestrator instead.",
            is_error=True,
        )

    # Gate riskier intents on a human y/N (skipped in yolo mode).
    if intent in GATED_INTENTS and not ctx.yolo:
        if ctx.console is None:
            return ToolResult(
                f"bash refused: intent={intent!r} requires confirmation but no console "
                "is attached (running headless). Use --yolo or simpler intent.",
                is_error=True,
            )
        ctx.console.print(
            f"  [yellow]⚠ intent={intent}[/yellow]  [bold]{cmd}[/bold]"
        )
        try:
            answer = input("  approve? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "y":
            return ToolResult(
                f"bash denied by user (intent={intent}). "
                "Try a different approach or ask the user for permission first.",
                is_error=True,
            )

    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=ctx.project.project_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(f"command timed out after {timeout}s", is_error=True)
    out = proc.stdout
    if proc.stderr:
        out += "\n--- stderr ---\n" + proc.stderr
    out += f"\n--- exit {proc.returncode} ---"
    # any successful command might have touched files
    if proc.returncode == 0:
        ctx.dirty_paths.add("__rescan__")
    return ToolResult(_truncate(out), is_error=proc.returncode != 0)


def t_web_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from xli.server_tools import web_search
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult("web_search: 'query' is required", is_error=True)
    try:
        text = web_search(
            ctx.clients, ctx.cfg, query,
            allowed_domains=args.get("allowed_domains") or None,
            excluded_domains=args.get("excluded_domains") or None,
            sink=ctx,
        )
    except Exception as e:
        return ToolResult(f"web_search failed: {type(e).__name__}: {e}", is_error=True)
    return ToolResult(_truncate(text))


def t_x_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from xli.server_tools import x_search
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult("x_search: 'query' is required", is_error=True)
    try:
        text = x_search(
            ctx.clients, ctx.cfg, query,
            allowed_x_handles=args.get("allowed_x_handles") or None,
            excluded_x_handles=args.get("excluded_x_handles") or None,
            from_date=args.get("from_date") or None,
            to_date=args.get("to_date") or None,
            enable_image_understanding=args.get("enable_image_understanding"),
            enable_video_understanding=args.get("enable_video_understanding"),
            sink=ctx,
        )
    except Exception as e:
        return ToolResult(f"x_search failed: {type(e).__name__}: {e}", is_error=True)
    return ToolResult(_truncate(text))


def t_code_execute(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from xli.server_tools import code_execute
    task = (args.get("task") or args.get("code") or "").strip()
    if not task:
        return ToolResult("code_execute: 'task' is required", is_error=True)
    try:
        text = code_execute(ctx.clients, ctx.cfg, task, sink=ctx)
    except Exception as e:
        return ToolResult(f"code_execute failed: {type(e).__name__}: {e}", is_error=True)
    return ToolResult(_truncate(text))


def t_search_project(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = args["query"]
    limit = int(args.get("limit", 10))
    mode = args.get("retrieval_mode", ctx.cfg.retrieval_mode)
    try:
        resp = ctx.clients.xai.collections.search(
            query=query,
            collection_ids=[ctx.project.collection_id],
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
        out.append(header + "\n" + text.strip())
    return ToolResult(_truncate("\n\n---\n\n".join(out)))


# --------------------------------------------------------------------------- #
#  Tool registry + JSON schema for tool-use
# --------------------------------------------------------------------------- #

ToolFn = Callable[[ToolContext, dict[str, Any]], ToolResult]

REGISTRY: dict[str, ToolFn] = {
    "read_file": t_read_file,
    "write_file": t_write_file,
    "edit_file": t_edit_file,
    "list_dir": t_list_dir,
    "glob": t_glob,
    "grep": t_grep,
    "bash": t_bash,
    "search_project": t_search_project,
    "web_search": t_web_search,
    "x_search": t_x_search,
    "code_execute": t_code_execute,
}

# Workers are read-only investigators: same toolset minus mutation + no swarm.
WORKER_REGISTRY: dict[str, ToolFn] = {
    name: fn
    for name, fn in REGISTRY.items()
    if name not in {"write_file", "edit_file"}
}

# Tools that are safe to execute concurrently within a single tool_calls batch.
# Read-only + idempotent. dispatch_subagent is included since worker fan-out
# is the original parallel use-case.
PARALLEL_SAFE: set[str] = {
    "read_file",
    "list_dir",
    "glob",
    "grep",
    "search_project",
    "web_search",
    "x_search",
    "code_execute",
    "dispatch_subagent",
}

# Tools available in PLAN MODE — strictly read-only, no shell, no fan-out.
# Bash and dispatch_subagent are excluded because both can mutate state.
PLAN_MODE_TOOLS: set[str] = {
    "read_file",
    "list_dir",
    "glob",
    "grep",
    "search_project",
    "web_search",
    "x_search",
}


def tool_schemas() -> list[dict]:
    """Return OpenAI-compatible tool schemas for chat.completions."""
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a UTF-8 text file from the project. Output is line-numbered.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Project-relative path"},
                        "offset": {"type": "integer", "description": "0-based start line"},
                        "limit": {"type": "integer", "description": "Max lines to return"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Create or overwrite a file with the given content. Marks file dirty for sync.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "Replace exact substring in a file. Errors if old_string is not unique unless replace_all=true.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "List immediate children of a directory.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "default '.'"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "glob",
                "description": "Recursive glob. Pattern uses fnmatch syntax against project-relative posix paths.",
                "parameters": {
                    "type": "object",
                    "properties": {"pattern": {"type": "string"}},
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "grep",
                "description": "Recursive regex search across project files. Returns 'path:line: match'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Python regex"},
                        "glob": {"type": "string", "description": "Optional fnmatch filter"},
                        "case_insensitive": {"type": "boolean"},
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": (
                    "Run a shell command in the project root. Capture stdout/stderr/exit code. "
                    "You MUST declare `intent` honestly — it's used to gate risky commands on user "
                    "confirmation. Lying about intent to skip the gate is a serious bug; declare "
                    "the strongest applicable category. Use `read-only` for inspection only "
                    "(ls, cat, grep, find, git status/log/diff, pytest). Use `modifies-project` "
                    "for in-tree edits via shell (git add/commit, sed -i on project files). Use "
                    "`modifies-system` for anything outside the project root, anything needing "
                    "sudo, or system config. Use `network` for any command that reaches the "
                    "internet (curl, wget, pip install, npm install, git push/pull/fetch)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "intent": {
                            "type": "string",
                            "enum": ["read-only", "modifies-project", "modifies-system", "network"],
                            "description": "Honest declaration of what this command will do.",
                        },
                        "timeout": {"type": "integer", "description": "Seconds, default 60"},
                    },
                    "required": ["command", "intent"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_project",
                "description": "Hybrid RAG search across the project's xAI Collection. Returns top-k matching chunks with file names.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "description": "default 10"},
                        "retrieval_mode": {
                            "type": "string",
                            "enum": ["hybrid", "semantic", "keyword"],
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": (
                    "Search the live web via xAI Live Search. Use for current docs, recent "
                    "library versions, breaking changes, error messages you can't resolve from "
                    "project context. Returns answer text + citations. Prefer search_project "
                    "for anything inside the project."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural-language search query"},
                        "allowed_domains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional whitelist of domains to search.",
                        },
                        "excluded_domains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional blacklist of domains to skip.",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "x_search",
                "description": (
                    "Search posts on X (Twitter) via xAI Live Search. Use for real-time "
                    "developer chatter, trending issues, official announcements from project "
                    "maintainers, or community sentiment about a library. Returns answer text "
                    "+ citations. Prefer web_search for stable docs and articles."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural-language query about X posts"},
                        "allowed_x_handles": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Restrict to these handles (max 10). e.g. ['xai', 'elonmusk']",
                        },
                        "excluded_x_handles": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Exclude these handles (max 10).",
                        },
                        "from_date": {"type": "string", "description": "ISO8601 start date (e.g. 2025-01-01)"},
                        "to_date": {"type": "string", "description": "ISO8601 end date"},
                        "enable_image_understanding": {"type": "boolean"},
                        "enable_video_understanding": {"type": "boolean"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "code_execute",
                "description": (
                    "Execute Python in xAI's sandbox to verify behavior, prototype logic, or "
                    "do computations. NumPy/Pandas/Matplotlib/SciPy preinstalled. Pass either "
                    "a description of what to compute or actual Python code. The model writes "
                    "and runs the code server-side and returns the result. For project-local "
                    "verification, use bash + python instead — this is for isolated snippets."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "Description of the computation, or Python code to run.",
                        },
                    },
                    "required": ["task"],
                },
            },
        },
    ]


def worker_tool_schemas() -> list[dict]:
    """Schemas for workers — same as main minus write_file/edit_file."""
    blocked = {"write_file", "edit_file"}
    return [s for s in tool_schemas() if s["function"]["name"] not in blocked]


def plan_mode_schemas() -> list[dict]:
    """Schemas available in plan mode — read-only investigation only."""
    return [s for s in tool_schemas() if s["function"]["name"] in PLAN_MODE_TOOLS]


def dispatch_subagent_schema() -> dict:
    """Schema for the dispatch_subagent tool — only available to the main agent."""
    return {
        "type": "function",
        "function": {
            "name": "dispatch_subagent",
            "description": (
                "Dispatch a read-only worker agent on a focused investigation task. "
                "Workers run in parallel when multiple are dispatched in the same batch. "
                "Workers can use search_project, read_file, list_dir, glob, grep, bash. "
                "They cannot modify files. Workers see ONLY the brief — write a tight, "
                "self-contained task description. Use `context` to pass relevant snippets. "
                "Returns the worker's final summary as a string."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The investigation task. Be specific about what you want back.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional snippets, file excerpts, or background the worker needs.",
                    },
                },
                "required": ["task"],
            },
        },
    }
