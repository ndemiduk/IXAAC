"""REPL implementations: `xli code` (project-scoped) and `xli chat` (persona-scoped).

Both REPLs share a slash-command vocabulary (see SLASH_HELP and CHAT_SLASH_HELP)
and use the attachment helpers in attachments.py + the slash dispatchers in
slash_commands.py.

Module shape:
- Constants: SLASH_HELP, CHAT_SLASH_HELP (printed by /help inside each REPL).
- Display helpers: _format_turn_line, _print_profile, _show_history_stats.
- Shell-passthrough: _run_shell_passthrough (runs `!cmd` inputs in a subshell).
- Project resolution: _resolve_project_target (cmd_code).
- Persona helpers: _chat_{list,new,edit,delete}_persona, _resolve_persona_to_load.
- The two REPLs themselves: _chat_run_session (chat) and cmd_code (project work).
- Top-level dispatchers: cmd_chat, cmd_code.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from prompt_toolkit import PromptSession, prompt
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.markup import escape as rich_escape
from rich.panel import Panel

from xli import __version__
from xli.agent import Agent
from xli.client import MissingCredentials
from xli.config import GlobalConfig, ProjectConfig
from xli.cost import format_cost, format_tokens
from xli.persona import (
    Persona,
    create_persona,
    delete_persona,
    is_valid_name,
    last_used,
    list_personas,
    open_in_editor,
)
from xli.pool import ClientPool
from xli.registry import Registry
from xli.sync import init_project, sync_project
from xli.transcript import (
    clear_turns,
    count_turns,
    load_recent_turns,
    turns_to_history,
    write_turn,
)

from .attachments import (
    archive_plan_notes,
    attachment_tag,
    load_attached_docs,
    load_attached_refs,
)
from .debug import _handle_debug_command
from .slash_commands import (
    _handle_doc_command,
    _handle_lib_command,
    _handle_ref_command,
)
from .status import print_pricing

console = Console()

# How many past turns to inline as history at chat start (used by _chat_run_session).
CHAT_RECENT_TURNS = 20


SLASH_HELP = """[bold]Code REPL slash commands[/bold]
  /help                 Show this list
  /exit, /quit          Leave the REPL
  !<shell command>      Run a shell command locally — no chat turn, no tokens
  /sync                 Sync local files to the collection now
  /reset                Clear conversation history (keep system prompt)
  /plan                 Enter plan mode (read-only investigation)
  /execute              Approve and run the plan
  /cancel               Exit plan mode
  /cost                 Show pricing config
  /yolo                 Disable bash confirmation gate
  /safe                 Re-enable bash gate
  /models               Show current models + temperatures
  /temp <0.0..2.0>      Override orchestrator temp for the next turn only
  /ref [persona]        Attach a persona's memory to search_project (no arg = list)
  /unref <persona>      Detach a previously-attached persona
  /doc [name]           Attach a reference doc into the system prompt (no arg = list)
  /undoc <name>         Detach a previously-attached doc
  /lib [...]            Plugin library: (no arg) = subscribed, all / subscribe / unsubscribe / remove
  /get <intent>         Find + invoke a subscribed plugin matching the intent
  /debug                Spawn a fresh-context verifier on uncommitted changes from the last turn
  /history              Show current in-memory history size + rough token estimate
  /status               Show project state (collection, pool, mode flags, refs, docs)
  /projects             List registered projects (current marked ●)

[dim]Admin commands (setup, keys, bootstrap, gc, models set) are CLI-only.[/dim]
[dim]Run [/dim][bold]xli help[/bold][dim] from your shell for the full CLI reference.[/dim]
"""



CHAT_SLASH_HELP = """[bold]Persona chat slash commands[/bold]
  /help                 Show this list
  /exit, /quit          Leave the REPL
  !<shell command>      Run a shell command locally — no chat turn, no tokens
  /persona <name>       Switch to another persona (loads its prompt + memory)
  /personas             List personas
  /edit                 Open current persona's prompt in $EDITOR
  /forget               Wipe current persona's transcript (with y/N confirm)
  /ref [persona]        Attach another persona's memory to this session (no arg = list)
  /unref <persona>      Detach a previously-attached persona
  /doc [name]           Attach a reference doc into the system prompt (no arg = list)
  /undoc <name>         Detach a previously-attached doc
  /lib [...]            Plugin library: (no arg) = subscribed, all / subscribe / unsubscribe / remove
  /get <intent>         Find + invoke a subscribed plugin matching the intent
  /debug                Spawn a fresh-context verifier on uncommitted changes from the last turn
  /status               Show persona state (turns on disk, attached refs/docs)
	  /history              Show current in-memory history size + rough token estimate
  /sync                 Sync turn-files to the Collection now
  /yolo / /safe         Toggle bash confirmation gate

