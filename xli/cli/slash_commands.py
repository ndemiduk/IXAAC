"""REPL slash-command handlers: /ref, /unref, /doc, /undoc, /lib.

Each `_handle_X_command(user_input, agent, project)` returns True if it
consumed the input, False if the caller should fall through to the next
dispatcher. Imported and called from both REPL loops in repl.py.
"""

from __future__ import annotations

from rich.console import Console

from xli.persona import Persona, is_valid_name, list_personas
import shlex

from .attachments import save_attached_docs, save_attached_refs

console = Console()


def _handle_ref_command(user_input: str, agent, project) -> bool:
    """Handle `/ref` and `/unref` slash commands.

    `/ref`            — list currently-attached personas (in-session state)
    `/ref <name>`     — attach <name>'s collection to search_project
    `/unref <name>`   — detach

    Returns True if the input was handled, False otherwise (caller falls
    through to the next slash dispatcher).
    """
    if not (user_input == "/ref" or user_input.startswith("/ref ") or
            user_input == "/unref" or user_input.startswith("/unref ")):
        return False

    parts = user_input.split(maxsplit=1)
    cmd = parts[0]

    if cmd == "/ref":
        if len(parts) == 1:
            # List current attachments
            if not agent.attached_refs:
                console.print("[dim](no refs attached this session)[/dim]")
                console.print("[dim]usage: [/dim][cyan]/ref <persona>[/cyan]"
                              "[dim] to attach a persona's memory[/dim]")
            else:
                console.print("[bold]attached refs:[/bold]")
                for name, cid in agent.attached_refs:
                    short = cid[:24] + "…" if len(cid) > 24 else cid
                    console.print(f"  · [cyan]{name}[/cyan]  [dim]{short}[/dim]")
            return True

        name = parts[1].strip()
        if not is_valid_name(name):
            existing = list_personas()
            console.print(
                f"[red]invalid persona name: {name!r}[/red]  "
                "[dim](names are single tokens — letters, digits, _ . - only)[/dim]"
            )
            if existing:
                console.print(
                    "[dim]available personas: [/dim]"
                    + ", ".join(f"[cyan]{p.name}[/cyan]" for p in existing)
                )
            return True
        persona = Persona(name)
        if not persona.exists():
            from difflib import get_close_matches
            existing = list_personas()
            existing_names = [p.name for p in existing]
            close = get_close_matches(name, existing_names, n=3, cutoff=0.4)
            console.print(f"[red]no such persona: {name!r}[/red]")
            if close:
                console.print(
                    "[dim]did you mean: [/dim]"
                    + ", ".join(f"[cyan]{c}[/cyan]" for c in close) + "?"
                )
            elif existing_names:
                console.print(
                    "[dim]available personas: [/dim]"
                    + ", ".join(f"[cyan]{n}[/cyan]" for n in existing_names)
                )
            else:
                console.print(
                    f"[dim](create one with [/dim][cyan]xli chat --new {name}[/cyan][dim])[/dim]"
                )
            return True
        cid = persona.collection_id()
        if not cid:
            console.print(
                f"[yellow]persona {name!r} has no Collection yet[/yellow] — "
                f"run [cyan]xli chat {name}[/cyan] once to initialize it, then try again."
            )
            return True
        if any(n == name for n, _ in agent.attached_refs):
            console.print(f"[dim](already attached: {name})[/dim]")
            return True
        agent.attached_refs.append((name, cid))
        save_attached_refs(project, agent)
        console.print(
            f"[green]✓[/green] attached [cyan]{name}[/cyan]'s memory — "
            "[dim]search_project will now include their conversation history[/dim]"
        )
        return True

    if cmd == "/unref":
        if len(parts) != 2:
            console.print("[dim]usage: [/dim][cyan]/unref <persona>[/cyan]")
            return True
        name = parts[1].strip()
        before = len(agent.attached_refs)
        agent.attached_refs = [(n, c) for n, c in agent.attached_refs if n != name]
        if len(agent.attached_refs) == before:
            console.print(f"[dim]no ref attached named {name!r}[/dim]")
        else:
            console.print(f"[green]✓[/green] detached [cyan]{name}[/cyan]")
            save_attached_refs(project, agent)
        return True

    return False


