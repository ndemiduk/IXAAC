"""Agent loop: Grok chat completion with tool-use, until no more tool calls.

Two flavors:
  - Agent       : main interactive agent. Full tool set incl. dispatch_subagent.
  - WorkerAgent : read-only investigator dispatched by the main agent.
                  Tools: read_file, list_dir, glob, grep, bash, search_project.
                  No write_file / edit_file / dispatch_subagent.
                  Returns a single string summary, no conversation history retained.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Optional

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown

from xli.client import Clients
from xli.config import GlobalConfig, ProjectConfig
from xli.cost import estimate_cost
from xli.pool import ClientPool
from xli.tools import (
    PARALLEL_SAFE,
    REGISTRY,
    WORKER_REGISTRY,
    ToolContext,
    dispatch_subagent_schema,
    plan_mode_schemas,
    tool_schemas,
    worker_tool_schemas,
)

MAIN_SYSTEM_PROMPT = """You are XLI, a terminal-based coding agent operating inside a single project directory.

You have a working copy of the project on disk and a synchronized hybrid-RAG search index over it (search_project). Local files are the source of truth — when you write or edit, your changes are mirrored to the remote collection automatically at the end of your turn.

You can dispatch parallel worker agents via dispatch_subagent. Workers are read-only investigators with their own tool loop and access to the same project collection. Use them for: parallel file investigation across multiple modules, "go research X and Y and Z and report back", running tests + summarizing logs while you keep coding. Workers see ONLY the brief you give them, not your conversation — write tight, self-contained briefs. Use the optional `context` field to paste in snippets they need to reason about.

Conventions:
- Project paths are POSIX-style and relative to the project root.
- Prefer search_project + read_file before guessing. Use grep/glob for exact matches.
- Use edit_file for surgical changes; write_file only for new files or full rewrites.
- Be terse. Don't narrate; just do the work and report what changed.

Verification (mandatory before declaring success):
- After writing or editing files, verify the code at the smallest reasonable level using bash:
  · new module → `python -c "import <module>"` (or equivalent for the language)
  · new behavior → run a smoke test that exercises it
  · existing tests → run them
- When you change a file's imports, structure, or interfaces, verify the *consumers* still import cleanly — not just the file you touched.
- Never claim that code "works", "is ready to run", "is verified", or "passes tests" unless you have actually run something that proves it. State results, don't predict them.
- If verification fails, fix the issues before ending the turn. Do not hand off broken code with a promise it'll work.
- For UI/GUI/network code that can't be exercised headlessly, say so explicitly ("imports cleanly; GUI not testable from this environment") rather than claiming success."""


WORKER_SYSTEM_PROMPT = """You are a worker agent dispatched by XLI to investigate a focused task and return a concise summary.

You have read-only access to a project: search_project (hybrid RAG), read_file, list_dir, glob, grep, bash. You CANNOT modify files. You CANNOT dispatch further workers.

Do the work, then return a tight summary of findings. Cite file paths and line numbers. Don't pad."""


PREVIEW_LINE_LIMIT = 120  # max chars per preview line; tool output gets dimmed


def _strip_line_number_prefix(line: str) -> str:
    """t_read_file emits '     1\\tcontent'. Trim the prefix for previews."""
    if "\t" in line:
        head, _, rest = line.partition("\t")
        if head.strip().isdigit():
            return rest
    return line


