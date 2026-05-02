"""Models subcommands (list, recommended, set)."""

from __future__ import annotations

import argparse

from rich.console import Console

from xli.bootstrap import (
    BootstrapError,
    discover_team_id,
    discover_models,
    pick_best_models,
    set_models_in_config,
)
from xli.config import GlobalConfig

console = Console()
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