[dim]Tip: ask the model to recall something specific — it will use search_project[/dim]
[dim]over the synced turn-files for long-term memory beyond the inline window.[/dim]
"""



def _run_shell_passthrough(user_input: str, cwd: Path) -> bool:
    """Handle `!<command>` shell passthrough at the REPL prompt.

    Runs the command locally in `cwd` with stdout/stderr inherited (so things
    like `clear`, `ls --color`, and pagers work naturally). No chat turn, no
    history change, no tokens. Returns True if the input was a `!` command and
    was handled — caller should `continue` the REPL loop. Returns False
    otherwise.
    """
    if not user_input.startswith("!"):
        return False
    cmd = user_input[1:].strip()
    if not cmd:
        console.print("[dim]usage: ![/dim][cyan]<shell command>[/cyan]   "
                      "[dim](runs locally, no chat turn)[/dim]")
        return True
    try:
        rc = subprocess.call(cmd, shell=True, cwd=str(cwd))
    except (OSError, subprocess.SubprocessError) as e:
        console.print(f"[red]shell error: {e}[/red]")
        return True
    if rc != 0:
        console.print(f"[dim]exit {rc}[/dim]")
    return True



def _show_history_stats(agent, persona=None) -> None:
    """Display current in-memory chat history size and rough token estimate.

    Uses char-count heuristic (~4 chars per token). The condensation
    keeps the system prompt + last ~HISTORY_KEEP_TURNS turns + note,
    plus aggressively scrubs older tool results even in the kept window.
    """
    from xli.transcript import count_turns as _count_turns
    from xli.agent import HISTORY_KEEP_TURNS

    n_disk = 0
    if persona is not None and hasattr(persona, "turns_dir"):
        try:
            n_disk = _count_turns(persona.turns_dir)
        except Exception:
            n_disk = 0
    n_msgs = len(getattr(agent, "history", []))
    total_chars = 0
    for m in getattr(agent, "history", []):
        content = m.get("content") or ""
        if isinstance(content, str):
            total_chars += len(content)
        tc = m.get("tool_calls") or []
        total_chars += sum(len(str(t)) for t in tc)
    est_tokens = max(0, total_chars // 4)

    console.print(
        f"history: [bold]{n_msgs} msgs[/bold]  ~[cyan]{est_tokens:,} tok[/cyan] (heuristic)"
    )
    if n_disk:
        console.print(f"  turns persisted: {n_disk}")
    console.print(f"  condensation: keeps last {HISTORY_KEEP_TURNS} turns + scrubs old tool results")
    # Peek for condensation marker
    for m in getattr(agent, "history", [])[:3]:
        c = str(m.get("content") or "")
        if "History condensed" in c or "condensed" in c.lower():
            console.print("  [dim]→ condensation note is in context[/dim]")
            break



def _format_turn_line(ts) -> str:
    """Compact, scannable turn summary."""
    parts: list[str] = [
        ts.orch.model,
        f"{ts.orch.iterations} iter",
        f"{ts.tool_calls} tools",
    ]
    orch_part = f"orch {format_tokens(ts.orch.total_tokens)}"
    if ts.orch.cost_usd is not None:
        orch_part += f" ({format_cost(ts.orch.cost_usd)})"
    parts.append(orch_part)

    if ts.workers_dispatched > 0:
        w = f"{ts.workers_dispatched} workers {format_tokens(ts.workers.total_tokens)}"
        if ts.workers.cost_usd is not None:
            w += f" ({format_cost(ts.workers.cost_usd)})"
        parts.append(w)

    if ts.total_cost is not None and ts.workers_dispatched > 0:
        parts.append(f"total {format_cost(ts.total_cost)}")

    return "[dim]" + " · ".join(parts) + "[/dim]"



def _print_profile(ts, console: Console) -> None:
    """Per-iteration breakdown for the orchestrator loop.

    Only renders when XLI_PROFILE=1 and the agent recorded iteration data
    (workers and trivial-path turns may produce empty `iters`). Goal: surface
    the prompt-growth curve so we can see which optimization knobs actually
    move the needle on long, tool-heavy turns.
    """
    iters = getattr(ts, "iters", None) or []
    if not iters:
        return

    from rich.table import Table

    table = Table(
        title="[bold]turn profile[/bold] (XLI_PROFILE=1)",
        title_justify="left",
        header_style="bold dim",
        box=None,
        padding=(0, 1),
    )
    table.add_column("iter", justify="right", style="dim")
    table.add_column("prompt", justify="right")
    table.add_column("cached", justify="right", style="green")
    table.add_column("compl", justify="right")
    table.add_column("hist_msgs", justify="right", style="dim")
    table.add_column("hist_chars", justify="right", style="dim")
    table.add_column("dur(s)", justify="right", style="dim")
    table.add_column("tools", style="cyan")

    def _compact(n: int) -> str:
        if n < 1000:
            return str(n)
        if n < 1_000_000:
            return f"{n / 1000:.1f}k"
        return f"{n / 1_000_000:.2f}M"

    cum_prompt = 0
    cum_compl = 0
    cum_cached = 0
    for it in iters:
        cum_prompt += it.prompt_tokens
        cum_compl += it.completion_tokens
        cum_cached += it.cached_tokens
        cache_pct = (
            f"{it.cached_tokens / it.prompt_tokens * 100:.0f}%"
            if it.prompt_tokens else "—"
        )
        cached_cell = (
            f"{_compact(it.cached_tokens)} ({cache_pct})"
            if it.cached_tokens else "—"
        )
        tools = ",".join(it.tool_names) if it.tool_names else "[dim](final)[/dim]"
        if len(tools) > 60:
            tools = tools[:57] + "…"
        table.add_row(
            str(it.n),
            _compact(it.prompt_tokens),
            cached_cell,
            _compact(it.completion_tokens),
            str(it.history_msgs_before),
            _compact(it.history_chars_before),
            f"{it.duration_s:.2f}",
            tools,
        )

    console.print(table)
    cache_total_pct = (
        f"{cum_cached / cum_prompt * 100:.0f}%"
        if cum_prompt else "—"
    )
    console.print(
        f"  [dim]totals: prompt {_compact(cum_prompt)} tok "
        f"(cached {_compact(cum_cached)} = {cache_total_pct}) · "
        f"compl {_compact(cum_compl)} tok · iters {len(iters)}[/dim]"
    )



def _resolve_project_target(arg: str | None) -> Path | None:
    """Resolve a `xli chat [target]` argument.

    Rules:
      - None / "" → cwd
      - looks like a path (has / or starts with . or exists as a dir) → that path
      - otherwise → registry lookup by name (exact match preferred, then substring)

    Returns the absolute project path, or None if it can't be resolved.
    """
    if not arg:
        return Path.cwd()
    p = Path(arg).expanduser()
    looks_like_path = "/" in arg or arg.startswith((".", "~")) or p.is_dir()
    if looks_like_path:
        return p.resolve() if p.is_dir() else None
    # registry lookup
    reg = Registry.load()
    exact = [e for e in reg.entries if e.name == arg]
    if exact:
        return Path(exact[0].path)
    sub = [e for e in reg.entries if arg.lower() in e.name.lower()]
    if len(sub) == 1:
        return Path(sub[0].path)
    if len(sub) > 1:
        console.print(f"[yellow]ambiguous: {arg!r} matches:[/yellow]")
        for e in sub:
            console.print(f"  · {e.name:<24} {e.path}")
        return None
    return None



def _chat_list_personas() -> int:
    personas = list_personas()
    if not personas:
        console.print(
            "[dim](no personas yet — create one with [cyan]xli chat --new <name>[/cyan])[/dim]"
        )
        return 0
    last = last_used()
    last_name = last.name if last else None
    for p in personas:
        marker = "[bold green]●[/bold green]" if p.name == last_name else " "
        try:
            first = p.first_line()
        except OSError:
            first = "(unreadable)"
        console.print(f"  {marker} [bold]{p.name}[/bold]  [dim]{first}[/dim]")
    return 0



def _chat_new_persona(name: str) -> int:
    if not is_valid_name(name):
        console.print(f"[red]invalid persona name: {name!r}[/red]")
        return 1
    p = Persona(name)
    if p.exists():
        console.print(f"[yellow]persona {name!r} already exists[/yellow] — use --edit instead")
        return 1
    create_persona(name)
    console.print(f"[green]✓[/green] created persona [bold]{name}[/bold] at {p.prompt_path}")
    console.print("[dim]opening $EDITOR — save and quit when done…[/dim]")
    open_in_editor(p.prompt_path)
    console.print(
        f"[dim]ready. Run [cyan]xli chat {name}[/cyan] to start a session.[/dim]"
    )
    return 0



def _chat_edit_persona(name: str) -> int:
    p = Persona(name)
    if not p.exists():
        console.print(f"[red]no such persona: {name!r}[/red]")
        return 1
    open_in_editor(p.prompt_path)
    console.print(f"[dim]ready. Run [cyan]xli chat {name}[/cyan] to start a session.[/dim]")
    return 0



def _chat_delete_persona(name: str, *, yes: bool) -> int:
    p = Persona(name)
    if not p.exists() and not p.project_root.exists():
        console.print(f"[red]no such persona: {name!r}[/red]")
        return 1
    if not yes:
        console.print(
            f"[yellow]about to delete persona [bold]{name}[/bold][/yellow]\n"
            f"  prompt:  {p.prompt_path}\n"
            f"  state:   {p.project_root}\n"
            f"  remote:  the persona's Collection will be left orphaned — "
            f"use [cyan]xli gc[/cyan] to clean up afterwards"
        )
        try:
            ans = input("delete? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans != "y":
            console.print("[dim]aborted[/dim]")
            return 1
    prompt_removed, state_removed = delete_persona(name)
    console.print(
        f"[green]✓[/green] deleted persona {name!r} "
        f"(prompt={'yes' if prompt_removed else 'no'}, state={'yes' if state_removed else 'no'})"
    )
    return 0



def _resolve_persona_to_load(requested: Optional[str]) -> Optional[Persona]:
    """Pick which persona to start a session with.

    - explicit name → use it (auto-create with default prompt if it doesn't exist)
    - no name + last-used exists → most-recently-used
    - no name + no last-used + no personas → bootstrap a 'default' persona
    - no name + multiple personas + no last-used → list and bail
    """
    if requested:
        if not is_valid_name(requested):
            console.print(f"[red]invalid persona name: {requested!r}[/red]")
            return None
        p = Persona(requested)
        if not p.exists():
            console.print(
                f"[dim]persona {requested!r} doesn't exist — creating with default prompt[/dim]"
            )
            create_persona(requested)
            console.print(
                f"[dim](edit it later with [cyan]xli chat --edit {requested}[/cyan])[/dim]"
            )
        return p
    last = last_used()
    if last:
        return last
    personas = list_personas()
    if not personas:
        console.print("[dim]no personas yet — bootstrapping [bold]default[/bold]…[/dim]")
        create_persona("default")
        return Persona("default")
    if len(personas) == 1:
        return personas[0]
    console.print(
        "[yellow]multiple personas — pass one explicitly:[/yellow] "
        + ", ".join(p.name for p in personas)
    )
    return None



def _chat_run_session(requested_name: Optional[str], *, yolo: bool) -> int:
    persona = _resolve_persona_to_load(requested_name)
    if persona is None:
        return 1

    cfg = GlobalConfig.load()
    try:
        pool = ClientPool.from_config(cfg)
    except MissingCredentials as e:
        console.print(f"[red]{e}[/red]")
        return 1

    # Each persona is a real XLI project (Collection-backed) — first run
    # initializes it; subsequent runs just load it.
    persona.project_root.mkdir(parents=True, exist_ok=True)
    project = ProjectConfig.load(persona.project_root)
    if project is None:
        console.print(f"[dim]initializing persona project for [bold]{persona.name}[/bold]…[/dim]")
        try:
            project = init_project(
                pool.primary(),
                persona.project_root,
                name=f"chat/{persona.name}",
            )
        except Exception as e:
            console.print(f"[red]could not init persona project: {e}[/red]")
            return 1

    # Sync any prior turn-files (catches edits made between sessions).
    if not project.local_only:
        with console.status("[cyan]syncing memory before chat…[/cyan]"):
            stats = sync_project(pool.primary(), project, cfg)
        console.print(f"[dim]sync: {stats.summary()}[/dim]")

    # Build the agent's initial history: persona's system prompt + last N turns.
    recent = load_recent_turns(persona.turns_dir, CHAT_RECENT_TURNS)
    history: list[dict] = [{"role": "system", "content": persona.system_prompt()}]
    history.extend(turns_to_history(recent))
    total_turns = count_turns(persona.turns_dir)

    agent = Agent(
        pool=pool,
        project=project,
        cfg=cfg,
        history=history,
        console=console,
        yolo=yolo,
    )
    load_attached_refs(project, agent)
    load_attached_docs(project, agent)

    history_path = project.xli_dir / "repl_history"
    session: PromptSession[str] = PromptSession(history=FileHistory(str(history_path)))

    yolo_banner = "  ·  [red]YOLO[/red]" if agent.yolo else ""
    memory_line = (
        f"memory: {total_turns} turn(s) on disk · {len(recent)} loaded inline · "
        f"older searchable via search_project"
    )
    console.print(
        Panel.fit(
            f"[bold cyan]XLI chat[/bold cyan] v{__version__}  ·  "
            f"[magenta]{persona.name}[/magenta]{yolo_banner}\n"
            f"orchestrator: {cfg.orchestrator()}  ·  worker: {cfg.worker()}\n"
            f"{memory_line}\n"
            "[dim]type [/dim][bold]/help[/bold][dim] for slash commands · "
            "[/dim][bold]/persona[/bold][dim] to switch[/dim]",
            border_style="magenta",
        )
    )

    persona.touch_used()

    while True:
        attach = attachment_tag(agent)
        prompt_prefix = f"[{persona.name}]{(' ' + attach) if attach else ''} › "
        try:
            user_input = session.prompt(f"\n{prompt_prefix}").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            return 0
        if not user_input:
            continue
        if user_input in ("/exit", "/quit"):
            return 0
        if _run_shell_passthrough(user_input, project.project_root):
            continue
        if _handle_ref_command(user_input, agent, project):
            continue
        if _handle_doc_command(user_input, agent, project):
            continue
        if _handle_lib_command(user_input, project):
            continue
        if _handle_debug_command(user_input, agent, project):
            continue
        if user_input.startswith("/get "):
            intent = user_input[len("/get "):].strip()
            if intent:
                user_input = (
                    f"Use plugin_search to find a subscribed plugin matching this "
                    f"intent, then plugin_get + bash to invoke it. If no plugin "
                    f"matches (NO_PLUGIN_MATCH), tell me — do not fabricate output. "
                    f"Intent: {intent}"
                )
            # Fall through — user_input is now the rephrased prompt, sent to model.
        elif user_input == "/get":
            console.print("[dim]usage: [/dim][cyan]/get <intent>[/cyan]"
                          "[dim] — e.g. /get the weather in seattle[/dim]")
            continue
        if user_input == "/help":
            console.print(CHAT_SLASH_HELP)
            continue
        if user_input == "/status":
            from xli.transcript import count_turns as _count_turns
            n_turns = _count_turns(persona.turns_dir)
            console.print(f"persona: [bold magenta]{persona.name}[/bold magenta]")
            console.print(f"  prompt:        {persona.prompt_path}")
            console.print(f"  state dir:     {persona.project_root}")
            console.print(f"  turns on disk: {n_turns}")
            console.print(f"  yolo:          {'[red]ON[/red]' if agent.yolo else 'off'}")
            if agent.attached_refs:
                names = ", ".join(n for n, _ in agent.attached_refs)
                console.print(f"  attached refs: [cyan]{names}[/cyan]")
            else:
                console.print(f"  attached refs: [dim](none)[/dim]")
            if agent.attached_docs:
                doc_names = ", ".join(n for n, _ in agent.attached_docs)
                total = sum(len(c) for _, c in agent.attached_docs)
                console.print(f"  attached docs: [cyan]{doc_names}[/cyan]  [dim]({total:,}b)[/dim]")
            else:
                console.print(f"  attached docs: [dim](none)[/dim]")
            continue
        if user_input == "/history":
            _show_history_stats(agent, persona)
            continue
        if user_input == "/personas":
            _chat_list_personas()
            continue
        if user_input.startswith("/persona"):
            parts = user_input.split(maxsplit=1)
            if len(parts) != 2:
                console.print(f"[dim]current persona: [bold]{persona.name}[/bold][/dim]")
                console.print("[dim]usage: /persona <name>[/dim]")
                continue
            new_name = parts[1].strip()
            if new_name == persona.name:
                console.print(f"[dim]already chatting as {new_name!r}[/dim]")
                continue
            console.print(f"[dim]switching to [bold]{new_name}[/bold]…[/dim]")
            return _chat_run_session(new_name, yolo=yolo)
        if user_input == "/edit":
            open_in_editor(persona.prompt_path)
            console.print(
                "[yellow]prompt edited[/yellow] — restart the session for the change "
                "to take effect (the agent is using the prompt loaded at start)."
            )
            continue
        if user_input == "/forget":
            n = count_turns(persona.turns_dir)
            if n == 0:
                console.print("[dim]nothing to forget — no turns recorded yet[/dim]")
                continue
            try:
                ans = input(f"  delete all {n} turns for {persona.name!r}? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            if ans != "y":
                console.print("[dim]aborted[/dim]")
                continue
            removed = clear_turns(persona.turns_dir)
            agent.history = agent.history[:1]  # keep system prompt only
            console.print(f"[green]✓[/green] removed {removed} turn(s); next sync propagates deletes")
            continue
        if user_input == "/sync":
            if project.local_only:
                console.print("[dim]/sync: local-only project — nothing to upload[/dim]")
                continue
            with console.status("[cyan]syncing…[/cyan]"):
                stats = sync_project(pool.primary(), project, cfg)
            console.print(f"[dim]sync: {stats.summary()}[/dim]")
            continue
        if user_input == "/yolo":
            agent.yolo = True
            console.print("[red]YOLO mode ON[/red] — bash gate disabled")
            continue
        if user_input == "/safe":
            agent.yolo = False
            console.print("[green]safe mode ON[/green] — bash gate enabled")
            continue

        # Real conversation turn. prompt_toolkit already echoes the input on
        # the `›` line and AI responses render in bold green for visual
        # separation, so a manual second-echo would just duplicate the user's
        # text on screen.
        try:
            text, dirty, turn_stats = agent.run_turn(user_input)
        except Exception as e:
            console.print(f"[red]turn failed: {rich_escape(str(e))}[/red]")
            continue

        # Pull the assistant's final reply out of history (works whether the
        # text streamed live or was returned non-streaming).
        final_reply = ""
        for entry in reversed(agent.history):
            if entry.get("role") == "assistant" and entry.get("content"):
                final_reply = entry["content"]
                break

        if text:
            console.print()
            console.print(text)
        console.print(_format_turn_line(turn_stats))
        _print_profile(turn_stats, console)
        for warn in turn_stats.warnings:
            console.print(f"  [yellow]⚠ {warn}[/yellow]")

        # Persist the turn — only if the assistant actually replied.
        if final_reply:
            turn_path = write_turn(persona.turns_dir, user_input, final_reply)
            try:
                rel = turn_path.relative_to(project.project_root).as_posix()
                dirty.add(rel)
            except ValueError:
                pass

        if dirty and not project.local_only:
            with console.status("[cyan]end-of-turn sync…[/cyan]"):
                stats = sync_project(pool.primary(), project, cfg)
            console.print(f"[dim]sync: {stats.summary()}[/dim]")



def cmd_chat(args: argparse.Namespace) -> int:
    """Persona-based conversational agent with persistent memory.

    Sub-routes for maintenance flags (--list / --new / --edit / --delete);
    otherwise launches a chat REPL backed by the named persona.
    """
    if args.list:
        return _chat_list_personas()
    if args.new:
        return _chat_new_persona(args.new)
    if args.edit:
        return _chat_edit_persona(args.edit)
    if args.delete:
        return _chat_delete_persona(args.delete, yes=args.yes)
    return _chat_run_session(args.name, yolo=args.yolo)



def cmd_code(args: argparse.Namespace) -> int:
    """Project-scoped code agent (was `xli chat` before the rename to clarify
    its purpose). Pass a project name or path; defaults to cwd."""
    cfg = GlobalConfig.load()
    try:
        pool = ClientPool.from_config(cfg)
    except MissingCredentials as e:
        console.print(f"[red]{e}[/red]")
        return 1
    target = _resolve_project_target(getattr(args, "target", None))
    if target is None:
        console.print(
            "[red]could not resolve target — run `xli projects` to see what's registered[/red]"
        )
        return 1
    project = ProjectConfig.load(target.resolve())
    if not project:
        console.print(
            f"[red]not an xli project: {target}[/red]\n"
            "[dim]run `xli init` in that directory first[/dim]"
        )
        return 1

    # Sync once on start so the agent has a current view (skipped in local mode).
    if not project.local_only:
        with console.status("[cyan]syncing project before chat…[/cyan]"):
            stats = sync_project(pool.primary(), project, cfg)
        console.print(f"[dim]sync: {stats.summary()}[/dim]")

    agent = Agent(pool=pool, project=project, cfg=cfg, console=console, yolo=args.yolo)
    load_attached_refs(project, agent)
    load_attached_docs(project, agent)

    history_path = project.xli_dir / "repl_history"
    session: PromptSession[str] = PromptSession(history=FileHistory(str(history_path)))

    yolo_banner = "  ·  [red]YOLO[/red]" if agent.yolo else ""
    local_banner = "  ·  [magenta]LOCAL[/magenta]" if project.local_only else ""
    coll_line = (
        f"collection: {project.collection_id}  ·  pool: {len(pool)} key(s)"
        if not project.local_only
        else f"local-only · pool: {len(pool)} key(s)"
    )
    console.print(
        Panel.fit(
            f"[bold cyan]XLI[/bold cyan] v{__version__}  ·  {project.name}{yolo_banner}{local_banner}\n"
            f"orchestrator: {cfg.orchestrator()}  ·  worker: {cfg.worker()}\n"
            f"{coll_line}\n"
            "[dim]type [/dim][bold]/help[/bold][dim] for slash commands[/dim]",
            border_style="cyan",
        )
    )

    while True:
        prefix_tag = "[plan]" if agent.plan_mode else ("[yolo]" if agent.yolo else "")
        attach = attachment_tag(agent)
        combined = " ".join(p for p in [prefix_tag, attach] if p)
        prompt_prefix = f"{combined} › " if combined else "› "
        try:
            user_input = session.prompt(f"\n{prompt_prefix}").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            return 0
        if not user_input:
            continue
        if user_input in ("/exit", "/quit"):
            return 0
        if _run_shell_passthrough(user_input, project.project_root):
            continue
        if _handle_ref_command(user_input, agent, project):
            continue
        if _handle_doc_command(user_input, agent, project):
            continue
        if _handle_lib_command(user_input, project):
            continue
        if _handle_debug_command(user_input, agent, project):
            continue
        if user_input.startswith("/get "):
            intent = user_input[len("/get "):].strip()
            if intent:
                user_input = (
                    f"Use plugin_search to find a subscribed plugin matching this "
                    f"intent, then plugin_get + bash to invoke it. If no plugin "
                    f"matches (NO_PLUGIN_MATCH), tell me — do not fabricate output. "
                    f"Intent: {intent}"
                )
            # Fall through — user_input is now the rephrased prompt, sent to model.
        elif user_input == "/get":
            console.print("[dim]usage: [/dim][cyan]/get <intent>[/cyan]"
                          "[dim] — e.g. /get the weather in seattle[/dim]")
            continue
        if user_input == "/help":
            console.print(SLASH_HELP)
            continue
        if user_input == "/history":
            _show_history_stats(agent)
            continue
        if user_input == "/models":
            override = (
                f"  [yellow](next turn override: {agent.next_turn_temp_override})[/yellow]"
                if agent.next_turn_temp_override is not None else ""
            )
            console.print(f"orchestrator: [cyan]{cfg.orchestrator()}[/cyan]  temp=[cyan]{cfg.orchestrator_temp()}[/cyan]{override}")
            console.print(f"worker:       [cyan]{cfg.worker()}[/cyan]  temp=[cyan]{cfg.worker_temp()}[/cyan]")
            console.print(
                "[dim]change persistently with `xli models set` (model) or edit "
                "`orchestrator_temperature`/`worker_temperature` in config.json[/dim]"
            )
            continue
        if user_input.startswith("/temp"):
            parts = user_input.split(maxsplit=1)
            if len(parts) != 2:
                console.print(
                    f"[dim]usage: /temp <value 0.0..2.0>  "
                    f"(current orch={cfg.orchestrator_temp()}, "
                    f"override={agent.next_turn_temp_override})[/dim]"
                )
                continue
            try:
                t = float(parts[1])
            except ValueError:
                console.print(f"[red]invalid temperature: {parts[1]!r}[/red]")
                continue
            if not 0.0 <= t <= 2.0:
                console.print(f"[red]temperature must be 0.0..2.0[/red]")
                continue
            agent.next_turn_temp_override = t
            console.print(
                f"[yellow]temperature override = {t}[/yellow] "
                "[dim](applies to next turn only, then reverts to config)[/dim]"
            )
            continue
        if user_input == "/status":
            conv = (project.conversation_id or "")[:12]
            mode = "[magenta]local-only[/magenta]" if project.local_only else "full (synced)"
            console.print(f"project: [bold]{project.name}[/bold]")
            console.print(f"  root:          {project.project_root}")
            console.print(f"  mode:          {mode}")
            if project.local_only:
                idx = project.xli_dir / "index.txt"
                if idx.exists():
                    n = sum(1 for _ in idx.open())
                    console.print(f"  index:         .xli/index.txt — {n} files cached")
            else:
                console.print(f"  collection_id: {project.collection_id}")
            console.print(f"  conv_id:       {conv}…")
            console.print(f"  pool:          {len(pool)} key(s)")
            console.print(f"  plan mode:     {'[yellow]ON[/yellow]' if agent.plan_mode else 'off'}")
            console.print(f"  yolo:          {'[red]ON[/red]' if agent.yolo else 'off'}")
            if agent.attached_refs:
                names = ", ".join(n for n, _ in agent.attached_refs)
                console.print(f"  attached refs: [cyan]{names}[/cyan]")
            else:
                console.print(f"  attached refs: [dim](none)[/dim]")
            if agent.attached_docs:
                doc_names = ", ".join(n for n, _ in agent.attached_docs)
                total = sum(len(c) for _, c in agent.attached_docs)
                console.print(f"  attached docs: [cyan]{doc_names}[/cyan]  [dim]({total:,}b)[/dim]")
            else:
                console.print(f"  attached docs: [dim](none)[/dim]")
            continue
        if user_input == "/projects":
            reg = Registry.load()
            if not reg.entries:
                console.print("[dim](no registered projects)[/dim]")
            else:
                here = project.project_root.resolve()
                for e in reg.entries:
                    is_current = Path(e.path).resolve() == here
                    marker = "[bold green]●[/bold green]" if is_current else " "
                    console.print(f"  {marker} [bold]{e.name}[/bold]  [dim]{e.path}[/dim]")
            continue
        if user_input == "/reset":
            agent.history = agent.history[:1]  # keep system prompt
            agent.plan_mode = False
            console.print("[dim]history cleared[/dim]")
            continue
        if user_input == "/sync":
            if project.local_only:
                console.print("[dim]/sync: local-only project — nothing to upload[/dim]")
                continue
            with console.status("[cyan]syncing…[/cyan]"):
                stats = sync_project(pool.primary(), project, cfg)
            console.print(f"[dim]sync: {stats.summary()}[/dim]")
            continue
        if user_input == "/plan":
            agent.plan_mode = True
            notes_path = project.xli_dir / "plan-notes.md"
            if notes_path.exists() and notes_path.stat().st_size > 0:
                line_count = sum(1 for _ in notes_path.open(encoding="utf-8"))
                age_seconds = int(time.time() - notes_path.stat().st_mtime)
                age_str = (
                    f"{age_seconds // 3600}h {age_seconds % 3600 // 60}m"
                    if age_seconds >= 3600 else f"{age_seconds // 60}m"
                )
                console.print(
                    f"[dim]found existing plan-notes.md ({line_count} lines, "
                    f"last modified {age_str} ago)[/dim]"
                )
                try:
                    ans = input("  resume from prior notes? [Y/n] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    ans = "y"
                if ans == "n":
                    archived = archive_plan_notes(project, label="abandoned")
                    if archived:
                        console.print(f"[dim]archived to {archived.relative_to(project.project_root)}[/dim]")
            console.print(
                "[yellow]plan mode ON[/yellow] — next turn will investigate read-only and produce a plan. "
                "/execute to approve, /cancel to drop."
            )
            continue
        if user_input == "/cancel":
            if agent.plan_mode:
                agent.plan_mode = False
                archived = archive_plan_notes(project, label="cancelled")
                if archived:
                    console.print(
                        f"[dim]plan mode off — notes archived to "
                        f"{archived.relative_to(project.project_root)}[/dim]"
                    )
                else:
                    console.print("[dim]plan mode off[/dim]")
            else:
                console.print("[dim](not in plan mode)[/dim]")
            continue
        if user_input == "/cost":
            print_pricing(cfg)
            continue
        if user_input == "/yolo":
            agent.yolo = True
            console.print(
                "[red]YOLO mode ON[/red] — bash confirmation gate is OFF. "
                "modifies-system and network commands will run without prompting. "
                "/safe to turn back on."
            )
            continue
        if user_input == "/safe":
            agent.yolo = False
            console.print("[green]safe mode ON[/green] — bash gate active for "
                          "modifies-system and network intents.")
            continue
        if user_input == "/execute":
            if not agent.plan_mode:
                console.print("[dim](not in plan mode — nothing to execute)[/dim]")
                continue
            agent.plan_mode = False
            archived = archive_plan_notes(project, label="approved")
            user_input = "Approved. Execute the plan above using all available tools."
            if archived:
                # Feed the full investigation notes into the execute-phase
                # context so the agent has the rationale, not just the bullet
                # points from chat history. Truncate if oversized to bound
                # per-iteration context bloat.
                try:
                    notes = archived.read_text(encoding="utf-8")
                except OSError:
                    notes = ""
                if notes:
                    MAX_NOTES = 30_000
                    if len(notes) > MAX_NOTES:
                        notes = notes[-MAX_NOTES:]
                        notes = "[…truncated to last 30KB…]\n" + notes
                    user_input += (
                        f"\n\nYour plan-mode investigation notes "
                        f"(archived to {archived.relative_to(project.project_root)}):\n\n"
                        + notes
                    )
                console.print(
                    f"[green]plan approved — executing[/green] "
                    f"[dim](notes archived to {archived.relative_to(project.project_root)})[/dim]"
                )
            else:
                console.print("[green]plan approved — executing[/green]")

        try:
            text, dirty, turn_stats = agent.run_turn(user_input)
        except Exception as e:
            console.print(f"[red]turn failed: {rich_escape(str(e))}[/red]")
            continue

        if text:
            console.print()
            console.print(text)
        console.print(_format_turn_line(turn_stats))
        _print_profile(turn_stats, console)
        for warn in turn_stats.warnings:
            console.print(f"  [yellow]⚠ {warn}[/yellow]")

        if dirty and not project.local_only:
            with console.status("[cyan]end-of-turn sync…[/cyan]"):
                stats = sync_project(pool.primary(), project, cfg)
            console.print(f"[dim]sync: {stats.summary()}[/dim]")