def _trunc(s: str, n: int = PREVIEW_LINE_LIMIT) -> str:
    s = s.rstrip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _format_tool_preview(name: str, content: str, is_error: bool) -> list[str]:
    """Return 1-3 short dimmed preview lines for a tool result.

    Each tool's shape is tailored so the user sees the *gist* of the work
    (head, tail, count, top hit) without the full output flooding the terminal.
    The full content still goes to the model — this is purely visual feedback.
    """
    if is_error:
        first = (content or "").split("\n", 1)[0]
        return [f"  [red]⎿[/red] [red]{_trunc(first)}[/red]"] if first else []

    text = (content or "").rstrip()
    if not text:
        return []
    lines = text.splitlines()
    if not lines:
        return []

    if name == "read_file":
        n = len(lines)
        first = _strip_line_number_prefix(lines[0])
        return [f"  [dim]⎿ {n} line{'s' if n != 1 else ''} · {_trunc(first, 80)}[/dim]"]

    if name == "list_dir":
        if text == "(empty)":
            return ["  [dim]⎿ (empty)[/dim]"]
        sample = "  ".join(lines[:5])
        return [f"  [dim]⎿ {len(lines)} entries · {_trunc(sample)}[/dim]"]

    if name == "glob":
        if text == "(no matches)":
            return ["  [dim]⎿ no matches[/dim]"]
        sample = ", ".join(lines[:3])
        return [
            f"  [dim]⎿ {len(lines)} match{'es' if len(lines) != 1 else ''} · {_trunc(sample)}[/dim]"
        ]

    if name == "grep":
        if text == "(no matches)":
            return ["  [dim]⎿ no matches[/dim]"]
        out = [f"  [dim]⎿ {len(lines)} match{'es' if len(lines) != 1 else ''}[/dim]"]
        for ln in lines[:2]:
            out.append(f"  [dim]   {_trunc(ln)}[/dim]")
        return out

    if name == "bash":
        # Drop the synthetic "--- exit N ---" trailer; show last 1-3 lines
        # of real output. Tests / build commands put the verdict at the end.
        meaningful = [ln for ln in lines if not ln.startswith("--- exit ")]
        if not meaningful:
            return [f"  [dim]⎿ {_trunc(lines[-1])}[/dim]"]
        if len(meaningful) <= 3:
            return [f"  [dim]⎿ {_trunc(ln)}[/dim]" for ln in meaningful]
        return [f"  [dim]⎿ … {_trunc(meaningful[-3])}[/dim]"] + [
            f"  [dim]   {_trunc(ln)}[/dim]" for ln in meaningful[-2:]
        ]

    if name == "search_project":
        for ln in lines:
            if ln.startswith("[1]"):
                return [f"  [dim]⎿ {_trunc(ln)}[/dim]"]
        return []

    if name in ("web_search", "x_search"):
        for ln in lines:
            s = ln.strip()
            if s and not s.startswith("---"):
                return [f"  [dim]⎿ {_trunc(s)}[/dim]"]
        return []

    if name == "code_execute":
        if len(lines) <= 2:
            return [f"  [dim]⎿ {_trunc(ln)}[/dim]" for ln in lines if ln.strip()]
        return [
            f"  [dim]⎿ {_trunc(lines[0])}[/dim]",
            f"  [dim]   … {_trunc(lines[-1])}[/dim]",
        ]

    if name == "dispatch_subagent":
        for ln in lines:
            if ln.startswith("---"):  # skip the worker[...] header
                continue
            s = ln.strip()
            if s:
                return [f"  [dim]⎿ {_trunc(s)}[/dim]"]
        return []

    # write_file, edit_file: tool result text already self-narrates
    # ("wrote foo.py (123 bytes)" / "edited foo.py"), no preview needed.
    return []


PLAN_MODE_PREAMBLE = """[PLAN MODE ACTIVE]
You cannot modify project content in this turn. Your tools are read-only investigation (read_file, list_dir, glob, grep, search_project, web_search, x_search, plugin_search, plugin_get) plus one scoped write tool: plan_note. No write_file, no edit_file, no bash, no dispatch_subagent.

plan_note appends to a scratchpad at .xli/plan-notes.md that survives across iterations and across /exit. USE IT. Capture intermediate findings as you go: files you've checked, things you've ruled out, open questions, partial conclusions. If this turn hits the iteration cap, future-you will resume from those notes — without them, all your investigation evaporates.

Investigate as needed, then output a numbered, concrete plan describing exactly what changes you would make and why. The user will review and either approve, refine, or cancel before any change happens.
"""

PLAN_MODE_NOTES_HEADER = "\n\n## Your scratchpad so far (.xli/plan-notes.md)\n\n"
PLAN_MODE_NOTES_EMPTY = "(empty — first plan_note call will create it)"
PLAN_MODE_USER_HEADER = "\n\nUser's request:\n"


def _read_plan_notes(project) -> str:
    """Load .xli/plan-notes.md content for injection into the plan-mode preamble.
    Returns the empty-state placeholder if missing or unreadable.
    """
    notes_path = project.xli_dir / "plan-notes.md"
    if not notes_path.exists():
        return PLAN_MODE_NOTES_EMPTY
    try:
        content = notes_path.read_text(encoding="utf-8").strip()
    except OSError:
        return PLAN_MODE_NOTES_EMPTY
    return content or PLAN_MODE_NOTES_EMPTY


