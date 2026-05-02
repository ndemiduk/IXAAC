"""Headless / non-interactive subcommands.

- `xli ask <prompt>` — one-shot agent run (used by the XMPP daemon's "agent
  fallback" dispatch when an inbound message doesn't match a verb).
- `xli daemon --xmpp` — start the XMPP listener daemon. Re-execs through the
  dedicated OMEMO venv since slixmpp-omemo can't share iXaac's own venv.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console

from xli.agent import Agent
from xli.client import MissingCredentials
from xli.config import GlobalConfig, ProjectConfig
from xli.pool import ClientPool
from xli.sync import sync_project
from xli import workspaces as ws_mod

console = Console()


def cmd_ask(args: argparse.Namespace) -> int:
    """One-shot agent run for non-interactive callers (e.g. the XMPP daemon).

    Resolves --workspace (alias or path) to a registered ProjectConfig, runs a
    single Agent.run_turn against the given prompt, and prints the reply to
    stdout. Used by `xli daemon` for the "agent fallback" dispatch when an
    incoming XMPP message doesn't match a verb.
    """
    cfg = GlobalConfig.load()
    try:
        pool = ClientPool.from_config(cfg)
    except MissingCredentials as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    workspace_arg = (args.workspace or "").strip()
    if workspace_arg:
        ws = ws_mod.Workspaces.load()
        entry = ws.find(workspace_arg)
        path = Path(entry.path) if entry else Path(workspace_arg).expanduser().resolve()
    else:
        ws = ws_mod.Workspaces.load()
        most_recent = ws.most_recent_project()
        if not most_recent:
            print(
                "error: no project workspaces registered; pass --workspace explicitly",
                file=sys.stderr,
            )
            return 1
        path = Path(most_recent.path)

    project = ProjectConfig.load(path)
    if not project:
        print(
            f"error: not an xli project: {path}\n"
            f"hint: cd into it and run `xli init` first",
            file=sys.stderr,
        )
        return 1

    if not project.local_only:
        try:
            sync_project(pool.primary(), project, cfg)
        except Exception as e:
            print(f"warning: pre-run sync failed: {e}", file=sys.stderr)

    # `xli ask` is non-interactive (driven by the XMPP daemon's agent fallback,
    # plus any other headless caller). Pass console=None so the bash-tool gate
    # refuses risky intents cleanly with a tool error — instead of trying to
    # call input() on a TTY that doesn't exist and blocking until timeout.
    # Agents in this path also can't be given yolo: there's no human watching
    # to revoke an "rm -rf" if the model decides it's fine.
    agent = Agent(pool=pool, project=project, cfg=cfg, console=None, yolo=False)
    try:
        reply, modified, _stats = agent.run_turn(args.prompt)
    except Exception as e:
        print(f"error: agent run failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    # Push any file edits the agent made to the Collection so the project's
    # remote state stays consistent. The REPL syncs at end of every turn; ask
    # was previously dropping `modified` on the floor, leaving Collection stale
    # after daemon-initiated edits.
    if modified and not project.local_only:
        try:
            sync_project(pool.primary(), project, cfg)
        except Exception as e:
            print(f"warning: post-run sync failed: {e}", file=sys.stderr)

    print(reply)
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    """Start the XMPP command daemon (Phase 2 inbound listener).

    The daemon needs slixmpp + slixmpp-omemo, which live in the dedicated
    OMEMO venv at ~/.config/xli/bin/venv/ (see plugin doc xmpp_send.md).
    Re-exec through that venv's Python so the daemon module's imports
    succeed without polluting iXaac's own venv.
    """
    import importlib.util
    import os as _os

    if not args.xmpp:
        console.print("[red]--xmpp is currently the only supported transport[/red]")
        return 1

    omemo_python = Path.home() / ".config" / "xli" / "bin" / "venv" / "bin" / "python3"
    if not omemo_python.exists():
        console.print(
            f"[red]OMEMO venv not found at {omemo_python}[/red]\n"
            "[dim]see ~/.config/xli/plugins/xmpp_send.md → 'Sender install' for setup[/dim]"
        )
        return 1

    # Locate xli/daemon.py without actually importing it — its top-level
    # `import omemo` only resolves inside the OMEMO venv we're about to exec into.
    spec = importlib.util.find_spec("xli.daemon")
    if not spec or not spec.origin:
        console.print("[red]could not locate xli.daemon module[/red]")
        return 1
    daemon_script = Path(spec.origin)
    if not daemon_script.exists():
        console.print(f"[red]daemon module missing at {daemon_script}[/red]")
        return 1

    config_path = (
        Path(args.config).expanduser()
        if args.config
        else Path.home() / ".config" / "xli" / "daemon.toml"
    )

    argv = [str(omemo_python), str(daemon_script), str(config_path)]
    # execv replaces this process so the user sees the daemon's logs directly;
    # Ctrl-C drops them straight into the daemon's signal handling.
    _os.execv(argv[0], argv)
    # unreachable
    return 0
