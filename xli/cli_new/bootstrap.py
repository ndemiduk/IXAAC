"""`xli bootstrap` subcommand: provision worker API keys via xAI's Management REST API.

Creates a batch of API keys (default count=N) labeled with a shared prefix and
writes them into config.json. Pairs with `xli bootstrap --revoke` (delegates to
key_ops.bootstrap_revoke) for cleanup.
"""

from __future__ import annotations

import argparse

from rich.console import Console

from xli.bootstrap import (
    BootstrapError,
    INTER_CREATE_DELAY_SEC,
    append_keys_to_config,
    create_api_key,
    discover_team_id,
    extract_api_key_string,
    set_team_id_in_config,
)
from xli.config import GlobalConfig

from .key_ops import bootstrap_revoke

console = Console()


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Provision worker API keys via the xAI Management REST API."""
    cfg = GlobalConfig.load()
    if not cfg.management_api_key:
        console.print(
            "[red]XAI_MANAGEMENT_API_KEY not set in env[/red] — "
            "export it first (see `xli status`)."
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
        return bootstrap_revoke(cfg, team_id, args)

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