@dataclass
class CallStats:
    """Stats for one model's contribution to a turn.

    For the orchestrator: aggregated across all main-loop iterations.
    For workers: aggregated across every dispatched worker in the turn,
    or (when stored on a single WorkerAgent.run) the single worker's call.
    """
    model: str = ""
    iterations: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Optional[float] = None  # None when no pricing configured

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def absorb_usage(self, usage, model: str, pricing: dict) -> None:
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        c = estimate_cost(pricing, model, usage.prompt_tokens, usage.completion_tokens)
        if c is not None:
            self.cost_usd = (self.cost_usd or 0.0) + c

    def absorb_server_tool(
        self, prompt_tokens: int, completion_tokens: int, cost: float
    ) -> None:
        """Absorb server-tool sub-call usage. Cost is pre-computed by ToolContext
        because the server-tool model may differ from this CallStats's model."""
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        if cost:
            self.cost_usd = (self.cost_usd or 0.0) + cost

    def absorb(self, other: "CallStats") -> None:
        """Merge another CallStats's totals into this one (for worker aggregation)."""
        self.iterations += other.iterations
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        if other.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + other.cost_usd


@dataclass
class TurnStats:
    orch: CallStats = field(default_factory=CallStats)
    workers: CallStats = field(default_factory=CallStats)
    tool_calls: int = 0
    workers_dispatched: int = 0
    server_tool_calls: int = 0  # web_search / x_search / code_execute sub-calls
    warnings: list[str] = field(default_factory=list)

    @property
    def model(self) -> str:
        return self.orch.model

    @property
    def total_tokens(self) -> int:
        return self.orch.total_tokens + self.workers.total_tokens

    @property
    def total_cost(self) -> Optional[float]:
        if self.orch.cost_usd is None and self.workers.cost_usd is None:
            return None
        return (self.orch.cost_usd or 0.0) + (self.workers.cost_usd or 0.0)


# Past-tense action verbs that imply work was completed. If the orchestrator
# uses one of these but called zero tools, it's claiming work it did not do —
# the system prompt forbids this but models violate it. We surface a yellow
# warning under the turn line so the user knows to verify before trusting.
_CLAIM_PATTERN = re.compile(
    r"\b("
    r"verified|created|wrote|added|installed|downloaded|uploaded|"
    r"tested|ran|executed|launched|"
    r"deleted|removed|"
    r"fixed|patched|repaired|"
    r"completed|implemented|built|generated|saved|persisted"
    r")\b",
    re.IGNORECASE,
)


def _detect_unsupported_claim(text: str, stats: "TurnStats") -> Optional[str]:
    """If the orchestrator claims an action but called 0 tools, return the
    matched verb. False positives are tolerable — this is a nudge, not a wall."""
    if stats.tool_calls > 0:
        return None
    if not text:
        return None
    m = _CLAIM_PATTERN.search(text)
    return m.group(1).lower() if m else None


def _cache_headers(conversation_id: Optional[str], suffix: str = "") -> Optional[dict]:
    """Build the xAI prompt-cache header.

    `x-grok-conv-id` is xAI's convention for tagging a stable conversation so
    repeated prefixes (system prompt + tool schemas + early context) hit cache.
    The orchestrator and workers use distinct IDs (`<id>` vs `<id>:workers`)
    because they have different system prompts — sharing one ID would give us
    cache misses anyway.

    Returns None when no conversation_id is set, so the call falls through
    with no extra headers and behaves identically to the un-cached path.
    """
    if not conversation_id:
        return None
    cid = f"{conversation_id}:{suffix}" if suffix else conversation_id
    return {"x-grok-conv-id": cid}


# --------------------------------------------------------------------------- #
#  Worker
# --------------------------------------------------------------------------- #

