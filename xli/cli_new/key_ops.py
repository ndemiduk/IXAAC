"""Shared key lifecycle operations used by both `keys` and `bootstrap` commands."""

import argparse

from rich.console import Console

from xli.bootstrap import (
    BootstrapError,
    delete_api_key,
    list_api_keys,
    remove_keys_from_config,
)
from xli.config import GlobalConfig

console = Console()


def bootstrap_revoke(cfg: GlobalConfig, team_id: str, args: argparse.Namespace) -> int:
    """Revoke keys by label prefix (server + local). Called from cmd_bootstrap."""
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