def _handle_lib_command(user_input: str, project) -> bool:
    """Handle `/lib` slash — manage plugin subscriptions for this project.

    /lib                    list subscribed plugins for this project
    /lib all                list every installed plugin (catalog)
    /lib subscribe <id>     subscribe to a plugin
    /lib unsubscribe <id>   unsubscribe
    /lib remove <id>        delete a plugin entirely (catalog-wide)

    Subscriptions persist at <project>/.xli/plugins.txt.
    """
    if not (user_input == "/lib" or user_input.startswith("/lib ")):
        return False

    from xli.plugin import (
        Plugin, add_subscription, delete_plugin, is_valid_id,
        list_plugins, load_subscriptions, remove_subscription,
    )

    parts = user_input.split(maxsplit=2)
    sub = parts[1] if len(parts) >= 2 else None

    if sub is None:
        # /lib  → list subscribed
        subs = load_subscriptions(project.xli_dir)
        if not subs:
            console.print(
                "[dim](no plugins subscribed for this project)[/dim]\n"
                "[dim]usage: [/dim][cyan]/lib all[/cyan][dim] to browse, "
                "[/dim][cyan]/lib subscribe <id>[/cyan][dim] to add[/dim]"
            )
            return True
        console.print(f"[bold]subscribed plugins ({len(subs)}):[/bold]")
        for pid in subs:
            p = Plugin(id=pid)
            if p.exists():
                console.print(f"  · [cyan]{pid}[/cyan]  [dim]{p.description() or '(no description)'}[/dim]")
            else:
                console.print(f"  · [yellow]{pid}[/yellow]  [red](orphan — file missing)[/red]")
        return True

    if sub == "all":
        plugins = list_plugins()
        subs = set(load_subscriptions(project.xli_dir))
        if not plugins:
            console.print("[dim](no plugins installed yet — [/dim]"
                          "[cyan]xli plugin --new <id>[/cyan][dim])[/dim]")
            return True
        console.print(f"[bold]all installed plugins ({len(plugins)}):[/bold]  "
                      "[dim]● = subscribed in this project[/dim]")
        for p in plugins:
            mark = "[bold green]●[/bold green]" if p.id in subs else " "
            risk = p.risk()
            risk_color = {"low": "green", "medium": "yellow", "high": "red"}.get(risk, "white")
            console.print(
                f"  {mark} [cyan]{p.id}[/cyan]  "
                f"[{risk_color}]{risk}[/{risk_color}]  "
                f"[dim]{p.description() or '(no description)'}[/dim]"
            )
        return True

    if sub == "subscribe":
        if len(parts) < 3:
            console.print("[dim]usage: [/dim][cyan]/lib subscribe <id>[/cyan]")
            return True
        pid = parts[2].strip()
        if not is_valid_id(pid):
            console.print(f"[red]invalid plugin id: {pid!r}[/red]")
            return True
        p = Plugin(id=pid)
        if not p.exists():
            console.print(
                f"[red]no such plugin: {pid!r}[/red]  "
                f"[dim](create with [/dim][cyan]xli plugin --new {pid}[/cyan][dim])[/dim]"
            )
            return True
        added = add_subscription(project.xli_dir, pid)
        if added:
            console.print(
                f"[green]✓[/green] subscribed to [cyan]{pid}[/cyan] "
                f"[dim](risk={p.risk()})[/dim]"
            )
        else:
            console.print(f"[dim](already subscribed: {pid})[/dim]")
        return True

    if sub == "unsubscribe":
        if len(parts) < 3:
            console.print("[dim]usage: [/dim][cyan]/lib unsubscribe <id>[/cyan]")
            return True
        pid = parts[2].strip()
        removed = remove_subscription(project.xli_dir, pid)
        if removed:
            console.print(f"[green]✓[/green] unsubscribed [cyan]{pid}[/cyan]")
        else:
            console.print(f"[dim]not subscribed: {pid!r}[/dim]")
        return True

    if sub == "remove":
        if len(parts) < 3:
            console.print("[dim]usage: [/dim][cyan]/lib remove <id>[/cyan]")
            return True
        pid = parts[2].strip()
        p = Plugin(id=pid)
        if not p.exists():
            console.print(f"[red]no such plugin: {pid!r}[/red]")
            return True
        # Also unsubscribe from this project, since the file's about to vanish.
        remove_subscription(project.xli_dir, pid)
        delete_plugin(pid)
        console.print(
            f"[green]✓[/green] removed plugin [cyan]{pid}[/cyan] "
            "[dim](other projects' subscriptions become orphan — cleaned on next /lib list)[/dim]"
        )
        return True

    console.print(f"[red]unknown /lib subcommand: {sub!r}[/red]")
    console.print("[dim]try: [/dim][cyan]/lib[/cyan][dim], [/dim][cyan]/lib all[/cyan][dim], "
                  "[/dim][cyan]/lib subscribe <id>[/cyan][dim], [/dim]"
                  "[cyan]/lib unsubscribe <id>[/cyan][dim], [/dim][cyan]/lib remove <id>[/cyan]")
    return True