@dataclass
class WorkerAgent:
    clients: Clients
    project: ProjectConfig
    cfg: GlobalConfig
    # Inherits the parent agent's /ref attachments so workers dispatched for
    # cross-cutting investigations search the same collection set.
    extra_collection_ids: list[str] = field(default_factory=list)
    # Inherits parent's plugin subscriptions so workers can plugin_search too.
    subscribed_plugins: list[str] = field(default_factory=list)

    def run(self, task: str, context: Optional[str] = None) -> tuple[str, CallStats]:
        call = CallStats()
        history: list[dict[str, Any]] = [
            {"role": "system", "content": WORKER_SYSTEM_PROMPT}
        ]
        user_msg = f"Task:\n{task}"
        if context:
            user_msg += f"\n\nContext supplied by parent:\n{context}"
        history.append({"role": "user", "content": user_msg})

        ctx = ToolContext(
            project=self.project,
            clients=self.clients,
            cfg=self.cfg,
            pool=None,    # workers don't get a pool — no nested dispatch
            is_worker=True,
            extra_collection_ids=list(self.extra_collection_ids),
            subscribed_plugins=list(self.subscribed_plugins),
        )
        schemas = worker_tool_schemas()
        if self.project.local_only:
            schemas = [s for s in schemas if s["function"]["name"] != "search_project"]
        if not self.subscribed_plugins:
            schemas = [
                s for s in schemas
                if s["function"]["name"] not in {"plugin_search", "plugin_get"}
            ]

        model = self.cfg.get_model_for_role("worker")
        call.model = model
        cache_hdrs = _cache_headers(self.project.conversation_id, suffix="workers")
        temperature = self.cfg.worker_temp()
        for _ in range(self.cfg.max_worker_iterations):
            call.iterations += 1
            kwargs = dict(
                model=model,
                messages=history,
                tools=schemas,
                tool_choice="auto",
                temperature=temperature,
            )
            if cache_hdrs:
                kwargs["extra_headers"] = cache_hdrs
            resp = self.clients.chat.chat.completions.create(**kwargs)
            call.absorb_usage(resp.usage, model, self.cfg.pricing)
            msg = resp.choices[0].message
            entry: dict[str, Any] = {"role": "assistant"}
            if msg.content:
                entry["content"] = msg.content
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            history.append(entry)

            if not msg.tool_calls:
                return (msg.content or "", call)

            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError as e:
                    result_text = f"invalid JSON arguments: {e}"
                else:
                    fn = WORKER_REGISTRY.get(name)
                    if fn is None:
                        result_text = f"tool not available to workers: {name}"
                    else:
                        try:
                            r = fn(ctx, args)
                            result_text = r.content
                        except Exception as e:
                            result_text = f"tool raised: {type(e).__name__}: {e}"
                history.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": result_text}
                )

            # Drain server-tool sub-call usage from this iteration's tools.
            in_t, out_t, cost, _ = ctx.drain_server_usage()
            if in_t or out_t or cost:
                call.absorb_server_tool(in_t, out_t, cost)

        return ("(worker hit max_worker_iterations without finishing)", call)


# --------------------------------------------------------------------------- #
#  Main agent
# --------------------------------------------------------------------------- #

