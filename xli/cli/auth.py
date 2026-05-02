"""`xli auth` subcommand: manage plugin credentials in the encrypted vault.

The vault lives at ~/.config/xli/vault.enc (Fernet-encrypted JSON). Master key
is in the OS keyring by default; falls back to ~/.config/xli/.vault-key or
$XLI_VAULT_KEY for headless use. First `xli auth set` provisions a key
automatically — no explicit init needed.
"""

from __future__ import annotations

import argparse

from rich.console import Console

console = Console()


def cmd_auth(args: argparse.Namespace) -> int:
    """Manage plugin credentials in the encrypted vault.

    The vault lives at ~/.config/xli/vault.enc (Fernet-encrypted JSON).
    Master key is in the OS keyring by default; falls back to ~/.config/xli/.vault-key
    or $XLI_VAULT_KEY for headless use. First `xli auth set` provisions a key
    automatically — no explicit init needed.
    """
    from xli.vault import Vault, VaultError, VAULT_FILE
    from xli.plugin import is_valid_id

    action = getattr(args, "auth_action", None)

    if action == "set":
        if not is_valid_id(args.plugin):
            console.print(f"[red]invalid plugin id: {args.plugin!r}[/red]")
            return 1
        pairs: list[tuple[str, str]] = []
        for raw in args.assignments:
            if "=" not in raw:
                console.print(f"[red]expected KEY=value, got {raw!r}[/red]")
                return 1
            k, _, v = raw.partition("=")
            k, v = k.strip(), v.strip()
            if not k:
                console.print(f"[red]empty key in {raw!r}[/red]")
                return 1
            pairs.append((k, v))
        try:
            vault = Vault.unlock()
        except VaultError as e:
            console.print(f"[red]{e}[/red]")
            return 1
        for k, v in pairs:
            vault.set(args.plugin, k, v)
        console.print(
            f"[green]✓[/green] stored {len(pairs)} secret(s) for "
            f"[cyan]{args.plugin}[/cyan]  [dim](backend={vault.backend})[/dim]"
        )
        return 0

    if action == "list":
        try:
            vault = Vault.unlock(create_if_missing=False)
        except VaultError as e:
            console.print(f"[red]{e}[/red]")
            return 1
        if args.plugin:
            keys = vault.list_keys(args.plugin)
            if not keys:
                console.print(f"[dim](no secrets stored for {args.plugin!r})[/dim]")
                return 0
            console.print(f"[bold]{args.plugin}[/bold]:")
            for k in keys:
                console.print(f"  · [cyan]{k}[/cyan]")
            return 0
        plugins = vault.list_plugins()
        if not plugins:
            console.print(
                "[dim](vault is empty — store one with [/dim]"
                "[cyan]xli auth set <plugin> KEY=value[/cyan][dim])[/dim]"
            )
            return 0
        console.print(f"[dim]vault: {VAULT_FILE} · backend={vault.backend}[/dim]")
        for pid in plugins:
            keys = vault.list_keys(pid)
            console.print(f"  [bold cyan]{pid}[/bold cyan]  [dim]({len(keys)} key(s): "
                          + ", ".join(keys) + ")[/dim]")
        return 0

    if action == "show":
        try:
            vault = Vault.unlock(create_if_missing=False)
        except VaultError as e:
            console.print(f"[red]{e}[/red]")
            return 1
        secrets = vault.get(args.plugin)
        if not secrets:
            console.print(f"[dim](no secrets stored for {args.plugin!r})[/dim]")
            return 0
        console.print(f"[bold]{args.plugin}[/bold]:")
        for k in sorted(secrets.keys()):
            v = secrets[k]
            if args.reveal:
                shown = v
            else:
                # Show length + last 4 chars so the user can spot-check rotation
                # without exposing full values to a shoulder-surfer.
                shown = f"[dim]({len(v)} chars, ends …{v[-4:]})[/dim]" if len(v) > 4 else "[dim](short)[/dim]"
            console.print(f"  [cyan]{k}[/cyan] = {shown}")
        if not args.reveal:
            console.print("[dim]use [/dim][cyan]--reveal[/cyan][dim] to print plaintext values[/dim]")
        return 0

    if action == "clear":
        try:
            vault = Vault.unlock(create_if_missing=False)
        except VaultError as e:
            console.print(f"[red]{e}[/red]")
            return 1
        if args.key:
            removed = vault.unset(args.plugin, args.key)
            if removed:
                console.print(f"[green]✓[/green] cleared {args.plugin}.{args.key}")
            else:
                console.print(f"[dim](nothing to clear for {args.plugin}.{args.key})[/dim]")
        else:
            removed = vault.unset(args.plugin)
            if removed:
                console.print(f"[green]✓[/green] cleared all secrets for {args.plugin}")
            else:
                console.print(f"[dim](no secrets stored for {args.plugin!r})[/dim]")
        return 0

    console.print("[red]unknown auth action[/red] — try set / list / show / clear")
    return 1
