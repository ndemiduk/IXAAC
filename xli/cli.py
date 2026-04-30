"""XLI CLI: subcommands + interactive REPL."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.panel import Panel

from xli import __version__
from xli.agent import Agent
from xli.bootstrap import (
    DEFAULT_EXPIRE_DAYS,
    INTER_CREATE_DELAY_SEC,
    BootstrapError,
    append_keys_to_config,
    create_api_key,
    delete_api_key,
    discover_models,
    discover_team_id,
    extract_api_key_string,
    list_api_keys,
    pick_best_models,
    remove_keys_from_config,
    rotate_api_key,
    set_models_in_config,
    set_team_id_in_config,
    update_api_key_expiration,
    update_key_in_config,
)
from xli.client import Clients, MissingCredentials
from xli.config import GLOBAL_CONFIG_DIR, GLOBAL_CONFIG_FILE, GlobalConfig, ProjectConfig
from xli.cost import format_cost, format_tokens
from xli.persona import (
    DEFAULT_PROMPT,
    Persona,
    create_persona,
    delete_persona,
    is_valid_name,
    last_used,
    list_personas,
    open_in_editor,
)
from xli.pool import ClientPool
from xli.registry import REGISTRY_FILE, Registry
from xli.sync import init_project, sync_project
from xli.transcript import (
    clear_turns,
    count_turns,
    load_recent_turns,
    turns_to_history,
    write_turn,
)

CHAT_RECENT_TURNS = 20  # how many past turns to inline as history at chat start


def _handle_ref_command(user_input: str, agent) -> bool:
    """Handle `/ref` and `/unref` slash commands.

    `/ref`            — list currently-attached personas (in-session state)
    `/ref <name>`     — attach <name>'s collection to search_project
    `/unref <name>`   — detach

    Returns True if the input was handled, False otherwise (caller falls
    through to the next slash dispatcher).
    """
    if not (user_input == "/ref" or user_input.startswith("/ref ") or
            user_input == "/unref" or user_input.startswith("/unref ")):
        return False

    parts = user_input.split(maxsplit=1)
    cmd = parts[0]

    if cmd == "/ref":
        if len(parts) == 1:
            # List current attachments
            if not agent.attached_refs:
                console.print("[dim](no refs attached this session)[/dim]")
                console.print("[dim]usage: [/dim][cyan]/ref <persona>[/cyan]"
                              "[dim] to attach a persona's memory[/dim]")
            else:
                console.print("[bold]attached refs:[/bold]")
                for name, cid in agent.attached_refs:
                    short = cid[:24] + "…" if len(cid) > 24 else cid
                    console.print(f"  · [cyan]{name}[/cyan]  [dim]{short}[/dim]")
            return True

        name = parts[1].strip()
        if not is_valid_name(name):
            console.print(f"[red]invalid persona name: {name!r}[/red]")
            return True
        persona = Persona(name)
        if not persona.exists():
            console.print(f"[red]no such persona: {name!r}[/red]  "
                          f"[dim](create with [/dim][cyan]xli chat --new {name}[/cyan][dim])[/dim]")
            return True
        cid = persona.collection_id()
        if not cid:
            console.print(
                f"[yellow]persona {name!r} has no Collection yet[/yellow] — "
                f"run [cyan]xli chat {name}[/cyan] once to initialize it, then try again."
            )
            return True
        if any(n == name for n, _ in agent.attached_refs):
            console.print(f"[dim](already attached: {name})[/dim]")
            return True
        agent.attached_refs.append((name, cid))
        console.print(
            f"[green]✓[/green] attached [cyan]{name}[/cyan]'s memory — "
            "[dim]search_project will now include their conversation history[/dim]"
        )
        return True

    if cmd == "/unref":
        if len(parts) != 2:
            console.print("[dim]usage: [/dim][cyan]/unref <persona>[/cyan]")
            return True
        name = parts[1].strip()
        before = len(agent.attached_refs)
        agent.attached_refs = [(n, c) for n, c in agent.attached_refs if n != name]
        if len(agent.attached_refs) == before:
            console.print(f"[dim]no ref attached named {name!r}[/dim]")
        else:
            console.print(f"[green]✓[/green] detached [cyan]{name}[/cyan]")
        return True

    return False


def _handle_doc_command(user_input: str, agent) -> bool:
    """Handle `/doc` and `/undoc` slash commands.

    `/doc`            — list currently-attached docs (in-session state)
    `/doc <name>`     — attach <name>'s content to the system prompt
    `/undoc <name>`   — detach

    Mirrors `_handle_ref_command` shape but operates on agent.attached_docs
    (which feeds the system prompt) instead of attached_refs (which feeds
    search_project's collection list).
    """
    from xli.doc import Doc, INLINE_SOFT_CAP_BYTES, is_valid_name as _is_valid_doc_name

    if not (user_input == "/doc" or user_input.startswith("/doc ") or
            user_input == "/undoc" or user_input.startswith("/undoc ")):
        return False

    parts = user_input.split(maxsplit=1)
    cmd = parts[0]

    if cmd == "/doc":
        if len(parts) == 1:
            if not agent.attached_docs:
                console.print("[dim](no docs attached this session)[/dim]")
                console.print("[dim]usage: [/dim][cyan]/doc <name>[/cyan]"
                              "[dim] to attach a reference doc[/dim]")
            else:
                console.print("[bold]attached docs:[/bold]")
                for name, content in agent.attached_docs:
                    size = len(content)
                    console.print(f"  · [cyan]{name}[/cyan]  [dim]{size:,} bytes[/dim]")
            return True

        name = parts[1].strip()
        if not _is_valid_doc_name(name):
            console.print(f"[red]invalid doc name: {name!r}[/red]")
            return True
        d = Doc(name)
        if not d.exists():
            console.print(
                f"[red]no such doc: {name!r}[/red]  "
                f"[dim](create with [/dim][cyan]xli doc --new {name}[/cyan][dim])[/dim]"
            )
            return True
        if any(n == name for n, _ in agent.attached_docs):
            console.print(f"[dim](already attached: {name})[/dim]")
            return True
        try:
            content = d.read()
        except OSError as e:
            console.print(f"[red]read failed: {e}[/red]")
            return True
        size = len(content)
        agent.attached_docs.append((name, content))
        warn = ""
        if size > INLINE_SOFT_CAP_BYTES:
            warn = (
                f"  [yellow]⚠ {size:,} bytes is large for inline mode — "
                "consider a persona + /ref for very long reference material[/yellow]"
            )
        console.print(
            f"[green]✓[/green] attached doc [cyan]{name}[/cyan] "
            f"[dim]({size:,} bytes inlined into system prompt)[/dim]"
            + (f"\n{warn}" if warn else "")
        )
        return True

    if cmd == "/undoc":
        if len(parts) != 2:
            console.print("[dim]usage: [/dim][cyan]/undoc <name>[/cyan]")
            return True
        name = parts[1].strip()
        before = len(agent.attached_docs)
        agent.attached_docs = [(n, c) for n, c in agent.attached_docs if n != name]
        if len(agent.attached_docs) == before:
            console.print(f"[dim]no doc attached named {name!r}[/dim]")
        else:
            console.print(f"[green]✓[/green] detached [cyan]{name}[/cyan]")
        return True

    return False


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

console = Console()


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
  /status               Show project state (collection, pool, mode flags, refs, docs)
  /projects             List registered projects (current marked ●)

[dim]Admin commands (setup, keys, bootstrap, gc, models set) are CLI-only.[/dim]
[dim]Run [/dim][bold]xli help[/bold][dim] from your shell for the full CLI reference.[/dim]
"""


HELP_TEXT = """xli — Grok + xAI Collections coding agent.

PROJECT (code work — files, edits, builds)
  init [NAME]              Initialize an xli project in the current directory.
                           --local: no Collection upload (Midnight Commander mode).
                           --snapshot: cache .xli/index.txt for fast structural search.
  new NAME                 Create a new project directory and initialize it.
  scratch [NAME]           Ephemeral local-only project under ~/.xli/scratch/, then chat.
  projects [FILTER]        List all registered xli projects.
  status [PATH]            Show config + project state.
  sync [PATH]              Push local changes to the project's collection.
  code [TARGET]            Start the project-scoped code REPL (was `chat` pre-rename).

CHAT (conversation with persistent memory)
  chat [NAME]              Start a chat as persona NAME (default: most-recently-used).
  chat --new NAME          Create a new persona; opens $EDITOR on its prompt.
  chat --list              List personas.
  chat --edit NAME         Edit a persona's prompt in $EDITOR.
  chat --delete NAME       Delete a persona (prompt + state dir).

KNOWLEDGE (attached in any REPL via /doc <name>)
  doc --new NAME           Create a new reference doc; opens $EDITOR.
  doc --list               List all docs.
  doc --edit NAME          Edit a doc in $EDITOR.
  doc --delete NAME        Delete a doc.

SETUP
  config                   Write a config template to ~/.config/xli/config.json.
  setup                    First-time setup: config + provision primary + worker keys.
  bootstrap                Lower-level: provision worker keys via management API.

MODELS
  models list              List models the team has access to.
  models recommended       Show heuristic best-of-class picks.
  models set [--orchestrator MODEL] [--worker MODEL]

KEYS
  keys list                List local chat keys with server-side expiration.
  keys rotate [--label X]  Rotate the secret of one or all keys.
  keys expire --days N     Update expireTime on existing key(s).
  keys revoke [--prefix X] Delete keys by label prefix (server + local).

MAINTENANCE
  gc [--dry-run]           Find and delete orphan xAI collections.
  help                     Show this message.

For per-command flags:  xli <cmd> --help
For version:            xli --version
"""


def cmd_help(args: argparse.Namespace) -> int:
    print(HELP_TEXT)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    cfg = GlobalConfig.load()
    project_root = Path(args.path or ".").resolve()
    if not project_root.is_dir():
        console.print(f"[red]not a directory: {project_root}[/red]")
        return 1
    name = args.name or project_root.name
    existing = ProjectConfig.load(project_root)
    if existing and not args.force:
        console.print(
            f"[yellow]project already initialized[/yellow] "
            f"(name={existing.name}, collection={existing.collection_id or 'local-only'})"
        )
        return 0

    # Local mode skips Collection provisioning, so no clients are needed for init.
    clients = None
    if not args.local:
        try:
            clients = Clients.from_config(cfg)
        except MissingCredentials as e:
            console.print(f"[red]{e}[/red]")
            return 1

    project = init_project(
        clients,
        project_root,
        name=name,
        existing_collection_id=args.collection_id,
        local_only=args.local,
    )

    if args.local:
        console.print(
            f"[green]✓[/green] initialized [bold]{project.name}[/bold] "
            "[dim](local mode — no Collection)[/dim]"
        )
    else:
        console.print(
            f"[green]✓[/green] initialized [bold]{project.name}[/bold] → "
            f"collection {project.collection_id}"
        )

    if args.snapshot:
        from xli.sync import write_file_index
        with console.status("[cyan]indexing files… 0[/cyan]") as status:
            def progress(n: int, last: str) -> None:
                # Truncate last path so the status line doesn't wrap weirdly.
                short = last if len(last) <= 60 else "…" + last[-59:]
                status.update(f"[cyan]indexing files… {n}[/cyan]  [dim]{short}[/dim]")
            count = write_file_index(project, cfg, on_progress=progress)
        console.print(
            f"  [dim]index: .xli/index.txt — {count} files cached[/dim]"
        )

    pool_size = len(cfg.key_pairs())
    if not args.local and pool_size <= 1 and cfg.management_api_key:
        console.print(
            f"\n[yellow]tip:[/yellow] you have only {pool_size} chat key in the pool. "
            "Run [cyan]xli bootstrap[/cyan] to auto-provision worker keys for parallel "
            "swarm investigation."
        )
    if args.sync and not args.local:
        return cmd_sync(argparse.Namespace(path=str(project_root), dry_run=False))
    return 0


def cmd_scratch(args: argparse.Namespace) -> int:
    """Create an ephemeral local-only project under ~/.xli/scratch/<name>/ and drop into chat.

    Use for: one-off file-management tasks ("rename these", "find duplicates"),
    quick experiments, anything you don't want to upload as a project.
    For snapshotting an existing big directory (NAS, media collection), run
    `xli init --local --snapshot` *in that directory* instead — scratch creates
    a fresh empty dir.
    """
    from datetime import datetime
    name = args.name or datetime.now().strftime("%Y%m%d-%H%M%S")
    scratch_root = (Path.home() / ".xli" / "scratch" / name).resolve()
    scratch_root.mkdir(parents=True, exist_ok=True)

    existing = ProjectConfig.load(scratch_root)
    if existing and not args.force:
        console.print(f"[yellow]scratch project already exists at[/yellow] {scratch_root}")
    else:
        project = init_project(
            None,
            scratch_root,
            name=f"scratch/{name}",
            local_only=True,
            snapshot=False,  # empty dir; nothing to snapshot
        )
        console.print(
            f"[green]✓[/green] scratch project [bold]{project.name}[/bold] at {scratch_root}"
        )

    if args.no_chat:
        return 0
    return cmd_code(argparse.Namespace(target=str(scratch_root), yolo=args.yolo))


def cmd_sync(args: argparse.Namespace) -> int:
    cfg = GlobalConfig.load()
    try:
        clients = Clients.from_config(cfg)
    except MissingCredentials as e:
        console.print(f"[red]{e}[/red]")
        return 1
    project = ProjectConfig.load(Path(args.path).resolve())
    if not project:
        console.print("[red]not an xli project — run `xli init` first[/red]")
        return 1
    with console.status("[cyan]syncing project…[/cyan]"):
        stats = sync_project(clients, project, cfg, dry_run=args.dry_run)
    color = "yellow" if args.dry_run else "green"
    label = "would sync" if args.dry_run else "synced"
    console.print(f"[{color}]{label}:[/{color}] {stats.summary()}")
    if stats.errors:
        for e in stats.errors[:5]:
            console.print(f"  [red]· {e}[/red]")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = GlobalConfig.load()
    project = ProjectConfig.load(Path(args.path).resolve())
    pairs = cfg.key_pairs()
    registry = Registry.load()
    console.print(f"[bold]xli[/bold] v{__version__}")
    console.print(f"  config file:        {GLOBAL_CONFIG_FILE}")
    console.print(f"  registry:           {REGISTRY_FILE} ({len(registry.entries)} project(s))")
    if cfg.management_api_key:
        console.print(f"  mgmt key:           [green]✓[/green] from env XAI_MANAGEMENT_API_KEY")
    else:
        console.print(f"  mgmt key:           [red]✗ unset[/red] — export XAI_MANAGEMENT_API_KEY")
    if GlobalConfig.mgmt_key_in_file():
        console.print(
            "  [yellow]⚠ legacy management_api_key found in config.json — remove it[/yellow]"
        )
    auto_tag = ""
    if cfg.models_detected_at:
        auto_tag = f"  [dim](auto-detected {cfg.models_detected_at[:10]})[/dim]"
    console.print(f"  orchestrator model: [cyan]{cfg.get_model_for_role('orchestrator')}[/cyan]{auto_tag}")
    console.print(f"  worker model:       [cyan]{cfg.get_model_for_role('worker')}[/cyan]")
    console.print(f"  orchestrator temp:  [cyan]{cfg.orchestrator_temp()}[/cyan]")
    console.print(f"  worker temp:        [cyan]{cfg.worker_temp()}[/cyan]")
    if cfg.pricing:
        priced = sum(
            1
            for m in (cfg.get_model_for_role("orchestrator"), cfg.get_model_for_role("worker"))
            if m in cfg.pricing
        )
        console.print(
            f"  cost tracking:      [green]enabled[/green] "
            f"({len(cfg.pricing)} models priced; {priced}/2 active models covered)"
        )
    else:
        console.print(
            "  cost tracking:      [yellow]disabled[/yellow] "
            "(add `pricing` map to config to enable)"
        )
    if pairs:
        for p in pairs:
            mgmt = "[green]✓[/green]" if p.management_api_key else "[red]missing[/red]"
            console.print(f"    · {p.label:<12} api=set  mgmt={mgmt}")
        console.print(f"  pool size:     {len(pairs)} key(s)")
    else:
        console.print("  [red]no keys configured[/red] — run `xli config` to write a template")
    if project:
        console.print(f"\n[bold]project:[/bold] {project.name}")
        console.print(f"  root:          {project.project_root}")
        console.print(f"  collection_id: {project.collection_id}")
        console.print(f"  manifest:      {project.manifest_path}")
        if project.conversation_id:
            console.print(
                f"  conv_id:       {project.conversation_id[:12]}…  "
                f"[dim](xAI prompt-cache key)[/dim]"
            )
    else:
        console.print("\n[yellow]no xli project in this directory[/yellow]")
    return 0


def _print_pricing(cfg: GlobalConfig) -> None:
    """Render the configured pricing table + coverage of active models."""
    orch = cfg.get_model_for_role("orchestrator")
    worker = cfg.get_model_for_role("worker")
    if not cfg.pricing:
        console.print(
            "[yellow]no pricing configured[/yellow] — add a `pricing` map to your "
            "config.json to enable cost estimates."
        )
        return
    console.print("[bold]pricing[/bold] (USD per million tokens)")
    for model, rates in cfg.pricing.items():
        in_r = rates.get("input_per_million", 0)
        out_r = rates.get("output_per_million", 0)
        marks = []
        if model == orch:
            marks.append("[cyan]orch[/cyan]")
        if model == worker:
            marks.append("[cyan]worker[/cyan]")
        tag = "  ←  " + " + ".join(marks) if marks else ""
        console.print(f"  · {model:<32}  in ${in_r:>6.2f}  out ${out_r:>6.2f}{tag}")
    if orch not in cfg.pricing:
        console.print(f"  [yellow]· orchestrator model {orch!r} has no pricing[/yellow]")
    if worker != orch and worker not in cfg.pricing:
        console.print(f"  [yellow]· worker model {worker!r} has no pricing[/yellow]")


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


def cmd_gc(args: argparse.Namespace) -> int:
    """List + (optionally) delete orphan xAI collections.

    Categories:
      - tracked-alive   : in registry, project on disk, in cloud  -> keep
      - tracked-dead    : in registry, project DELETED on disk    -> orphan
      - untracked-cloud : in cloud (xli/* prefix), not in registry -> orphan
      - tracked-missing : in registry, NOT in cloud (already gone) -> registry stale
    """
    cfg = GlobalConfig.load()
    try:
        clients = Clients.from_config(cfg)
    except MissingCredentials as e:
        console.print(f"[red]{e}[/red]")
        return 1

    registry = Registry.load()
    by_id = {e.collection_id: e for e in registry.entries}

    with console.status("[cyan]listing collections…[/cyan]"):
        cloud_ids: dict[str, str] = {}  # id -> name
        pagination_token = None
        while True:
            resp = clients.xai.collections.list(limit=500, pagination_token=pagination_token)
            for c in resp.collections:
                cloud_ids[c.collection_id] = c.collection_name
            pagination_token = resp.pagination_token or None
            if not pagination_token or not resp.collections:
                break

    tracked_alive: list[tuple[str, str, str]] = []   # (path, name, id)
    tracked_dead: list[tuple[str, str, str]] = []
    untracked_cloud: list[tuple[str, str]] = []      # (id, name)
    tracked_missing: list[tuple[str, str, str]] = []

    for cid, entry in by_id.items():
        if cid not in cloud_ids:
            tracked_missing.append((entry.path, entry.name, cid))
            continue
        path_alive = (
            Path(entry.path).is_dir()
            and (Path(entry.path) / ".xli" / "project.json").exists()
        )
        bucket = tracked_alive if path_alive else tracked_dead
        bucket.append((entry.path, entry.name, cid))

    for cid, name in cloud_ids.items():
        if cid in by_id:
            continue
        if not name.startswith("xli/"):
            continue  # only consider XLI-prefixed collections
        untracked_cloud.append((cid, name))

    def _print_section(title: str, items: list, fmt) -> None:
        if not items:
            return
        console.print(f"\n[bold]{title}[/bold] ({len(items)})")
        for item in items:
            console.print(f"  · {fmt(item)}")

    _print_section(
        "[green]tracked & alive[/green]",
        tracked_alive,
        lambda x: f"{x[1]:<30} {x[2]}  →  {x[0]}",
    )
    _print_section(
        "[yellow]tracked but path deleted[/yellow]",
        tracked_dead,
        lambda x: f"{x[1]:<30} {x[2]}  ✗  {x[0]}",
    )
    _print_section(
        "[yellow]untracked cloud collection[/yellow]",
        untracked_cloud,
        lambda x: f"{x[1]:<30} {x[0]}",
    )
    _print_section(
        "[dim]registry stale (cloud already gone)[/dim]",
        tracked_missing,
        lambda x: f"{x[1]:<30} {x[2]}  →  {x[0]}",
    )

    deletable = len(tracked_dead) + len(untracked_cloud)
    if not deletable and not tracked_missing:
        console.print("\n[green]nothing to clean up[/green]")
        return 0

    if args.dry_run:
        console.print(f"\n[yellow]dry-run:[/yellow] would delete {deletable} collection(s)")
        return 0

    if deletable:
        if args.yes:
            choice = "a"
        else:
            console.print(
                f"\nDelete: [a]ll {deletable} orphans, [d]ead-path-only "
                f"({len(tracked_dead)}), [n]one ?"
            )
            choice = input("> ").strip().lower() or "n"

        targets: list[tuple[str, str]] = []
        if choice == "a":
            targets = [(cid, name) for _, name, cid in tracked_dead] + untracked_cloud
        elif choice == "d":
            targets = [(cid, name) for _, name, cid in tracked_dead]

        for cid, name in targets:
            try:
                clients.xai.collections.delete(cid)
                registry.remove(cid)
                console.print(f"  [green]✓[/green] deleted {name} ({cid})")
            except Exception as e:
                console.print(f"  [red]✗[/red] {name} ({cid}): {e}")

    # Always prune stale registry entries (cloud already gone — nothing to delete remotely)
    for _, _, cid in tracked_missing:
        registry.remove(cid)

    registry.save()
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    registry = Registry.load()
    entries = registry.entries
    flt = (args.filter or "").lower()
    if flt:
        entries = [
            e for e in entries
            if flt in e.name.lower() or flt in e.path.lower()
        ]
    if not entries:
        msg = (
            f"no projects matching {args.filter!r}" if flt
            else "no registered projects yet — run `xli init` in a project dir"
        )
        console.print(f"[yellow]{msg}[/yellow]")
        return 0
    console.print("[bold]registered xli projects:[/bold]")
    for entry in sorted(entries, key=lambda e: e.name.lower()):
        alive = (
            Path(entry.path).is_dir()
            and (Path(entry.path) / ".xli" / "project.json").exists()
        )
        marker = "[green]●[/green]" if alive else "[red]✗[/red]"
        console.print(
            f"  {marker} [bold]{entry.name:<24}[/bold]  {entry.path:<50}  [dim]{entry.collection_id}[/dim]"
        )
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    """Manage chat keys: list / rotate / expire / revoke."""
    cfg = GlobalConfig.load()
    if not cfg.management_api_key:
        console.print(
            "[red]XAI_MANAGEMENT_API_KEY not set in env[/red] — required for key ops."
        )
        return 1
    try:
        team_id = discover_team_id(cfg)
    except BootstrapError as e:
        console.print(f"[red]{e}[/red]")
        return 1

    action = args.action
    if action == "list":
        return _keys_list(cfg, team_id)
    if action == "rotate":
        return _keys_rotate(cfg, team_id, args)
    if action == "expire":
        return _keys_expire(cfg, team_id, args)
    if action == "revoke":
        return _keys_revoke(cfg, team_id, args)
    console.print(f"[red]unknown action: {action}[/red]")
    return 1


def _keys_list(cfg: GlobalConfig, team_id: str) -> int:
    """List local keys with their server-side expiration & status."""
    from datetime import datetime, timezone
    try:
        remote = list_api_keys(cfg.management_api_key, team_id)
    except BootstrapError as e:
        console.print(f"[red]{e}[/red]")
        return 1
    by_id = {(k.get("apiKeyId") or k.get("api_key_id") or k.get("id")): k for k in remote}
    by_name = {k.get("name"): k for k in remote}

    if not cfg.keys:
        console.print("[yellow]no chat keys in config — run `xli setup`[/yellow]")
        return 0

    console.print("[bold]chat keys:[/bold]")
    now = datetime.now(timezone.utc)
    for entry in cfg.keys:
        if not isinstance(entry, dict):
            continue
        label = entry.get("label", "?")
        kid = entry.get("api_key_id")
        # If we don't have api_key_id locally, try to find by name `xli-{label}`
        rk = by_id.get(kid) if kid else by_name.get(f"xli-{label}")
        if rk is None:
            console.print(f"  · {label:<14}  [yellow]not found on xAI[/yellow]")
            continue
        exp_str = rk.get("expireTime") or entry.get("expire_time") or ""
        days_left = "—"
        if exp_str:
            try:
                exp = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                d = (exp - now).days
                days_left = f"{d}d"
                if d < 0:
                    days_left = "[red]EXPIRED[/red]"
                elif d < 7:
                    days_left = f"[yellow]{d}d[/yellow]"
            except Exception:
                days_left = "?"
        disabled = "[red](disabled)[/red]" if rk.get("disabled") else ""
        console.print(f"  · {label:<14}  expires={days_left}  name={rk.get('name', '?')} {disabled}")
    return 0


def _keys_rotate(cfg: GlobalConfig, team_id: str, args: argparse.Namespace) -> int:
    targets = _select_keys(cfg, args.label)
    if not targets:
        console.print("[yellow]no matching keys[/yellow]")
        return 0
    console.print(f"rotating [bold]{len(targets)}[/bold] key(s)…")
    for entry in targets:
        label = entry.get("label", "?")
        kid = entry.get("api_key_id")
        if not kid:
            console.print(f"  [yellow]skip {label}: no api_key_id stored (created before rotation support?)[/yellow]")
            continue
        try:
            resp = rotate_api_key(cfg.management_api_key, kid)
        except BootstrapError as e:
            console.print(f"  [red]✗[/red] {label}: {e}")
            continue
        new_secret = extract_api_key_string(resp) if isinstance(resp, dict) else None
        if not new_secret:
            console.print(f"  [red]✗[/red] {label}: response missing new secret. raw: {resp}")
            continue
        update_key_in_config(label, api_key=new_secret)
        console.print(f"  [green]✓[/green] {label}  rotated (new secret saved)")
    return 0


def _keys_expire(cfg: GlobalConfig, team_id: str, args: argparse.Namespace) -> int:
    days = args.days
    targets = _select_keys(cfg, args.label)
    if not targets:
        console.print("[yellow]no matching keys[/yellow]")
        return 0
    console.print(f"setting expiration on [bold]{len(targets)}[/bold] key(s) to +{days}d…")
    for entry in targets:
        label = entry.get("label", "?")
        kid = entry.get("api_key_id")
        if not kid:
            console.print(f"  [yellow]skip {label}: no api_key_id stored[/yellow]")
            continue
        try:
            resp = update_api_key_expiration(cfg.management_api_key, team_id, kid, days)
        except BootstrapError as e:
            console.print(f"  [red]✗[/red] {label}: {e}")
            continue
        new_exp = (resp or {}).get("expireTime") if isinstance(resp, dict) else None
        if new_exp:
            update_key_in_config(label, expire_time=new_exp)
        console.print(f"  [green]✓[/green] {label}  expireTime updated")
    return 0


def _keys_revoke(cfg: GlobalConfig, team_id: str, args: argparse.Namespace) -> int:
    # Delegate to the existing bootstrap revoke flow, scoped by prefix.
    fake = argparse.Namespace(prefix=args.prefix, yes=args.yes)
    return _bootstrap_revoke(cfg, team_id, fake)


def _select_keys(cfg: GlobalConfig, label: Optional[str]) -> list[dict]:
    """Return entries matching `label` (exact), or all chat-key entries if None."""
    out: list[dict] = []
    for e in cfg.keys:
        if not isinstance(e, dict):
            continue
        if label is None or e.get("label") == label:
            out.append(e)
    return out


def cmd_models(args: argparse.Namespace) -> int:
    """Inspect & set the models XLI uses."""
    cfg = GlobalConfig.load()
    if not cfg.management_api_key:
        console.print("[red]XAI_MANAGEMENT_API_KEY not set in env[/red]")
        return 1
    # Pass every chat key so discovery can iterate past dead/revoked entries.
    chat_keys = [
        e.get("api_key") for e in cfg.keys
        if isinstance(e, dict) and e.get("api_key")
    ]
    chat_key = chat_keys[0] if chat_keys else None

    action = args.action
    if action == "list":
        return _models_list(cfg, chat_keys)
    if action == "recommended":
        return _models_recommended(cfg, chat_keys)
    if action == "set":
        return _models_set(args)
    console.print(f"[red]unknown action: {action}[/red]")
    return 1


def _models_list(cfg: GlobalConfig, chat_keys: list[str]) -> int:
    try:
        team_id = discover_team_id(cfg)
    except BootstrapError as e:
        console.print(f"[red]{e}[/red]")
        return 1
    available = discover_models(cfg.management_api_key, team_id, chat_keys=chat_keys)
    if not available:
        console.print(
            "[yellow]no models returned — see stderr for the last endpoint error.\n"
            "If all keys are rejected, try `xli keys list` then `xli keys rotate` "
            "on a healthy one[/yellow]"
        )
        return 1
    orch = cfg.get_model_for_role("orchestrator")
    worker = cfg.get_model_for_role("worker")
    console.print(f"[bold]{len(available)} model(s) available:[/bold]")
    for m in sorted(available):
        marks = []
        if m == orch:
            marks.append("[cyan]orch[/cyan]")
        if m == worker:
            marks.append("[cyan]worker[/cyan]")
        tag = "  ←  " + " + ".join(marks) if marks else ""
        console.print(f"  · {m}{tag}")
    return 0


def _models_recommended(cfg: GlobalConfig, chat_keys: list[str]) -> int:
    try:
        team_id = discover_team_id(cfg)
    except BootstrapError as e:
        console.print(f"[red]{e}[/red]")
        return 1
    available = discover_models(cfg.management_api_key, team_id, chat_keys=chat_keys)
    if not available:
        console.print("[yellow]no models returned[/yellow]")
        return 1
    orch, worker = pick_best_models(available)
    console.print("[bold]heuristic recommendations:[/bold]")
    console.print(f"  orchestrator: [cyan]{orch or '(none)'}[/cyan]")
    console.print(f"  worker:       [cyan]{worker or '(none)'}[/cyan]")
    cur_orch = cfg.get_model_for_role("orchestrator")
    cur_worker = cfg.get_model_for_role("worker")
    if orch and orch != cur_orch:
        console.print(
            f"  [dim]apply orch:   [/dim] [cyan]xli models set --orchestrator {orch}[/cyan]"
        )
    if worker and worker != cur_worker:
        console.print(
            f"  [dim]apply worker: [/dim] [cyan]xli models set --worker {worker}[/cyan]"
        )
    return 0


def _models_set(args: argparse.Namespace) -> int:
    if not args.orchestrator and not args.worker:
        console.print("[red]nothing to set — pass --orchestrator and/or --worker[/red]")
        return 1
    set_models_in_config(args.orchestrator, args.worker, auto_detected=False)
    if args.orchestrator:
        console.print(f"[green]✓[/green] orchestrator_model = [cyan]{args.orchestrator}[/cyan]")
    if args.worker:
        console.print(f"[green]✓[/green] worker_model       = [cyan]{args.worker}[/cyan]")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """One-shot first-time setup: template → env-key check → primary + N workers.

    Idempotent: skips steps that are already done. Safe to re-run.
    """
    import time as _time

    # Step 1 — ensure config file exists
    if not GLOBAL_CONFIG_FILE.exists():
        GlobalConfig.write_template()
        console.print(f"[green]✓[/green] wrote config template at {GLOBAL_CONFIG_FILE}")
    else:
        console.print(f"[dim]·[/dim] config exists at {GLOBAL_CONFIG_FILE}")

    cfg = GlobalConfig.load()

    # Step 2 — management key (env-only, never persisted)
    if not cfg.management_api_key:
        console.print(
            "\n[red]XAI_MANAGEMENT_API_KEY is not set in your environment[/red]"
        )
        console.print(
            "Add this to your shell rc (~/.bashrc, ~/.zshrc, etc.) and re-source it:\n"
            "  [cyan]export XAI_MANAGEMENT_API_KEY=xai-...your-management-key...[/cyan]\n\n"
            "[dim]Why env, not config? The management key creates other keys + manages "
            "your collections. Keeping it out of any on-disk file makes it much harder "
            "to leak through git, screen-shares, or backups. The chat keys we provision "
            "next ARE persisted (revocable) so you only deal with this once.[/dim]"
        )
        return 1
    console.print("[dim]·[/dim] XAI_MANAGEMENT_API_KEY found in environment")

    # Migration nudge: warn if a legacy management_api_key is still in the file
    if GlobalConfig.mgmt_key_in_file():
        console.print(
            "[yellow]⚠ legacy management_api_key found in config.json — "
            "remove it (env var is the only valid source now)[/yellow]"
        )

    # Step 3 — team_id discovery + cache
    try:
        team_id = discover_team_id(cfg)
    except BootstrapError as e:
        console.print(f"[red]team discovery failed: {e}[/red]")
        return 1
    if not cfg.team_id:
        set_team_id_in_config(team_id)
        console.print(f"[green]✓[/green] team_id discovered + cached: {team_id}")
        cfg = GlobalConfig.load()
    else:
        console.print(f"[dim]·[/dim] team_id already cached: {team_id}")

    # Step 4 — chat keys
    existing = len(cfg.key_pairs())
    workers = args.workers

    if existing >= 1 + workers and not args.force:
        console.print(
            f"\n[dim]·[/dim] pool already has {existing} key(s) — skipping bootstrap "
            "(re-run with --force to add more)"
        )
        return _setup_finish(cfg, console)

    console.print()
    expire_days = args.expire_days
    if existing == 0:
        # Step 4a — primary
        console.print(f"creating [bold]primary[/bold] key (expires in {expire_days}d)…")
        ok = _create_one_key(cfg, team_id, label="primary-1",
                             name_on_server="xli-primary-1",
                             expire_days=expire_days)
        if not ok:
            return 1
        cfg = GlobalConfig.load()

        # Step 4a.1 — model auto-detection (now that we have a chat key to query with)
        chat_key = cfg.keys[0].get("api_key") if cfg.keys else None
        _maybe_auto_detect_models(cfg, team_id, chat_key, args)
        cfg = GlobalConfig.load()

    # Step 4b — workers
    needed = max(0, workers - max(0, existing - 1))
    if needed:
        console.print(f"creating [bold]{needed}[/bold] worker key(s) (expires in {expire_days}d)…")
        for i in range(1, needed + 1):
            ok = _create_one_key(
                cfg,
                team_id,
                label=f"worker-{i}",
                name_on_server=f"xli-worker-{i}",
                expire_days=expire_days,
            )
            if not ok:
                return 1
            cfg = GlobalConfig.load()
            if i < needed:
                _time.sleep(INTER_CREATE_DELAY_SEC)

    return _setup_finish(GlobalConfig.load(), console)


def _create_one_key(cfg: GlobalConfig, team_id: str, *, label: str, name_on_server: str,
                    expire_days: int | None = None) -> bool:
    """Create one API key, append to config, print success line. Returns True on success."""
    from xli.bootstrap import _extract_api_key_id, DEFAULT_EXPIRE_DAYS
    if expire_days is None:
        expire_days = DEFAULT_EXPIRE_DAYS
    # Bump label suffix if it collides with an existing entry
    base = label
    n = int(label.rsplit("-", 1)[-1])
    while any((e.get("label") if isinstance(e, dict) else None) == label for e in cfg.keys):
        n += 1
        label = f"{base.rsplit('-', 1)[0]}-{n}"
        name_on_server = f"xli-{label}"
    try:
        resp = create_api_key(cfg.management_api_key, team_id, name_on_server,
                              expire_days=expire_days)
    except BootstrapError as e:
        console.print(f"  [red]✗[/red] {label}: {e}")
        return False
    secret = extract_api_key_string(resp) if isinstance(resp, dict) else None
    if not secret:
        console.print(f"  [red]✗[/red] {label}: response missing key string. raw: {resp}")
        return False
    api_key_id = _extract_api_key_id(resp) if isinstance(resp, dict) else None
    expire_time = (
        resp.get("expireTime") or resp.get("expire_time")
        if isinstance(resp, dict) else None
    )
    entry = {"api_key": secret, "label": label}
    if api_key_id:
        entry["api_key_id"] = api_key_id
    if expire_time:
        entry["expire_time"] = expire_time
    append_keys_to_config([entry])
    exp_note = f"  (expires in {expire_days}d)" if expire_days else ""
    console.print(f"  [green]✓[/green] {label}  →  xAI: {name_on_server}{exp_note}")
    return True


def _maybe_auto_detect_models(
    cfg: GlobalConfig, team_id: str, chat_key: Optional[str], args: argparse.Namespace
) -> None:
    """If the user hasn't pinned models, query xAI and pick the best pair.

    Skips if user already has both models set explicitly (orchestrator_model
    and worker_model both non-None).
    """
    user_pinned = cfg.orchestrator_model and cfg.worker_model
    if user_pinned:
        console.print(
            f"[dim]·[/dim] models pinned in config "
            f"(orch={cfg.orchestrator_model}, worker={cfg.worker_model}) — skipping auto-detect"
        )
        return
    console.print("discovering available models on this team…")
    chat_keys = [
        e.get("api_key") for e in cfg.keys
        if isinstance(e, dict) and e.get("api_key")
    ] or ([chat_key] if chat_key else [])
    available = discover_models(cfg.management_api_key, team_id, chat_keys=chat_keys)
    if not available:
        console.print(
            "[yellow]could not discover models (endpoint unavailable) — keeping defaults[/yellow]"
        )
        return
    orch, worker = pick_best_models(available)
    if not orch and not worker:
        console.print(
            f"[yellow]found {len(available)} model(s) but couldn't classify any[/yellow]"
        )
        return
    set_models_in_config(orch, worker, auto_detected=True)
    console.print(f"[green]✓[/green] auto-detected from {len(available)} model(s):")
    if orch:
        console.print(f"    orchestrator: [cyan]{orch}[/cyan]")
    if worker:
        console.print(f"    worker:       [cyan]{worker}[/cyan]")


def _setup_finish(cfg: GlobalConfig, console_) -> int:
    pool = len(cfg.key_pairs())
    console_.print(
        f"\n[green]setup complete[/green] — pool size: {pool} key(s)"
    )
    console_.print(
        "[dim]next:[/dim] [cyan]xli init[/cyan] in your project dir, "
        "or [cyan]xli new <name>[/cyan] to start fresh"
    )
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Provision worker API keys via the xAI Management REST API."""
    cfg = GlobalConfig.load()
    if not cfg.management_api_key:
        console.print(
            "[red]management_api_key not set in config.json[/red] — "
            "run `xli config` and paste your management key first."
        )
        return 1

    try:
        team_id = discover_team_id(cfg)
    except BootstrapError as e:
        console.print(f"[red]{e}[/red]")
        return 1
    if not team_id:
        console.print("[red]team_id resolution returned empty[/red]")
        return 1
    if not cfg.team_id:
        # cache it so we skip discovery next time
        set_team_id_in_config(team_id)
        console.print(f"[dim]team_id discovered + cached: {team_id}[/dim]")
    else:
        console.print(f"[dim]team_id: {team_id}[/dim]")

    if args.revoke:
        return _bootstrap_revoke(cfg, team_id, args)

    return _bootstrap_create(cfg, team_id, args)


def _bootstrap_create(cfg: GlobalConfig, team_id: str, args: argparse.Namespace) -> int:
    prefix = args.prefix
    count = args.count

    existing_pool_size = len(cfg.key_pairs())
    existing_with_prefix = [
        e for e in cfg.keys
        if isinstance(e, dict) and (e.get("label") or "").startswith(f"{prefix}-")
    ]
    if existing_with_prefix and not args.force:
        console.print(
            f"[yellow]you already have {len(existing_with_prefix)} key(s) labeled "
            f"'{prefix}-*' in config[/yellow]. Re-run with --force to add {count} more, "
            f"or use --revoke to remove them first."
        )
        return 0

    import time as _time
    console.print(f"creating [bold]{count}[/bold] worker key(s) on xAI…")
    new_entries: list[dict] = []
    new_secrets: list[tuple[str, str]] = []  # (label, secret) for warning print
    for i in range(1, count + 1):
        # Choose a label that doesn't collide with an existing one in config.
        n = i
        label = f"{prefix}-{n}"
        while any((e.get("label") if isinstance(e, dict) else None) == label for e in cfg.keys + new_entries):
            n += 1
            label = f"{prefix}-{n}"
        name_on_server = f"xli-{label}"
        try:
            resp = create_api_key(
                cfg.management_api_key, team_id, name_on_server,
                expire_days=args.expire_days,
            )
        except BootstrapError as e:
            console.print(f"  [red]✗[/red] {label}: {e}")
            console.print(
                f"[yellow]aborting — {len(new_entries)} key(s) created so far have been written to config[/yellow]"
            )
            if new_entries:
                append_keys_to_config(new_entries)
            return 1
        from xli.bootstrap import _extract_api_key_id
        secret = extract_api_key_string(resp) if isinstance(resp, dict) else None
        if not secret:
            console.print(
                f"  [red]✗[/red] {label}: response did not contain a key string. raw response:\n  {resp}"
            )
            return 1
        api_key_id = _extract_api_key_id(resp) if isinstance(resp, dict) else None
        expire_time = (
            resp.get("expireTime") or resp.get("expire_time")
            if isinstance(resp, dict) else None
        )
        entry = {"api_key": secret, "label": label}
        if api_key_id:
            entry["api_key_id"] = api_key_id
        if expire_time:
            entry["expire_time"] = expire_time
        new_entries.append(entry)
        new_secrets.append((label, secret))
        exp_note = f"  (expires in {args.expire_days}d)" if args.expire_days else ""
        console.print(f"  [green]✓[/green] {label}  →  xAI name: {name_on_server}{exp_note}")
        # Pace creates so we don't get throttled batch-wide
        if i < count:
            _time.sleep(INTER_CREATE_DELAY_SEC)

    path = append_keys_to_config(new_entries)
    console.print(
        f"\n[green]wrote {len(new_entries)} key(s) to {path}[/green]  "
        f"(pool now {existing_pool_size + len(new_entries)})"
    )
    console.print(
        "\n[bold yellow]⚠ copy these somewhere safe — xAI only shows the full key once:[/bold yellow]"
    )
    for label, secret in new_secrets:
        console.print(f"  {label:<14}  {secret}")
    console.print(
        "\n[dim]next:[/dim] [cyan]xli init[/cyan]  in your project, "
        "or [cyan]xli new <name>[/cyan] to spin up a fresh one"
    )
    return 0


def _bootstrap_revoke(cfg: GlobalConfig, team_id: str, args: argparse.Namespace) -> int:
    prefix = args.prefix
    name_prefix = f"xli-{prefix}-"
    console.print(f"listing API keys on xAI to find names starting with '{name_prefix}'…")
    try:
        keys = list_api_keys(cfg.management_api_key, team_id)
    except BootstrapError as e:
        console.print(f"[red]{e}[/red]")
        return 1

    matches = [
        k for k in keys
        if (k.get("name") or "").startswith(name_prefix)
    ]
    if not matches:
        console.print(f"[yellow]no API keys named '{name_prefix}*' on xAI[/yellow]")
    else:
        console.print(f"will delete {len(matches)} API key(s) on xAI:")
        for k in matches:
            console.print(f"  · {k.get('name', '?')}  id={k.get('api_key_id') or k.get('id', '?')}")
        if not args.yes:
            ans = input("proceed? [y/N] ").strip().lower()
            if ans != "y":
                console.print("[dim]cancelled[/dim]")
                return 0
        for k in matches:
            kid = k.get("api_key_id") or k.get("id")
            if not kid:
                console.print(f"  [yellow]skip {k.get('name', '?')}: no id field[/yellow]")
                continue
            try:
                delete_api_key(cfg.management_api_key, team_id, kid)
                console.print(f"  [green]✓[/green] revoked {k.get('name', '?')}")
            except BootstrapError as e:
                console.print(f"  [red]✗[/red] {k.get('name', '?')}: {e}")

    # Strip matching entries from local config too
    path, n_removed = remove_keys_from_config(
        lambda e: isinstance(e, dict) and (e.get("label") or "").startswith(f"{prefix}-")
    )
    console.print(f"removed {n_removed} matching entry(s) from {path}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    path = GlobalConfig.write_template()
    console.print(f"[green]✓[/green] config at {path}")
    console.print("[dim]edit it: paste your management_api_key + add api keys to the keys[] list[/dim]")
    return 0


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


def cmd_new(args: argparse.Namespace) -> int:
    """Create a new project directory and initialize it."""
    name = args.name
    base = Path(args.path or ".").resolve()
    project_root = base / name
    if project_root.exists():
        console.print(f"[red]already exists: {project_root}[/red]")
        return 1
    project_root.mkdir(parents=True)
    console.print(f"[green]✓[/green] created {project_root}")
    return cmd_init(
        argparse.Namespace(
            path=str(project_root),
            name=name,
            collection_id=None,
            sync=True,
            force=False,
        )
    )


def cmd_doc(args: argparse.Namespace) -> int:
    """Manage reference docs at ~/.config/xli/docs/<name>.md.

    Sub-routes off the action flags. Docs are markdown files inlined into
    the agent's system prompt when attached via /doc <name> in either REPL.
    """
    from xli.doc import (
        Doc, DOCS_DIR, create_doc, delete_doc, is_valid_name as _is_valid,
        list_docs, open_in_editor as _open_in_editor,
    )

    if args.list:
        docs = list_docs()
        if not docs:
            console.print(
                f"[dim](no docs yet — create one with [/dim][cyan]xli doc --new <name>[/cyan][dim])[/dim]"
            )
            return 0
        for d in docs:
            console.print(
                f"  [bold]{d.name}[/bold]  [dim]{d.size_bytes():,}b · {d.first_line()}[/dim]"
            )
        return 0

    if args.new:
        name = args.new
        if not _is_valid(name):
            console.print(f"[red]invalid doc name: {name!r}[/red]")
            return 1
        d = Doc(name)
        if d.exists():
            console.print(
                f"[yellow]doc {name!r} already exists[/yellow] — use --edit instead"
            )
            return 1
        create_doc(name)
        console.print(f"[green]✓[/green] created doc [bold]{name}[/bold] at {d.path}")
        console.print("[dim]opening $EDITOR — save and quit when done…[/dim]")
        _open_in_editor(d.path)
        console.print(
            f"[dim]ready. In any REPL, run [/dim][cyan]/doc {name}[/cyan][dim] to attach it.[/dim]"
        )
        return 0

    if args.edit:
        d = Doc(args.edit)
        if not d.exists():
            console.print(f"[red]no such doc: {args.edit!r}[/red]")
            return 1
        _open_in_editor(d.path)
        console.print(
            f"[dim]ready. Re-attach with [/dim][cyan]/doc {args.edit}[/cyan][dim] "
            "for the changes to take effect (already-running sessions hold the old text).[/dim]"
        )
        return 0

    if args.delete:
        d = Doc(args.delete)
        if not d.exists():
            console.print(f"[red]no such doc: {args.delete!r}[/red]")
            return 1
        if not args.yes:
            console.print(
                f"[yellow]about to delete doc [bold]{args.delete}[/bold][/yellow]\n"
                f"  path: {d.path}"
            )
            try:
                ans = input("delete? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            if ans != "y":
                console.print("[dim]aborted[/dim]")
                return 1
        delete_doc(args.delete)
        console.print(f"[green]✓[/green] deleted doc {args.delete!r}")
        return 0

    # No flag → default to listing.
    return cmd_doc(argparse.Namespace(list=True, new=None, edit=None, delete=None, yes=False))


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
        prompt_prefix = f"[{persona.name}] › "
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
        if _handle_ref_command(user_input, agent):
            continue
        if _handle_doc_command(user_input, agent):
            continue
        if user_input == "/help":
            console.print(CHAT_SLASH_HELP)
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

        # Real conversation turn.
        try:
            text, dirty, turn_stats = agent.run_turn(user_input)
        except Exception as e:
            console.print(f"[red]turn failed: {e}[/red]")
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
  /sync                 Sync turn-files to the Collection now
  /yolo / /safe         Toggle bash confirmation gate

[dim]Tip: ask the model to recall something specific — it will use search_project[/dim]
[dim]over the synced turn-files for long-term memory beyond the inline window.[/dim]
"""


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
        prompt_prefix = f"{prefix_tag} › " if prefix_tag else "› "
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
        if _handle_ref_command(user_input, agent):
            continue
        if _handle_doc_command(user_input, agent):
            continue
        if user_input == "/help":
            console.print(SLASH_HELP)
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
            console.print(
                "[yellow]plan mode ON[/yellow] — next turn will investigate read-only and produce a plan. "
                "/execute to approve, /cancel to drop."
            )
            continue
        if user_input == "/cancel":
            if agent.plan_mode:
                agent.plan_mode = False
                console.print("[dim]plan mode off[/dim]")
            else:
                console.print("[dim](not in plan mode)[/dim]")
            continue
        if user_input == "/cost":
            _print_pricing(cfg)
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
            console.print("[green]plan approved — executing[/green]")
            user_input = "Approved. Execute the plan above using all available tools."

        try:
            text, dirty, turn_stats = agent.run_turn(user_input)
        except Exception as e:
            console.print(f"[red]turn failed: {e}[/red]")
            continue

        if text:
            console.print()
            console.print(text)
        console.print(_format_turn_line(turn_stats))
        for warn in turn_stats.warnings:
            console.print(f"  [yellow]⚠ {warn}[/yellow]")

        if dirty and not project.local_only:
            with console.status("[cyan]end-of-turn sync…[/cyan]"):
                stats = sync_project(pool.primary(), project, cfg)
            console.print(f"[dim]sync: {stats.summary()}[/dim]")


def main() -> int:
    p = argparse.ArgumentParser(prog="xli", description="Grok + xAI Collections coding agent.")
    p.add_argument("--version", action="version", version=f"xli {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Initialize an xli project. Positional NAME labels the collection.")
    p_init.add_argument("name", nargs="?", help="Project name (default: cwd basename)")
    p_init.add_argument("--path", help="Project directory (default: cwd)")
    p_init.add_argument("--collection-id", help="Reuse an existing collection instead of creating one")
    p_init.add_argument("--no-sync", dest="sync", action="store_false", help="Skip the initial sync")
    p_init.add_argument("--force", action="store_true", help="Reinitialize even if project exists")
    p_init.add_argument("--local", action="store_true",
                        help="Local-only mode: no Collection, no upload, no sync. search_project disabled.")
    p_init.add_argument("--snapshot", action="store_true",
                        help="Cache a paths+sizes index at .xli/index.txt for fast structural search.")
    p_init.set_defaults(func=cmd_init, sync=True)

    p_new = sub.add_parser("new", help="Create a new project directory and initialize it.")
    p_new.add_argument("name", help="Project name (also the new directory name)")
    p_new.add_argument("--path", help="Parent directory (default: cwd)")
    p_new.set_defaults(func=cmd_new)

    p_projects = sub.add_parser("projects", help="List all registered xli projects (filter by substring).")
    p_projects.add_argument("filter", nargs="?", help="Optional substring filter (matches name or path)")
    p_projects.set_defaults(func=cmd_projects)

    p_sync = sub.add_parser("sync", help="Push local changes to the project's collection.")
    p_sync.add_argument("path", nargs="?", default=".")
    p_sync.add_argument("--dry-run", action="store_true")
    p_sync.set_defaults(func=cmd_sync)

    p_status = sub.add_parser("status", help="Show config + project state.")
    p_status.add_argument("path", nargs="?", default=".")
    p_status.set_defaults(func=cmd_status)

    p_code = sub.add_parser(
        "code",
        help="Project-scoped code agent REPL. Pass a project NAME (registry lookup) or PATH; default cwd.",
    )
    p_code.add_argument("target", nargs="?", help="Project name (from registry) or path")
    p_code.add_argument("--yolo", action="store_true",
                        help="Auto-approve every bash command regardless of intent (no confirmation prompts)")
    p_code.set_defaults(func=cmd_code)

    p_chat = sub.add_parser(
        "chat",
        help="Persona-based conversational agent with persistent memory (each persona has its own Collection).",
    )
    p_chat.add_argument("name", nargs="?", help="Persona name (default: most-recently-used or 'default')")
    p_chat.add_argument("--new", metavar="NAME", help="Create a new persona; opens $EDITOR on its prompt file")
    p_chat.add_argument("--list", action="store_true", help="List all personas and exit")
    p_chat.add_argument("--edit", metavar="NAME", help="Open an existing persona's prompt in $EDITOR")
    p_chat.add_argument("--delete", metavar="NAME", help="Delete a persona (prompt + state dir)")
    p_chat.add_argument("--yolo", action="store_true", help="Auto-approve bash commands")
    p_chat.add_argument("--yes", action="store_true", help="Skip confirmation prompts (used with --delete)")
    p_chat.set_defaults(func=cmd_chat)

    p_config = sub.add_parser("config", help="Write a config template to ~/.config/xli/config.json if missing.")
    p_config.set_defaults(func=cmd_config)

    p_setup = sub.add_parser(
        "setup",
        help="One-shot first-time setup: writes config, checks env mgmt key, provisions primary + workers.",
    )
    p_setup.add_argument("--workers", type=int, default=8, help="Number of worker keys to create (default: 8)")
    p_setup.add_argument("--expire-days", type=int, default=DEFAULT_EXPIRE_DAYS,
                         help=f"Key expiration in days (default: {DEFAULT_EXPIRE_DAYS}; 0 = no expiry)")
    p_setup.add_argument("--force", action="store_true", help="Re-run bootstrap even if pool already populated")
    p_setup.set_defaults(func=cmd_setup)

    p_bootstrap = sub.add_parser(
        "bootstrap",
        help="Provision worker API keys via the management API (lower-level than `setup`).",
    )
    p_bootstrap.add_argument("--count", type=int, default=8, help="How many worker keys to create (default: 8)")
    p_bootstrap.add_argument("--prefix", default="worker", help="Label prefix for created keys (default: 'worker')")
    p_bootstrap.add_argument("--expire-days", type=int, default=DEFAULT_EXPIRE_DAYS,
                             help=f"Key expiration in days (default: {DEFAULT_EXPIRE_DAYS}; 0 = no expiry)")
    p_bootstrap.add_argument("--force", action="store_true", help="Add new keys even if matching prefix already exists")
    p_bootstrap.add_argument("--revoke", action="store_true", help="Revoke (delete) all keys with matching prefix")
    p_bootstrap.add_argument("--yes", action="store_true", help="Skip confirmation when revoking")
    p_bootstrap.set_defaults(func=cmd_bootstrap)

    p_models = sub.add_parser("models", help="Inspect or set the orchestrator/worker models.")
    p_models_sub = p_models.add_subparsers(dest="action", required=True)

    p_models_list = p_models_sub.add_parser("list", help="List models the team has access to.")
    p_models_list.set_defaults(func=cmd_models)

    p_models_rec = p_models_sub.add_parser("recommended", help="Show heuristic best-of-class picks.")
    p_models_rec.set_defaults(func=cmd_models)

    p_models_set = p_models_sub.add_parser("set", help="Pin orchestrator and/or worker model(s).")
    p_models_set.add_argument("--orchestrator", help="Model id for the main agent")
    p_models_set.add_argument("--worker", help="Model id for dispatched workers")
    p_models_set.set_defaults(func=cmd_models)

    p_keys = sub.add_parser("keys", help="Manage chat keys (list / rotate / expire / revoke).")
    p_keys_sub = p_keys.add_subparsers(dest="action", required=True)

    p_keys_list = p_keys_sub.add_parser("list", help="List local chat keys with their server-side expiration.")
    p_keys_list.set_defaults(func=cmd_keys)

    p_keys_rot = p_keys_sub.add_parser("rotate", help="Rotate the secret of one or all keys (same key_id, new value).")
    p_keys_rot.add_argument("--label", help="Rotate only this label (otherwise: all)")
    p_keys_rot.set_defaults(func=cmd_keys)

    p_keys_exp = p_keys_sub.add_parser("expire", help="Update expireTime on existing key(s).")
    p_keys_exp.add_argument("--days", type=int, required=True, help="Days from now (0 = remove expiry)")
    p_keys_exp.add_argument("--label", help="Apply to a single label (otherwise: all)")
    p_keys_exp.set_defaults(func=cmd_keys)

    p_keys_rev = p_keys_sub.add_parser("revoke", help="Delete keys by label prefix (server-side + local).")
    p_keys_rev.add_argument("--prefix", default="worker", help="Label prefix to revoke (default: worker)")
    p_keys_rev.add_argument("--yes", action="store_true", help="Skip confirmation")
    p_keys_rev.set_defaults(func=cmd_keys)

    p_gc = sub.add_parser("gc", help="Find and delete orphan xAI collections.")
    p_gc.add_argument("--dry-run", action="store_true", help="Show what would be deleted, take no action")
    p_gc.add_argument("--yes", action="store_true", help="Delete all orphans without prompting")
    p_gc.set_defaults(func=cmd_gc)

    p_doc = sub.add_parser(
        "doc",
        help="Manage reference docs (markdown files attached via /doc in any REPL).",
    )
    p_doc.add_argument("--new", metavar="NAME", help="Create a new doc; opens $EDITOR")
    p_doc.add_argument("--list", action="store_true", help="List all docs")
    p_doc.add_argument("--edit", metavar="NAME", help="Open an existing doc in $EDITOR")
    p_doc.add_argument("--delete", metavar="NAME", help="Delete a doc")
    p_doc.add_argument("--yes", action="store_true", help="Skip confirmation prompt for --delete")
    p_doc.set_defaults(func=cmd_doc)

    p_scratch = sub.add_parser(
        "scratch",
        help="Spin up an ephemeral local-only project under ~/.xli/scratch/<name>/ and drop into chat.",
    )
    p_scratch.add_argument("name", nargs="?", help="Scratch name (default: timestamp)")
    p_scratch.add_argument("--no-chat", action="store_true", help="Just create the project, don't enter chat")
    p_scratch.add_argument("--yolo", action="store_true", help="Pass --yolo to chat (auto-approve bash)")
    p_scratch.add_argument("--force", action="store_true", help="Re-init even if scratch with this name exists")
    p_scratch.set_defaults(func=cmd_scratch)

    p_help = sub.add_parser("help", help="Show grouped command listing.")
    p_help.set_defaults(func=cmd_help)

    args = p.parse_args()
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
