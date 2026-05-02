from __future__ import annotations

import argparse
from pathlib import Path

from rich.console import Console

from xli import __version__
from xli.config import GLOBAL_CONFIG_FILE, GlobalConfig, ProjectConfig
from xli.registry import REGISTRY_FILE, Registry

console = Console()


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


def print_pricing(cfg: GlobalConfig) -> None:
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