def _handle_doc_command(user_input: str, agent, project) -> bool:
    """Handle `/doc` and `/undoc` slash commands.

    `/doc`            — list currently-attached docs (in-session state)
    `/doc <name>`     — attach <name>'s content to the system prompt
    `/undoc <name>`   — detach

    Mirrors `_handle_ref_command` shape but operates on agent.attached_docs
    (which feeds the system prompt) instead of attached_refs (which feeds
    search_project's collection list).
    """
    from xli.doc import Doc, INLINE_SOFT_CAP_BYTES, is_valid_name as _is_valid_doc_name

    if not (user_input == "/doc" or user_input.startswith("/doc ") or
            user_input == "/undoc" or user_input.startswith("/undoc ")):
        return False

    parts = user_input.split(maxsplit=1)
    cmd = parts[0]

    if cmd == "/doc":
        if len(parts) == 1:
            if not agent.attached_docs:
                console.print("[dim](no docs attached this session)[/dim]")
                console.print("[dim]usage: [/dim][cyan]/doc <name>[/cyan]"
                              "[dim] to attach a reference doc[/dim]")
            else:
                console.print("[bold]attached docs:[/bold]")
                for name, content in agent.attached_docs:
                    size = len(content)
                    console.print(f"  · [cyan]{name}[/cyan]  [dim]{size:,} bytes[/dim]")
            return True

        name = parts[1].strip()
        if not _is_valid_doc_name(name):
            from xli.doc import list_docs as _list_docs
            existing = _list_docs()
            console.print(
                f"[red]invalid doc name: {name!r}[/red]  "
                "[dim](names are single tokens — letters, digits, _ . - only)[/dim]"
            )
            if existing:
                console.print(
                    "[dim]available docs: [/dim]"
                    + ", ".join(f"[cyan]{d.name}[/cyan]" for d in existing)
                )
            return True
        d = Doc(name)
        if not d.exists():
            from xli.doc import list_docs as _list_docs
            from difflib import get_close_matches
            existing = _list_docs()
            existing_names = [doc.name for doc in existing]
            close = get_close_matches(name, existing_names, n=3, cutoff=0.4)
            msg_lines = [f"[red]no such doc: {name!r}[/red]"]
            if close:
                msg_lines.append(
                    "[dim]did you mean: [/dim]"
                    + ", ".join(f"[cyan]{c}[/cyan]" for c in close) + "?"
                )
            elif existing_names:
                msg_lines.append(
                    "[dim]available docs: [/dim]"
                    + ", ".join(f"[cyan]{n}[/cyan]" for n in existing_names)
                )
            else:
                msg_lines.append(
                    "[dim](no docs exist yet — create with [/dim]"
                    f"[cyan]xli doc --new {name}[/cyan][dim])[/dim]"
                )
            for line in msg_lines:
                console.print(line)
            return True
        if any(n == name for n, _ in agent.attached_docs):
            console.print(f"[dim](already attached: {name})[/dim]")
            return True
        try:
            content = d.read()
        except OSError as e:
            console.print(f"[red]read failed: {e}[/red]")
            return True
        size = len(content)
        agent.attached_docs.append((name, content))
        save_attached_docs(project, agent)
        warn = ""
        if size > INLINE_SOFT_CAP_BYTES:
            warn = (
                f"  [yellow]⚠ {size:,} bytes is large for inline mode — "
                "consider a persona + /ref for very long reference material[/yellow]"
            )
        console.print(
            f"[green]✓[/green] attached doc [cyan]{name}[/cyan] "
            f"[dim]({size:,} bytes inlined into system prompt)[/dim]"
            + (f"\n{warn}" if warn else "")
        )
        return True

    if cmd == "/undoc":
        if len(parts) != 2:
            console.print("[dim]usage: [/dim][cyan]/undoc <name>[/cyan]")
            return True
        name = parts[1].strip()
        before = len(agent.attached_docs)
        agent.attached_docs = [(n, c) for n, c in agent.attached_docs if n != name]
        if len(agent.attached_docs) == before:
            console.print(f"[dim]no doc attached named {name!r}[/dim]")
        else:
            console.print(f"[green]✓[/green] detached [cyan]{name}[/cyan]")
            save_attached_docs(project, agent)
        return True

    return False


