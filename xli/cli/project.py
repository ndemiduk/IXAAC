"""Project lifecycle subcommands: init / new / sync / gc / scratch / projects / workspaces.

These all operate on a project (`.xli/project.json` directory) or the
cross-project registry/workspaces files at ~/.config/xli/.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from rich.console import Console

from xli import workspaces as ws_mod
from xli.client import Clients, MissingCredentials
from xli.config import GlobalConfig, ProjectConfig
from xli.registry import Registry
from xli.sync import init_project, sync_project

console = Console()


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


def cmd_scratch(args: argparse.Namespace) -> int:
    """Create an ephemeral local-only project under ~/.xli/scratch/<name>/ and drop into chat.

    Use for: one-off file-management tasks ("rename these", "find duplicates"),
    quick experiments, anything you don't want to upload as a project.
    For snapshotting an existing big directory (NAS, media collection), run
    `xli init --local --snapshot` *in that directory* instead — scratch creates
    a fresh empty dir.
    """
    from datetime import datetime

    from .repl import cmd_code  # imported lazily to avoid eager REPL deps for non-REPL paths

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


def cmd_sync(args: argparse.Namespace) -> int:
    cfg = GlobalConfig.load()
    project = ProjectConfig.load(Path(args.path).resolve())
    if not project:
        console.print("[red]not an xli project — run `xli init` first[/red]")
        return 1
    if project.local_only:
        clients = None
    else:
        try:
            clients = Clients.from_config(cfg)
        except MissingCredentials as e:
            console.print(f"[red]{e}[/red]")
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


def cmd_workspaces(args: argparse.Namespace) -> int:
    """List/manage iXaac workspaces (project + snapshot directories).

    Workspaces is a broader registry than `xli projects` — it tracks every
    directory xli has been invoked in, plus any directory the user explicitly
    registers (e.g. archived references). The XMPP daemon (Phase 2) reads it
    to know where to dispatch agent runs.
    """
    action = getattr(args, "ws_action", None) or "list"

    if action == "list":
        ws = ws_mod.Workspaces.load()
        entries = ws.entries
        if getattr(args, "projects_only", False):
            entries = [e for e in entries if e.kind == ws_mod.KIND_PROJECT]
        if getattr(args, "snapshots_only", False):
            entries = [e for e in entries if e.kind == ws_mod.KIND_SNAPSHOT]
        if not entries:
            console.print(
                "[yellow]no workspaces yet — run `xli` in a project dir, or "
                "`xli workspaces add <path>`[/yellow]"
            )
            return 0
        entries = sorted(entries, key=lambda e: e.last_active, reverse=True)
        console.print("[bold]xli workspaces:[/bold] (most-recent first)")
        for e in entries:
            kind_marker = (
                "[green]●[/green]" if e.kind == ws_mod.KIND_PROJECT
                else "[yellow]○[/yellow]"
            )
            alias_str = f"[bold cyan]{e.alias}[/bold cyan]" if e.alias else "[dim]—[/dim]"
            console.print(
                f"  {kind_marker} {alias_str:<25} {e.path:<50} "
                f"[dim]last: {e.last_active[:19]}[/dim]"
            )
            if e.notes:
                console.print(f"      [dim italic]{e.notes}[/dim italic]")
        return 0

    if action == "add":
        try:
            entry = ws_mod.add(
                args.path,
                kind=ws_mod.KIND_SNAPSHOT if args.snapshot else ws_mod.KIND_PROJECT,
                alias=args.alias,
                notes=args.notes,
            )
        except (FileNotFoundError, ValueError) as e:
            console.print(f"[red]{e}[/red]")
            return 1
        suffix = f" (alias: [bold]{entry.alias}[/bold])" if entry.alias else ""
        console.print(f"[green]added[/green] {entry.path} as [bold]{entry.kind}[/bold]{suffix}")
        return 0

    if action in ("snapshot", "project"):
        try:
            entry = ws_mod.set_kind(args.key, action)
        except (KeyError, ValueError) as e:
            console.print(f"[red]{e}[/red]")
            return 1
        console.print(f"[green]marked {action}:[/green] {entry.path}")
        return 0

    if action == "alias":
        try:
            entry = ws_mod.set_alias(args.key, args.alias)
        except KeyError as e:
            console.print(f"[red]{e}[/red]")
            return 1
        if args.alias:
            console.print(f"[green]aliased[/green] {entry.path} as [bold]{args.alias}[/bold]")
        else:
            console.print(f"[green]cleared alias[/green] for {entry.path}")
        return 0

    if action == "remove":
        if ws_mod.remove(args.key):
            console.print(f"[green]removed:[/green] {args.key}")
            return 0
        console.print(f"[red]not found:[/red] {args.key}")
        return 1

    console.print(f"[red]unknown workspaces action:[/red] {action}")
    return 2
