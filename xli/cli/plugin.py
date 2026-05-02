"""`xli plugin` subcommand: manage plugins at ~/.config/xli/plugins/<id>.md.

Plugins are markdown files describing external APIs. Subscribe a plugin to a
project with `/lib subscribe <id>` from inside the REPL — the agent only sees
subscribed plugins via plugin_search.
"""

from __future__ import annotations

import argparse

from prompt_toolkit import prompt
from prompt_toolkit.completion import WordCompleter
from rich.console import Console

console = Console()


def cmd_plugin(args: argparse.Namespace) -> int:
    """Manage plugins at ~/.config/xli/plugins/<id>.md.

    Plugins are markdown files describing external APIs. Subscribe a plugin
    to a project with `/lib subscribe <id>` from inside the REPL — the agent
    only sees subscribed plugins via plugin_search.
    """
    from xli.plugin import (
        Plugin, PLUGINS_DIR, create_plugin, delete_plugin,
        install_stock_plugins, is_valid_id, list_plugins,
        open_in_editor as _open_in_editor,
    )

    if getattr(args, "install_stock", False):
        installed, skipped = install_stock_plugins(force=args.force)
        if installed:
            console.print(
                f"[green]✓[/green] installed {len(installed)} plugin(s) into "
                f"[cyan]{PLUGINS_DIR}[/cyan]:"
            )
            for pid in installed:
                console.print(f"  · [cyan]{pid}[/cyan]")
        if skipped:
            console.print(
                f"[dim]skipped {len(skipped)} (already exists; use [/dim]"
                "[cyan]--force[/cyan][dim] to overwrite): [/dim]"
                + ", ".join(f"[cyan]{p}[/cyan]" for p in skipped)
            )
        if not installed and not skipped:
            console.print("[dim](no stock plugins shipped in this build)[/dim]")
        else:
            console.print(
                "\n[dim]subscribe in any REPL with [/dim][cyan]/lib subscribe <id>[/cyan]"
                "[dim]; some plugins need [/dim][cyan]xli auth set <id> KEY=...[/cyan]"
                "[dim] before they can call.[/dim]"
            )
        return 0

    if args.list:
        plugins = list_plugins()
        if not plugins:
            console.print(
                "[dim](no plugins yet — create one with [/dim]"
                "[cyan]xli plugin --add <id>[/cyan][dim])[/dim]"
            )
            return 0
        console.print("[dim]id (use this with /lib subscribe) · risk · categories · description[/dim]")
        for p in plugins:
            try:
                meta = p.metadata()
            except OSError:
                meta = {}
            cats = ", ".join(meta.get("categories") or []) or "—"
            desc = meta.get("description") or "(no description)"
            risk = meta.get("risk", "low")
            risk_color = {"low": "green", "medium": "yellow", "high": "red"}.get(risk, "white")
            console.print(
                f"  [bold cyan]{p.id}[/bold cyan]  "
                f"[{risk_color}]{risk}[/{risk_color}]  "
                f"[dim]{cats}  ·  {desc}[/dim]"
            )
        return 0

    if args.show:
        p = Plugin(id=args.show)
        if not p.exists():
            console.print(f"[red]no such plugin: {args.show!r}[/red]")
            return 1
        console.print(p.read_raw())
        return 0

    if args.new:
        if not is_valid_id(args.new):
            console.print(f"[red]invalid plugin id: {args.new!r}[/red]")
            return 1
        p = Plugin(id=args.new)
        if p.exists():
            console.print(f"[yellow]plugin {args.new!r} already exists[/yellow] — use --edit instead")
            return 1
        create_plugin(args.new)
        console.print(f"[green]✓[/green] created plugin [bold]{args.new}[/bold] at {p.path}")
        console.print("[dim]opening $EDITOR — fill in the template, save, quit…[/dim]")
        _open_in_editor(p.path)
        console.print(
            "[dim]ready. From any project REPL, [/dim]"
            f"[cyan]/lib subscribe {args.new}[/cyan][dim] to make it available there.[/dim]"
        )
        return 0

    if args.edit:
        p = Plugin(id=args.edit)
        if not p.exists():
            console.print(f"[red]no such plugin: {args.edit!r}[/red]")
            return 1
        _open_in_editor(p.path)
        console.print("[dim]ready. Already-subscribed projects will use the new content on next turn.[/dim]")
        return 0

    if args.delete:
        p = Plugin(id=args.delete)
        if not p.exists():
            console.print(f"[red]no such plugin: {args.delete!r}[/red]")
            return 1
        if not args.yes:
            console.print(
                f"[yellow]about to delete plugin [bold]{args.delete}[/bold][/yellow]\n"
                f"  path: {p.path}"
            )
            try:
                ans = input("delete? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            if ans != "y":
                console.print("[dim]aborted[/dim]")
                return 1
        delete_plugin(args.delete)
        console.print(
            f"[green]✓[/green] deleted plugin {args.delete!r} "
            "[dim](existing project subscriptions become orphan; cleanup on next sub list)[/dim]"
        )
        return 0

    # No flag → list.
    return cmd_plugin(argparse.Namespace(
        list=True, new=None, add=None, edit=None, delete=None, show=None, yes=False,
        install_stock=False, force=False,
    ))


def cmd_plugin_add(plugin_id: str) -> int:
    """Interactively create a new plugin."""
    from xli.plugin import Plugin, create_plugin, is_valid_id

    if not is_valid_id(plugin_id):
        console.print(f"[red]invalid plugin id: {plugin_id!r}[/red]")
        return 1
    p = Plugin(id=plugin_id)
    if p.exists():
        console.print(f"[yellow]plugin {plugin_id!r} already exists[/yellow] — use --edit instead")
        return 1

    # Prompt for details
    name = prompt("Display name: ", default=plugin_id.title())
    description = prompt("One-line description: ")
    categories = prompt("Categories (comma-separated, e.g. misc,api): ", default="misc")
    risk_completer = WordCompleter(["low", "medium", "high"])
    risk = prompt("Risk level (low/medium/high): ", default="low", completer=risk_completer)
    auth_type = prompt("Auth type (none/query_param/header/bearer): ", default="none")
    auth_env_vars = []
    if auth_type != "none":
        env_vars = prompt("Auth env vars (comma-separated, e.g. API_KEY,SECRET): ")
        auth_env_vars = [v.strip() for v in env_vars.split(",") if v.strip()]

    # Generate frontmatter
    frontmatter = f"""---
id: {plugin_id}
name: {name}
description: {description}
categories: [{categories}]
risk: {risk}
auth_type: {auth_type}"""
    if auth_env_vars:
        frontmatter += f"\nauth_env_vars:\n" + "\n".join(f"  - {v}" for v in auth_env_vars)
    frontmatter += "\n---\n\n"

    # Body template
    body = f"""# {name}

{description}

## Auth setup

"""
    if auth_type == "none":
        body += "No authentication required.\n"
    else:
        body += f"""Store the key in the encrypted vault — the bash tool injects it into curl
calls automatically when this plugin is subscribed and the command references
the variable:

```bash
xli auth set {plugin_id} {' '.join(f'{v}=<your-{v.lower()}>' for v in auth_env_vars)}
```

## Usage

### Action 1 — short verb describing what this call does

```bash
curl "https://api.example.com/v1/endpoint?param={{PARAM}}"
```
"""
        if auth_env_vars:
            body += f""" \\
  -H "Authorization: Bearer ${{{auth_env_vars[0]}}}" """
        body += """

Parameters:
- `PARAM`: <what the agent should fill in here>

## Response shape

<JSON / XML / etc., briefly. Link to upstream docs for full schema.>

## Cost / rate limits

<Free tier: N requests/min. Paid: $X/M calls. Etc.>
"""

    content = frontmatter + body
    create_plugin(plugin_id, content=content)
    console.print(f"[green]✓[/green] created plugin [bold]{plugin_id}[/bold] at {p.path}")
    console.print(
        "[dim]ready. From any project REPL, [/dim]"
        f"[cyan]/lib subscribe {plugin_id}[/cyan][dim] to make it available there.[/dim]"
    )
    return 0
