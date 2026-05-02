from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Optional

from rich.console import Console

from xli.bootstrap import (
    BootstrapError,
    discover_team_id,
    list_api_keys,
    rotate_api_key,
    update_api_key_expiration,
    delete_api_key,
    extract_api_key_string,
    update_key_in_config,
    remove_keys_from_config,
)
from xli.config import GlobalConfig
from .key_ops import bootstrap_revoke

console = Console()


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
    # Delegate to the shared revoke helper.
    fake = argparse.Namespace(prefix=args.prefix, yes=args.yes)
    return bootstrap_revoke(cfg, team_id, fake)


def _select_keys(cfg: GlobalConfig, label: Optional[str]) -> list[dict]:
    """Return entries matching `label` (exact), or all chat-key entries if None."""
    out: list[dict] = []
    for e in cfg.keys:
        if not isinstance(e, dict):
            continue
        if label is None or e.get("label") == label:
            out.append(e)
    return out
