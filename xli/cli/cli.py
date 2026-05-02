"""XLI CLI entry point: parse + dispatch + workspace touch.

Argparse setup, HELP_TEXT, and the trivial cmd_help/cmd_config live in parser.py.
Subcommand implementations live in sibling modules (ask.py, auth.py, bootstrap.py,
doc.py, keys.py, models.py, plugin.py, project.py, repl.py, setup.py, status.py).
"""

from __future__ import annotations

from pathlib import Path

from xli import workspaces as ws_mod

from .parser import build_parser

# Commands that don't represent "work in this directory" and therefore shouldn't
# bump the workspace's last_active timestamp on each invocation.
_STATELESS_COMMANDS = frozenset({
    "help", "config", "setup", "models", "keys", "bootstrap",
    "gc", "workspaces", "ask", "daemon",
})


def main() -> int:
    args = build_parser().parse_args()

    # Auto-touch the cwd as a workspace, so the registry keeps an honest
    # last_active timeline for every dir xli has run in. Skip stateless
    # commands and the workspaces command itself (`xli workspaces` is a
    # registry inspection, not a workspace activity).
    if args.command not in _STATELESS_COMMANDS:
        try:
            ws_mod.touch(Path.cwd())
        except Exception:
            pass  # touching is best-effort; never block the actual command

    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