@dataclass
class Agent:
    pool: ClientPool
    project: ProjectConfig
    cfg: GlobalConfig
    history: list[dict[str, Any]] = field(default_factory=list)
    console: Console = field(default_factory=Console)
    plan_mode: bool = False
    yolo: bool = False                 # skip per-intent confirmation gate
    # One-shot temperature override applied to the next run_turn call only.
    # Set by the /temp slash command; cleared at the start of run_turn.
    next_turn_temp_override: Optional[float] = None
    # Attached personas for cross-session memory recall. Each entry is
    # (persona_name, collection_id). Populated by the /ref slash command;
    # session-level state, not persisted across REPL restarts.
    attached_refs: list[tuple[str, str]] = field(default_factory=list)
    # Attached reference docs — markdown content inlined into the system
    # prompt every turn. Each entry is (doc_name, content). Populated by
    # /doc; session-level state. Captured separately from base_system_prompt
    # so we can re-render the effective system prompt each turn as docs are
    # attached/detached without touching the rest of history.
    attached_docs: list[tuple[str, str]] = field(default_factory=list)
    # Set in __post_init__ — the system prompt before any /doc attachments.
    # Kept separate so _effective_system_prompt can rebuild fresh each turn.
    base_system_prompt: str = ""

    def __post_init__(self) -> None:
        if not self.history:
            sys_prompt = MAIN_SYSTEM_PROMPT
            if self.project.local_only:
                addendum = [
                    "",
                    "[LOCAL MODE] This project has no remote Collection — there is "
                    "no search_project tool available. For path-based search use "
                    "glob/grep/list_dir; for content search use grep on the file directly.",
                ]
                index_path = self.project.xli_dir / "index.txt"
                if index_path.exists():
                    addendum.append(
                        f"A pre-computed file index lives at `.xli/index.txt` "
                        f"(format: `<size>\\t<relpath>` per line). Grep that file "
                        "for fast structural search instead of walking the live "
                        "filesystem — this is the right tool when the tree is large."
                    )
                sys_prompt = sys_prompt + "\n\n" + "\n".join(addendum)
            self.base_system_prompt = sys_prompt
            self.history.append({"role": "system", "content": self._effective_system_prompt()})
        else:
            # History is pre-populated (e.g. persona chat with last-N turns
            # already loaded). Capture history[0] as the base so /doc re-renders
            # work cleanly.
            if self.history and self.history[0].get("role") == "system":
                self.base_system_prompt = self.history[0]["content"]

    def _effective_system_prompt(self) -> str:
        """Base system prompt + any attached /doc content. Rebuilt each turn
        in run_turn() so /doc and /undoc take effect immediately without
        replaying history."""
        if not self.attached_docs:
            return self.base_system_prompt
        sections = [self.base_system_prompt.rstrip(), "", "---", "",
                    "# Attached reference documents",
                    "",
                    "(These were attached by the user via `/doc <name>`. Treat "
                    "them as authoritative project rules / framework conventions / "
                    "specs the user wants you to follow.)",
                    ""]
        for name, content in self.attached_docs:
            sections.append(f"## {name}")
            sections.append("")
            sections.append(content.rstrip())
            sections.append("")
        return "\n".join(sections)

    @property
    def clients(self) -> Clients:
        return self.pool.primary()

    def run_turn(self, user_message: str) -> tuple[str, set[str], TurnStats]:
        # Refresh the system prompt so /doc and /undoc take effect immediately.
        # Cheap when no docs are attached (string identity check); rebuilds
        # only when the attached set changed.
        if self.history and self.history[0].get("role") == "system":
            self.history[0] = {"role": "system", "content": self._effective_system_prompt()}

        if self.plan_mode:
            notes = _read_plan_notes(self.project)
            user_message = (
                PLAN_MODE_PREAMBLE
                + PLAN_MODE_NOTES_HEADER + notes
                + PLAN_MODE_USER_HEADER + user_message
            )
            schemas = plan_mode_schemas()
        else:
            schemas = tool_schemas() + [dispatch_subagent_schema()]
        if self.project.local_only:
            schemas = [s for s in schemas if s["function"]["name"] != "search_project"]
        # Hide plugin_search / plugin_get when no plugins are subscribed —
        # otherwise the agent has tools that always return NO_PLUGIN_MATCH.
        from xli.plugin import load_subscriptions as _load_subs
        if not _load_subs(self.project.xli_dir):
            schemas = [
                s for s in schemas
                if s["function"]["name"] not in {"plugin_search", "plugin_get"}
            ]

        self.history.append({"role": "user", "content": user_message})
        from xli.plugin import load_subscriptions
        subs = load_subscriptions(self.project.xli_dir)
        ctx = ToolContext(
            project=self.project,
            clients=self.clients,
            cfg=self.cfg,
            pool=self.pool,
            console=self.console,
            yolo=self.yolo,
            extra_collection_ids=[cid for _, cid in self.attached_refs],
            subscribed_plugins=subs,
        )
        stats = TurnStats()
        stats.orch.model = self.cfg.get_model_for_role("orchestrator")
        stats.workers.model = self.cfg.get_model_for_role("worker")
        model = stats.orch.model
        cache_hdrs = _cache_headers(self.project.conversation_id)

        # Resolve temperature for this turn: one-shot /temp override wins,
        # otherwise the configured orchestrator temperature.
        if self.next_turn_temp_override is not None:
            temperature = self.next_turn_temp_override
            self.next_turn_temp_override = None
        else:
            temperature = self.cfg.orchestrator_temp()

        for _ in range(self.cfg.max_tool_iterations):
            stats.orch.iterations += 1
            msg, usage, streamed = self._stream_orchestrator_iteration(
                model=model,
                schemas=schemas,
                temperature=temperature,
                cache_hdrs=cache_hdrs,
            )
            if usage is not None:
                stats.orch.absorb_usage(usage, model, self.cfg.pricing)

            entry: dict[str, Any] = {"role": "assistant"}
            if msg.content:
                entry["content"] = msg.content
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            self.history.append(entry)

            if not msg.tool_calls:
                # Inspect the actual content (not the empty string we'll return
                # if streamed) so the claim detector still works post-stream.
                claim = _detect_unsupported_claim(msg.content or "", stats)
                if claim is not None:
                    stats.warnings.append(
                        f'model said "{claim}" but called 0 tools — verify before trusting'
                    )
                # Content was already streamed live; return empty text so the
                # REPL doesn't print it again. The history still holds the real
                # content for the next turn's context.
                return ("" if streamed else (msg.content or ""), ctx.dirty_paths, stats)

            self._execute_tool_batch(msg.tool_calls, ctx, stats)

            # Drain server-tool sub-call usage (web_search / x_search / code_execute)
            # into orchestrator stats. Cost is pre-computed by ToolContext.
            in_t, out_t, cost, n = ctx.drain_server_usage()
            if n:
                stats.orch.absorb_server_tool(in_t, out_t, cost)
                stats.server_tool_calls += n

        return (
            "(stopped: hit max_tool_iterations — bump it in config if needed)",
            ctx.dirty_paths,
            stats,
        )

    # ------------------------------------------------------------------ #

    def _stream_orchestrator_iteration(
        self,
        *,
        model: str,
        schemas: list,
        temperature: float,
        cache_hdrs: Optional[dict],
    ) -> tuple[Any, Any, bool]:
        """One orchestrator chat-completions call, streamed.

        Streams content deltas as plain text live, snaps to Markdown at end. Tool-call deltas are
        accumulated silently — the user sees discrete tool events (with
        previews) in the next phase. Returns (msg, usage, streamed_text)
        in the same shape the non-streaming code expects:
        msg.content / msg.tool_calls[i].function.name / .arguments / .id

        `streamed_text` is True iff any content was printed live; the caller
        uses this to suppress double-printing in the REPL.
        """
        kwargs: dict[str, Any] = dict(
            model=model,
            messages=self.history,
            tools=schemas,
            tool_choice="auto",
            temperature=temperature,
            stream=True,
            stream_options={"include_usage": True},
        )
        if cache_hdrs:
            kwargs["extra_headers"] = cache_hdrs

        stream = self.clients.chat.chat.completions.create(**kwargs)

        current_content: str = ""
        # Reasoning models (grok-4.20-reasoning, etc.) emit private "thinking"
        # tokens in delta.reasoning_content separately from the user-facing
        # answer in delta.content. Capture them so we can surface them when
        # the model produces reasoning without a final content segment.
        reasoning_parts: list[str] = []
        tool_buf: dict[int, dict[str, str]] = {}
        usage: Any = None
        live: Optional[Live] = None
        streamed_any = False

        try:
            for chunk in stream:
                # Final usage chunk arrives once stream_options.include_usage is set.
                if getattr(chunk, "usage", None) is not None:
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                # Reasoning content from reasoning models. Don't render live —
                # treat it as private thinking. Surface it after the stream
                # ends *only if* no actual content arrived (diagnostic mode).
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    reasoning_parts.append(reasoning)

                if getattr(delta, "content", None):
                    if live is None:
                        # Blank line before the answer; open the Live widget
                        # lazily so iterations with only tool_calls emit
                        # nothing visible from this helper.
                        self.console.print()
                        live = Live(
                            "",
                            console=self.console,
                            refresh_per_second=10,
                            vertical_overflow="visible",
                        )
                        live.start()
                        streamed_any = True
                    # Accumulate incremental delta.content to build full text.
                    current_content += delta.content
                    # Update with plain text for smooth streaming without re-renders.
                    live.update(current_content)

                if getattr(delta, "tool_calls", None):
                    for tcd in delta.tool_calls:
                        idx = tcd.index
                        buf = tool_buf.setdefault(
                            idx, {"id": "", "name": "", "arguments": ""}
                        )
                        if getattr(tcd, "id", None):
                            buf["id"] = tcd.id
                        fn = getattr(tcd, "function", None)
                        if fn is not None:
                            if getattr(fn, "name", None):
                                buf["name"] += fn.name
                            if getattr(fn, "arguments", None):
                                buf["arguments"] += fn.arguments
        finally:
            pass  # Live will be stopped after final update

        # Snap to Markdown for final formatting if content was streamed.
        if streamed_any and current_content and live is not None:
            live.update(Markdown(current_content))
            live.stop()
        elif live is not None:
            live.stop()

        # If a reasoning model produced thinking tokens but no actual content
        # and no tool_calls, the user would see nothing — surface the
        # reasoning so the failure mode is diagnostic rather than silent.
        if reasoning_parts and not current_content and not tool_buf:
            from rich.markdown import Markdown as _Markdown
            from rich.panel import Panel
            reasoning_text = "".join(reasoning_parts).strip()
            self.console.print()
            self.console.print(
                Panel(
                    _Markdown(reasoning_text) if reasoning_text else "[dim](empty reasoning)[/dim]",
                    title="[yellow]reasoning only — no final answer was produced[/yellow]",
                    border_style="yellow",
                    padding=(0, 1),
                )
            )
            self.console.print(
                "[dim]The reasoning model thought through the question but "
                "didn't emit a final answer. Try rephrasing, or ask a "
                "follow-up to push it past the reasoning phase.[/dim]"
            )
            streamed_any = True  # suppress empty-text re-print in REPL

        content = current_content or None
        tool_calls: Optional[list[Any]] = None
        if tool_buf:
            tool_calls = []
            for idx in sorted(tool_buf.keys()):
                buf = tool_buf[idx]
                tool_calls.append(
                    SimpleNamespace(
                        id=buf["id"],
                        type="function",
                        function=SimpleNamespace(
                            name=buf["name"],
                            arguments=buf["arguments"],
                        ),
                    )
                )
        msg = SimpleNamespace(content=content, tool_calls=tool_calls)
        return msg, usage, streamed_any

    # ------------------------------------------------------------------ #

    def _execute_tool_batch(self, tool_calls, ctx: ToolContext, stats: TurnStats) -> None:
        """Run a batch of tool calls.

        Strategy: parallel-safe tools (reads, greps, searches, dispatch_subagent)
        all execute concurrently in a thread pool. Mutating tools (write, edit,
        bash) execute sequentially in batch order. Results append to history in
        the original tool_calls order so the model sees a stable transcript.
        """
        # Parse args once. Bad JSON resolves to a recorded error result.
        parsed: list[tuple[str, dict | None, str | None]] = []
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
                parsed.append((tc.function.name, args, None))
            except json.JSONDecodeError as e:
                parsed.append((tc.function.name, None, f"invalid JSON arguments: {e}"))

        # Pre-execute every parallel-safe call concurrently.
        parallel_results: dict[int, str] = {}
        parallel_errors: dict[int, bool] = {}
        parallel_indices = [
            i for i, (name, args, _err) in enumerate(parsed)
            if args is not None and name in PARALLEL_SAFE
        ]
        worker_count = sum(
            1 for i in parallel_indices if parsed[i][0] == "dispatch_subagent"
        )
        if parallel_indices:
            tag = f"{len(parallel_indices)} tool(s) in parallel"
            if worker_count:
                tag += f" ({worker_count} worker(s))"
            self.console.print(f"  [magenta]⇉[/magenta] {tag}")
            max_workers = min(self.cfg.max_parallel_workers, len(parallel_indices))
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures = {
                    ex.submit(self._run_one_safe_tool, parsed[i][0], parsed[i][1], ctx): i
                    for i in parallel_indices
                }
                for fut in as_completed(futures):
                    i = futures[fut]
                    try:
                        text, is_err, wcall = fut.result()
                    except Exception as e:
                        text, is_err, wcall = (
                            f"tool raised: {type(e).__name__}: {e}",
                            True,
                            None,
                        )
                    parallel_results[i] = text
                    parallel_errors[i] = is_err
                    if wcall is not None:
                        stats.workers.absorb(wcall)
            stats.workers_dispatched += worker_count

        # Walk tool_calls in original order, appending results.
        for i, tc in enumerate(tool_calls):
            stats.tool_calls += 1
            name, args, parse_err = parsed[i]

            if parse_err is not None:
                result_text, is_err = parse_err, True
                self.console.print(f"  [red]✗[/red] {name}: bad JSON")
            elif i in parallel_results:
                result_text = parallel_results[i]
                is_err = parallel_errors[i]
                suffix = " (worker)" if name == "dispatch_subagent" else " (parallel)"
                self._emit_tool_result(name, result_text, is_err, suffix=suffix)
            else:
                fn = REGISTRY.get(name)
                if fn is None:
                    result_text, is_err = f"unknown tool: {name}", True
                    self.console.print(f"  [red]✗[/red] {name}: unknown")
                else:
                    self._announce_tool(name, args)
                    try:
                        r = fn(ctx, args)
                        result_text, is_err = r.content, r.is_error
                        self._emit_tool_result(name, result_text, is_err)
                    except Exception as e:
                        result_text, is_err = f"tool raised: {type(e).__name__}: {e}", True
                        self.console.print(f"  [red]✗[/red] {name}: {e}")

            self.history.append(
                {"role": "tool", "tool_call_id": tc.id, "content": result_text}
            )

    def _run_one_safe_tool(
        self, name: str, args: dict, ctx: ToolContext
    ) -> tuple[str, bool, Optional[CallStats]]:
        """Execute one parallel-safe tool. Returns (content, is_error, worker_call).

        worker_call is non-None only for dispatch_subagent; the caller absorbs
        it into the main turn's worker stats.
        """
        if name == "dispatch_subagent":
            text, wcall = self._run_worker(args)
            return (text, False, wcall)
        fn = REGISTRY.get(name)
        if fn is None:
            return (f"unknown tool: {name}", True, None)
        try:
            r = fn(ctx, args)
            return (r.content, r.is_error, None)
        except Exception as e:
            return (f"tool raised: {type(e).__name__}: {e}", True, None)

    def _run_worker(self, args: dict[str, Any]) -> tuple[str, CallStats]:
        task = args.get("task", "").strip()
        if not task:
            return ("dispatch_subagent: 'task' is required", CallStats())
        context = args.get("context")
        from xli.plugin import load_subscriptions
        worker_clients = self.pool.acquire()
        worker = WorkerAgent(
            clients=worker_clients,
            project=self.project,
            cfg=self.cfg,
            extra_collection_ids=[cid for _, cid in self.attached_refs],
            subscribed_plugins=load_subscriptions(self.project.xli_dir),
        )
        text, wcall = worker.run(task, context=context)
        from xli.cost import format_cost, format_tokens
        cost_part = (
            f" · {format_cost(wcall.cost_usd)}" if wcall.cost_usd is not None else ""
        )
        header = (
            f"--- worker[{worker_clients.label}] · {wcall.model} · "
            f"{wcall.iterations} iter · {format_tokens(wcall.total_tokens)}{cost_part} ---"
        )
        return (f"{header}\n{text}", wcall)

    def _emit_tool_result(
        self, name: str, content: str, is_error: bool, *, suffix: str = ""
    ) -> None:
        """Print the result badge plus a short preview of what came back.

        The preview is purely cosmetic — the full content still flows to the
        model. The shape per tool lives in _format_tool_preview.
        """
        badge = "[red]✗[/red]" if is_error else "[green]✓[/green]"
        self.console.print(f"  {badge} {name}{suffix}")
        for line in _format_tool_preview(name, content, is_error):
            self.console.print(line)

    def _announce_tool(self, name: str, args: dict[str, Any]) -> None:
        if name in ("read_file", "write_file", "edit_file", "list_dir"):
            preview = args.get("path", "")
        elif name == "bash":
            preview = args.get("command", "")[:80]
        elif name == "grep":
            preview = f"/{args.get('pattern', '')}/"
        elif name == "glob":
            preview = args.get("pattern", "")
        elif name == "search_project":
            preview = args.get("query", "")[:80]
        elif name == "dispatch_subagent":
            preview = args.get("task", "")[:80]
        else:
            preview = ""
        self.console.print(f"  [dim]→[/dim] [cyan]{name}[/cyan] {preview}")