_2NDEYE_USAGE = (
    "[dim]usage: /2ndeye [scope] [--with-tools] [--from-debug | --from PATH] <question>[/dim]\n"
    "[dim]  scope: (none, default = no history) | --turns | --last N | --since MARK | --full[/dim]\n"
    "[dim]  --with-tools           include tool calls/results in sent history (default: stripped)[/dim]\n"
    "[dim]  --from-debug           prepend latest .xli/debug-last.md as context[/dim]\n"
    "[dim]  --from PATH            prepend the contents of any file as context[/dim]\n"
    "[dim]  (configure secondary_ai in ~/.config/xli/config.json first)[/dim]"
)


def _filter_history_for_secondary(history: list) -> list:
    """Strip tool_calls, tool-result messages, and pure-tool-call assistant
    messages from history before sending to a secondary AI.

    The secondary provider doesn't share iXaac's tool palette — tool metadata
    is noise that adds tokens without informing the critique. Keeps user +
    assistant text and system messages only. 5-10× reduction on long sessions.
    """
    filtered = []
    for msg in history:
        role = msg.get("role")
        if role == "tool":
            continue
        if role == "assistant":
            content = msg.get("content")
            if not content:
                continue
            filtered.append({"role": "assistant", "content": content})
        elif role in ("user", "system"):
            content = msg.get("content")
            if content:
                filtered.append({"role": role, "content": content})
    return filtered


