"""`xli setup` subcommand: one-shot first-time provisioning.

Walks through config-template → env-key check → team-id discovery → primary +
N worker keys → optional model auto-detection. Idempotent: skips steps that
are already done. Safe to re-run.
"""

from __future__ import annotations

import argparse
from typing import Optional

from rich.console import Console

from xli.bootstrap import (
    BootstrapError,
    INTER_CREATE_DELAY_SEC,
    append_keys_to_config,
    create_api_key,
    discover_models,
    discover_team_id,
    extract_api_key_string,
    pick_best_models,
    set_models_in_config,
    set_team_id_in_config,
)
from xli.config import GLOBAL_CONFIG_FILE, GlobalConfig

console = Console()


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
