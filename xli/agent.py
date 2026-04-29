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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Optional

from rich.console import Console

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


PLAN_MODE_PREAMBLE = """[PLAN MODE ACTIVE]
You cannot modify anything in this turn. Your tools are read-only: read_file, list_dir, glob, grep, search_project. No write_file, no edit_file, no bash, no dispatch_subagent.

Investigate as needed, then output a numbered, concrete plan describing exactly what changes you would make and why. The user will review and either approve, refine, or cancel before any change happens.

User's request:
"""


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
        )
        schemas = worker_tool_schemas()

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

    def __post_init__(self) -> None:
        if not self.history:
            self.history.append({"role": "system", "content": MAIN_SYSTEM_PROMPT})

    @property
    def clients(self) -> Clients:
        return self.pool.primary()

    def run_turn(self, user_message: str) -> tuple[str, set[str], TurnStats]:
        if self.plan_mode:
            user_message = PLAN_MODE_PREAMBLE + user_message
            schemas = plan_mode_schemas()
        else:
            schemas = tool_schemas() + [dispatch_subagent_schema()]

        self.history.append({"role": "user", "content": user_message})
        ctx = ToolContext(
            project=self.project,
            clients=self.clients,
            cfg=self.cfg,
            pool=self.pool,
            console=self.console,
            yolo=self.yolo,
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
            kwargs = dict(
                model=model,
                messages=self.history,
                tools=schemas,
                tool_choice="auto",
                temperature=temperature,
            )
            if cache_hdrs:
                kwargs["extra_headers"] = cache_hdrs
            resp = self.clients.chat.chat.completions.create(**kwargs)
            stats.orch.absorb_usage(resp.usage, model, self.cfg.pricing)

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
            self.history.append(entry)

            if not msg.tool_calls:
                return (msg.content or "", ctx.dirty_paths, stats)

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
                badge = "[red]✗[/red]" if is_err else "[green]✓[/green]"
                suffix = " (worker)" if name == "dispatch_subagent" else " (parallel)"
                self.console.print(f"  {badge} {name}{suffix}")
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
                        badge = "[red]✗[/red]" if is_err else "[green]✓[/green]"
                        self.console.print(f"  {badge} {name}")
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
        worker_clients = self.pool.acquire()
        worker = WorkerAgent(
            clients=worker_clients, project=self.project, cfg=self.cfg
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