def _handle_2ndeye_command(user_input: str, agent, project) -> bool:
    """Handle `/2ndeye` — user-invoked cross-vendor critique.

    Default scope is empty (no history) so cheap general-purpose questions
    don't ship the entire conversation to a second vendor every call.
    Opt into history with --turns, --last N, --since MARK, or --full.
    """
    if not user_input.startswith("/2ndeye"):
        return False

    try:
        parts = shlex.split(user_input)
    except ValueError:
        parts = user_input.split()

    if len(parts) < 2 or parts[0] != "/2ndeye":
        return False

    last_n = None
    since_mark = None
    use_full = False
    use_turns = False
    with_tools = False
    from_debug = False
    from_path: str | None = None
    q_parts: list[str] = []
    i = 1
    while i < len(parts):
        tok = parts[i]
        if tok == "--last" and i + 1 < len(parts):
            try:
                last_n = int(parts[i + 1])
                i += 2
                continue
            except ValueError:
                pass
        elif tok.startswith("--last="):
            try:
                last_n = int(tok.split("=", 1)[1])
                i += 1
                continue
            except ValueError:
                pass
        elif tok == "--since" and i + 1 < len(parts):
            since_mark = parts[i + 1]
            i += 2
            continue
        elif tok == "--from" and i + 1 < len(parts):
            from_path = parts[i + 1]
            i += 2
            continue
        elif tok == "--full":
            use_full = True
            i += 1
            continue
        elif tok == "--turns":
            use_turns = True
            i += 1
            continue
        elif tok == "--with-tools":
            with_tools = True
            i += 1
            continue
        elif tok == "--from-debug":
            from_debug = True
            i += 1
            continue
        else:
            q_parts.append(tok)
            i += 1

    question = " ".join(q_parts).strip()
    if not question:
        console.print(_2NDEYE_USAGE)
        return True

    if from_debug and from_path:
        console.print("[red]2ndeye: --from-debug and --from are mutually exclusive[/red]")
        return True

    # Build history slice. NEW DEFAULT: no history. Opt-in via flag.
    raw_history = getattr(agent, "history", []) or []
    history: list = []
    if use_full:
        history = list(raw_history)
    elif last_n is not None and last_n > 0:
        history = raw_history[-last_n * 2:]
    elif use_turns:
        history = raw_history[-2:]
    elif since_mark:
        console.print(
            "[yellow]2ndeye: --since support deferred (waiting on /mark); falling back to --full[/yellow]"
        )
        history = list(raw_history)

    if history and not with_tools:
        history = _filter_history_for_secondary(history)

    # Build context preamble from --from-debug or --from PATH.
    preamble = ""
    if from_debug:
        debug_path = project.xli_dir / "debug-last.md"
        if not debug_path.exists():
            console.print(
                "[red]2ndeye: no /debug output saved yet — run /debug first.[/red]"
            )
            return True
        try:
            debug_body = debug_path.read_text()
        except OSError as e:
            console.print(f"[red]2ndeye: cannot read {debug_path}: {e}[/red]")
            return True
        preamble = (
            f"--- /debug output ---\n{debug_body}\n--- end /debug output ---\n\n"
        )
    elif from_path:
        from pathlib import Path
        p = Path(from_path).expanduser()
        if not p.exists():
            console.print(f"[red]2ndeye: file not found: {from_path}[/red]")
            return True
        try:
            file_body = p.read_text()
        except OSError as e:
            console.print(f"[red]2ndeye: cannot read {from_path}: {e}[/red]")
            return True
        preamble = f"--- {p} ---\n{file_body}\n--- end {p} ---\n\n"

    full_question = preamble + question

    # Build a short scope summary for the spinner header / debugging.
    scope_label = (
        "--full" if use_full
        else f"--last {last_n}" if last_n
        else "--turns" if use_turns
        else "--since" if since_mark
        else "no history"
    )
    if from_debug:
        scope_label += " + debug"
    elif from_path:
        scope_label += f" + {from_path}"

    try:
        from xli.secondary_ai import query
        response = query(history, full_question, scope=scope_label)
    except Exception as exc:
        console.print(f"[red]2ndeye: {exc}[/red]")
        return True

    from xli.config import GlobalConfig
    cfg = GlobalConfig.load()
    sec = getattr(cfg, "secondary_ai", {}) or {}
    model = sec.get("model", "secondary")
    console.print(f"\n[2ndeye · {model}]")
    console.print(response)
    console.print()
    return True
