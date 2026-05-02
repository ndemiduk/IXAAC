"""`xli doc` subcommand: manage reference docs at ~/.config/xli/docs/<name>.md.

Docs are markdown files inlined into the agent's system prompt when attached
via `/doc <name>` in either REPL.
"""

from __future__ import annotations

import argparse

from rich.console import Console

console = Console()


def cmd_doc(args: argparse.Namespace) -> int:
    """Manage reference docs at ~/.config/xli/docs/<name>.md.

    Sub-routes off the action flags. Docs are markdown files inlined into
    the agent's system prompt when attached via /doc <name> in either REPL.
    """
    from xli.doc import (
        Doc, create_doc, delete_doc, is_valid_name as _is_valid,
        list_docs, open_in_editor as _open_in_editor,
    )

    if args.list:
        docs = list_docs()
        if not docs:
            console.print(
                f"[dim](no docs yet — create one with [/dim][cyan]xli doc --new <name>[/cyan][dim])[/dim]"
            )
            return 0
        console.print("[dim]name (use this with /doc) · size · first line[/dim]")
        for d in docs:
            console.print(
                f"  [bold cyan]{d.name}[/bold cyan]"
                f"  [dim]·  {d.size_bytes():,}b  ·  \"{d.first_line()}\"[/dim]"
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
