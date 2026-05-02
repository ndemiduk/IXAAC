"""XLI CLI: argparse wiring + small command shims (init/scratch/projects/config/new/help).

Subcommand implementations live in sibling modules (auth.py, bootstrap.py, doc.py,
plugin.py, project.py, repl.py, etc.). This file is now mostly the argparse build
in main() plus the few commands too small to warrant their own file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console

from xli import __version__
from xli.bootstrap import DEFAULT_EXPIRE_DAYS
from xli.config import GlobalConfig, ProjectConfig
from xli.registry import Registry
from xli.sync import init_project

from .ask import cmd_ask, cmd_daemon
from .auth import cmd_auth
from .bootstrap import cmd_bootstrap
from .doc import cmd_doc
from .keys import cmd_keys
from .models import cmd_models
from .plugin import cmd_plugin
from .project import cmd_gc, cmd_init, cmd_sync, cmd_workspaces
from .repl import cmd_chat, cmd_code
from .setup import cmd_setup
from .status import cmd_status

console = Console()


HELP_TEXT = """xli — Grok + xAI Collections coding agent.

PROJECT (code work — files, edits, builds)
  init [NAME]              Initialize an xli project in the current directory.
                           --local: no Collection upload (Midnight Commander mode).
                           --snapshot: cache .xli/index.txt for fast structural search.
  new NAME                 Create a new project directory and initialize it.
  scratch [NAME]           Ephemeral local-only project under ~/.xli/scratch/, then chat.
  projects [FILTER]        List all registered xli projects.
  status [PATH]            Show config + project state.
  sync [PATH]              Push local changes to the project's collection.
  code [TARGET]            Start the project-scoped code REPL (was `chat` pre-rename).

CHAT (conversation with persistent memory)
  chat [NAME]              Start a chat as persona NAME (default: most-recently-used).
  chat --new NAME          Create a new persona; opens $EDITOR on its prompt.
  chat --list              List personas.
  chat --edit NAME         Edit a persona's prompt in $EDITOR.
  chat --delete NAME       Delete a persona (prompt + state dir).

KNOWLEDGE (attached in any REPL via /doc <name>)
  doc --new NAME           Create a new reference doc; opens $EDITOR.
  doc --list               List all docs.
  doc --edit NAME          Edit a doc in $EDITOR.
  doc --delete NAME        Delete a doc.

PLUGINS (subscribe in REPL via /lib subscribe; invoke via /get <intent>)
  plugin --new ID          Create a new plugin from template; opens $EDITOR.
  plugin --list            List all installed plugins.
  plugin --show ID         Print a plugin's full markdown.
  plugin --edit ID         Edit a plugin in $EDITOR.
  plugin --delete ID       Delete a plugin.

PLUGIN CREDENTIALS (encrypted vault at ~/.config/xli/vault.enc)
  auth set ID KEY=value    Store secret(s) for a plugin (auto-init on first call).
  auth list [ID]           List plugins with secrets (or keys for one plugin).
  auth show ID [--reveal]  Show stored keys (values redacted unless --reveal).
  auth clear ID [KEY]      Remove a key (or every key for the plugin).

SETUP
  config                   Write a config template to ~/.config/xli/config.json.
  setup                    First-time setup: config + provision primary + worker keys.
  bootstrap                Lower-level: provision worker keys via management API.

MODELS
  models list              List models the team has access to.
  models recommended       Show heuristic best-of-class picks.
  models set [--orchestrator MODEL] [--worker MODEL]

KEYS
  keys list                List local chat keys with server-side expiration.
  keys rotate [--label X]  Rotate the secret of one or all keys.
  keys expire --days N     Update expireTime on existing key(s).
  keys revoke [--prefix X] Delete keys by label prefix (server + local).

MAINTENANCE
  gc [--dry-run]           Find and delete orphan xAI collections.
  help                     Show this message.

For per-command flags:  xli <cmd> --help
For version:            xli --version
"""


def cmd_help(args: argparse.Namespace) -> int:
    print(HELP_TEXT)
    return 0


def cmd_scratch(args: argparse.Namespace) -> int:
    """Create an ephemeral local-only project under ~/.xli/scratch/<name>/ and drop into chat.

    Use for: one-off file-management tasks ("rename these", "find duplicates"),
    quick experiments, anything you don't want to upload as a project.
    For snapshotting an existing big directory (NAS, media collection), run
    `xli init --local --snapshot` *in that directory* instead — scratch creates
    a fresh empty dir.
    """
    from datetime import datetime
    name = args.name or datetime.now().strftime("%Y%m%d-%H%M%S")
    scratch_root = (Path.home() / ".xli" / "scratch" / name).resolve()
    scratch_root.mkdir(parents=True, exist_ok=True)

    existing = ProjectConfig.load(scratch_root)
    if existing and not args.force:
        console.print(f"[yellow]scratch project already exists at[/yellow] {scratch_root}")
    else:
        project = init_project(
            None,
            scratch_root,
            name=f"scratch/{name}",
            local_only=True,
            snapshot=False,  # empty dir; nothing to snapshot
        )
        console.print(
            f"[green]✓[/green] scratch project [bold]{project.name}[/bold] at {scratch_root}"
        )

    if args.no_chat:
        return 0
    return cmd_code(argparse.Namespace(target=str(scratch_root), yolo=args.yolo))


def cmd_projects(args: argparse.Namespace) -> int:
    registry = Registry.load()
    entries = registry.entries
    flt = (args.filter or "").lower()
    if flt:
        entries = [
            e for e in entries
            if flt in e.name.lower() or flt in e.path.lower()
        ]
    if not entries:
        msg = (
            f"no projects matching {args.filter!r}" if flt
            else "no registered projects yet — run `xli init` in a project dir"
        )
        console.print(f"[yellow]{msg}[/yellow]")
        return 0
    console.print("[bold]registered xli projects:[/bold]")
    for entry in sorted(entries, key=lambda e: e.name.lower()):
        alive = (
            Path(entry.path).is_dir()
            and (Path(entry.path) / ".xli" / "project.json").exists()
        )
        marker = "[green]●[/green]" if alive else "[red]✗[/red]"
        console.print(
            f"  {marker} [bold]{entry.name:<24}[/bold]  {entry.path:<50}  [dim]{entry.collection_id}[/dim]"
        )
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    path = GlobalConfig.write_template()
    console.print(f"[green]✓[/green] config at {path}")
    console.print("[dim]edit it: paste your management_api_key + add api keys to the keys[] list[/dim]")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    """Create a new project directory and initialize it."""
    name = args.name
    base = Path(args.path or ".").resolve()
    project_root = base / name
    if project_root.exists():
        console.print(f"[red]already exists: {project_root}[/red]")
        return 1
    project_root.mkdir(parents=True)
    console.print(f"[green]✓[/green] created {project_root}")
    return cmd_init(
        argparse.Namespace(
            path=str(project_root),
            name=name,
            collection_id=None,
            sync=True,
            force=False,
        )
    )


def main() -> int:
    p = argparse.ArgumentParser(prog="xli", description="Grok + xAI Collections coding agent.")
    p.add_argument("--version", action="version", version=f"xli {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Initialize an xli project. Positional NAME labels the collection.")
    p_init.add_argument("name", nargs="?", help="Project name (default: cwd basename)")
    p_init.add_argument("--path", help="Project directory (default: cwd)")
    p_init.add_argument("--collection-id", help="Reuse an existing collection instead of creating one")
    p_init.add_argument("--no-sync", dest="sync", action="store_false", help="Skip the initial sync")
    p_init.add_argument("--force", action="store_true", help="Reinitialize even if project exists")
    p_init.add_argument("--local", action="store_true",
                        help="Local-only mode: no Collection, no upload, no sync. search_project disabled.")
    p_init.add_argument("--snapshot", action="store_true",
                        help="Cache a paths+sizes index at .xli/index.txt for fast structural search.")
    p_init.set_defaults(func=cmd_init, sync=True)

    p_new = sub.add_parser("new", help="Create a new project directory and initialize it.")
    p_new.add_argument("name", help="Project name (also the new directory name)")
    p_new.add_argument("--path", help="Parent directory (default: cwd)")
    p_new.set_defaults(func=cmd_new)

    p_projects = sub.add_parser("projects", help="List all registered xli projects (filter by substring).")
    p_projects.add_argument("filter", nargs="?", help="Optional substring filter (matches name or path)")
    p_projects.set_defaults(func=cmd_projects)

    p_ws = sub.add_parser(
        "workspaces",
        help="List/manage workspaces (auto-tracked directories + explicit references).",
    )
    p_ws.set_defaults(func=cmd_workspaces, ws_action=None)
    ws_sub = p_ws.add_subparsers(dest="ws_action")

    ws_list = ws_sub.add_parser("list", help="List workspaces, sorted by last_active (default action).")
    ws_list.add_argument("--projects", dest="projects_only", action="store_true",
                         help="Show only kind=project")
    ws_list.add_argument("--snapshots", dest="snapshots_only", action="store_true",
                         help="Show only kind=snapshot")
    ws_list.set_defaults(func=cmd_workspaces, ws_action="list")

    ws_add = ws_sub.add_parser("add", help="Register a directory as a workspace.")
    ws_add.add_argument("path", help="Directory path to register")
    ws_add.add_argument("--snapshot", action="store_true",
                        help="Mark as snapshot (read-only reference, excluded from default rotation)")
    ws_add.add_argument("--alias", help="Optional short alias for daemon dispatch (e.g. ixaac, isaac2)")
    ws_add.add_argument("--notes", help="Optional notes")
    ws_add.set_defaults(func=cmd_workspaces, ws_action="add")

    ws_snap = ws_sub.add_parser("snapshot", help="Mark a workspace as snapshot.")
    ws_snap.add_argument("key", help="Path or alias")
    ws_snap.set_defaults(func=cmd_workspaces, ws_action="snapshot")

    ws_proj = ws_sub.add_parser("project", help="Mark a workspace as project (active).")
    ws_proj.add_argument("key", help="Path or alias")
    ws_proj.set_defaults(func=cmd_workspaces, ws_action="project")

    ws_alias = ws_sub.add_parser("alias", help="Set or clear a workspace alias.")
    ws_alias.add_argument("key", help="Path or current alias")
    ws_alias.add_argument("alias", nargs="?", help="New alias (omit to clear)")
    ws_alias.set_defaults(func=cmd_workspaces, ws_action="alias")

    ws_rm = ws_sub.add_parser("remove", help="Forget about a workspace.")
    ws_rm.add_argument("key", help="Path or alias")
    ws_rm.set_defaults(func=cmd_workspaces, ws_action="remove")

    p_sync = sub.add_parser("sync", help="Push local changes to the project's collection.")
    p_sync.add_argument("path", nargs="?", default=".")
    p_sync.add_argument("--dry-run", action="store_true")
    p_sync.set_defaults(func=cmd_sync)

    p_status = sub.add_parser("status", help="Show config + project state.")
    p_status.add_argument("path", nargs="?", default=".")
    p_status.set_defaults(func=cmd_status)

    p_code = sub.add_parser(
        "code",
        help="Project-scoped code agent REPL. Pass a project NAME (registry lookup) or PATH; default cwd.",
    )
    p_code.add_argument("target", nargs="?", help="Project name (from registry) or path")
    p_code.add_argument("--yolo", action="store_true",
                        help="Auto-approve every bash command regardless of intent (no confirmation prompts)")
    p_code.set_defaults(func=cmd_code)

    p_chat = sub.add_parser(
        "chat",
        help="Persona-based conversational agent with persistent memory (each persona has its own Collection).",
    )
    p_chat.add_argument("name", nargs="?", help="Persona name (default: most-recently-used or 'default')")
    p_chat.add_argument("--new", metavar="NAME", help="Create a new persona; opens $EDITOR on its prompt file")
    p_chat.add_argument("--list", action="store_true", help="List all personas and exit")
    p_chat.add_argument("--edit", metavar="NAME", help="Open an existing persona's prompt in $EDITOR")
    p_chat.add_argument("--delete", metavar="NAME", help="Delete a persona (prompt + state dir)")
    p_chat.add_argument("--yolo", action="store_true", help="Auto-approve bash commands")
    p_chat.add_argument("--yes", action="store_true", help="Skip confirmation prompts (used with --delete)")
    p_chat.set_defaults(func=cmd_chat)

    p_config = sub.add_parser("config", help="Write a config template to ~/.config/xli/config.json if missing.")
    p_config.set_defaults(func=cmd_config)

    p_setup = sub.add_parser(
        "setup",
        help="One-shot first-time setup: writes config, checks env mgmt key, provisions primary + workers.",
    )
    p_setup.add_argument("--workers", type=int, default=8, help="Number of worker keys to create (default: 8)")
    p_setup.add_argument("--expire-days", type=int, default=DEFAULT_EXPIRE_DAYS,
                         help=f"Key expiration in days (default: {DEFAULT_EXPIRE_DAYS}; 0 = no expiry)")
    p_setup.add_argument("--force", action="store_true", help="Re-run bootstrap even if pool already populated")
    p_setup.set_defaults(func=cmd_setup)

    p_bootstrap = sub.add_parser(
        "bootstrap",
        help="Provision worker API keys via the management API (lower-level than `setup`).",
    )
    p_bootstrap.add_argument("--count", type=int, default=8, help="How many worker keys to create (default: 8)")
    p_bootstrap.add_argument("--prefix", default="worker", help="Label prefix for created keys (default: 'worker')")
    p_bootstrap.add_argument("--expire-days", type=int, default=DEFAULT_EXPIRE_DAYS,
                             help=f"Key expiration in days (default: {DEFAULT_EXPIRE_DAYS}; 0 = no expiry)")
    p_bootstrap.add_argument("--force", action="store_true", help="Add new keys even if matching prefix already exists")
    p_bootstrap.add_argument("--revoke", action="store_true", help="Revoke (delete) all keys with matching prefix")
    p_bootstrap.add_argument("--yes", action="store_true", help="Skip confirmation when revoking")
    p_bootstrap.set_defaults(func=cmd_bootstrap)

    p_models = sub.add_parser("models", help="Inspect or set the orchestrator/worker models.")
    p_models_sub = p_models.add_subparsers(dest="action", required=True)

    p_models_list = p_models_sub.add_parser("list", help="List models the team has access to.")
    p_models_list.set_defaults(func=cmd_models)

    p_models_rec = p_models_sub.add_parser("recommended", help="Show heuristic best-of-class picks.")
    p_models_rec.set_defaults(func=cmd_models)

    p_models_set = p_models_sub.add_parser("set", help="Pin orchestrator and/or worker model(s).")
    p_models_set.add_argument("--orchestrator", help="Model id for the main agent")
    p_models_set.add_argument("--worker", help="Model id for dispatched workers")
    p_models_set.set_defaults(func=cmd_models)

    p_keys = sub.add_parser("keys", help="Manage chat keys (list / rotate / expire / revoke).")
    p_keys_sub = p_keys.add_subparsers(dest="action", required=True)

    p_keys_list = p_keys_sub.add_parser("list", help="List local chat keys with their server-side expiration.")
    p_keys_list.set_defaults(func=cmd_keys)

    p_keys_rot = p_keys_sub.add_parser("rotate", help="Rotate the secret of one or all keys (same key_id, new value).")
    p_keys_rot.add_argument("--label", help="Rotate only this label (otherwise: all)")
    p_keys_rot.set_defaults(func=cmd_keys)

    p_keys_exp = p_keys_sub.add_parser("expire", help="Update expireTime on existing key(s).")
    p_keys_exp.add_argument("--days", type=int, required=True, help="Days from now (0 = remove expiry)")
    p_keys_exp.add_argument("--label", help="Apply to a single label (otherwise: all)")
    p_keys_exp.set_defaults(func=cmd_keys)

    p_keys_rev = p_keys_sub.add_parser("revoke", help="Delete keys by label prefix (server-side + local).")
    p_keys_rev.add_argument("--prefix", default="worker", help="Label prefix to revoke (default: worker)")
    p_keys_rev.add_argument("--yes", action="store_true", help="Skip confirmation")
    p_keys_rev.set_defaults(func=cmd_keys)

    p_gc = sub.add_parser("gc", help="Find and delete orphan xAI collections.")
    p_gc.add_argument("--dry-run", action="store_true", help="Show what would be deleted, take no action")
    p_gc.add_argument("--yes", action="store_true", help="Delete all orphans without prompting")
    p_gc.set_defaults(func=cmd_gc)

    p_plugin = sub.add_parser(
        "plugin",
        help="Manage plugins (markdown API descriptors used via /lib + /get).",
    )
    p_plugin.add_argument("--new", metavar="ID", help="Create a new plugin from template; opens $EDITOR")
    p_plugin.add_argument("--add", metavar="ID", help="Interactively create a new plugin")
    p_plugin.add_argument("--list", action="store_true", help="List all installed plugins")
    p_plugin.add_argument("--show", metavar="ID", help="Print a plugin's full markdown")
    p_plugin.add_argument("--edit", metavar="ID", help="Edit a plugin in $EDITOR")
    p_plugin.add_argument("--delete", metavar="ID", help="Delete a plugin")
    p_plugin.add_argument("--install-stock", action="store_true",
                          help="Copy the bundled starter pack into ~/.config/xli/plugins/ "
                               "(skips plugins you already have unless --force)")
    p_plugin.add_argument("--force", action="store_true",
                          help="With --install-stock: overwrite plugins you've already installed")
    p_plugin.add_argument("--yes", action="store_true", help="Skip confirmation for --delete")
    p_plugin.set_defaults(func=cmd_plugin)

    p_auth = sub.add_parser(
        "auth",
        help="Manage plugin credentials in the encrypted vault (~/.config/xli/vault.enc).",
    )
    auth_sub = p_auth.add_subparsers(dest="auth_action", required=True)

    auth_set = auth_sub.add_parser("set", help="Store one or more KEY=value secrets for a plugin.")
    auth_set.add_argument("plugin", help="Plugin id (must match the plugin's frontmatter id)")
    auth_set.add_argument("assignments", nargs="+", metavar="KEY=value",
                          help="One or more KEY=value pairs (e.g. OPENWEATHER_KEY=xxx)")
    auth_set.set_defaults(func=cmd_auth, auth_action="set")

    auth_list = auth_sub.add_parser("list", help="List plugins with stored secrets, or keys for one plugin.")
    auth_list.add_argument("plugin", nargs="?", help="If omitted, list all plugins; if given, list keys for that plugin")
    auth_list.set_defaults(func=cmd_auth, auth_action="list")

    auth_show = auth_sub.add_parser("show", help="Show stored keys for a plugin (values redacted by default).")
    auth_show.add_argument("plugin", help="Plugin id")
    auth_show.add_argument("--reveal", action="store_true", help="Print plaintext values instead of redacting")
    auth_show.set_defaults(func=cmd_auth, auth_action="show")

    auth_clear = auth_sub.add_parser("clear", help="Remove a single key, or every key for a plugin.")
    auth_clear.add_argument("plugin", help="Plugin id")
    auth_clear.add_argument("key", nargs="?", help="Specific key to remove (omit to clear all keys for the plugin)")
    auth_clear.set_defaults(func=cmd_auth, auth_action="clear")

    p_doc = sub.add_parser(
        "doc",
        help="Manage reference docs (markdown files attached via /doc in any REPL).",
    )
    p_doc.add_argument("--new", metavar="NAME", help="Create a new doc; opens $EDITOR")
    p_doc.add_argument("--list", action="store_true", help="List all docs")
    p_doc.add_argument("--edit", metavar="NAME", help="Open an existing doc in $EDITOR")
    p_doc.add_argument("--delete", metavar="NAME", help="Delete a doc")
    p_doc.add_argument("--yes", action="store_true", help="Skip confirmation prompt for --delete")
    p_doc.set_defaults(func=cmd_doc)

    p_scratch = sub.add_parser(
        "scratch",
        help="Spin up an ephemeral local-only project under ~/.xli/scratch/<name>/ and drop into chat.",
    )
    p_scratch.add_argument("name", nargs="?", help="Scratch name (default: timestamp)")
    p_scratch.add_argument("--no-chat", action="store_true", help="Just create the project, don't enter chat")
    p_scratch.add_argument("--yolo", action="store_true", help="Pass --yolo to chat (auto-approve bash)")
    p_scratch.add_argument("--force", action="store_true", help="Re-init even if scratch with this name exists")
    p_scratch.set_defaults(func=cmd_scratch)

    p_ask = sub.add_parser(
        "ask",
        help="One-shot agent run (non-interactive). Used by the XMPP daemon for fallback dispatch.",
    )
    p_ask.add_argument(
        "--workspace",
        help="Workspace alias or path (default: most-recently-active project from registry)",
    )
    p_ask.add_argument("prompt", help="The user message to feed the agent")
    p_ask.set_defaults(func=cmd_ask)

    p_daemon = sub.add_parser(
        "daemon",
        help="Run the inbound XMPP command daemon (listens for OMEMO DMs, dispatches to verbs/agent).",
    )
    p_daemon.add_argument("--xmpp", action="store_true", required=True,
                          help="Use the XMPP transport (currently the only one)")
    p_daemon.add_argument("--config", help="Path to daemon.toml (default: ~/.config/xli/daemon.toml)")
    p_daemon.set_defaults(func=cmd_daemon)

    p_help = sub.add_parser("help", help="Show grouped command listing.")
    p_help.set_defaults(func=cmd_help)

    args = p.parse_args()

    # Auto-touch the cwd as a workspace, so the registry keeps an honest
    # last_active timeline for every dir xli has run in. Skip stateless
    # commands (config/setup/keys/etc.) and the workspaces command itself
    # (`xli workspaces` is a registry inspection, not a workspace activity).
    _STATELESS_COMMANDS = {
        "help", "config", "setup", "models", "keys", "bootstrap",
        "gc", "workspaces", "ask", "daemon",
    }
    if args.command not in _STATELESS_COMMANDS:
        try:
            ws_mod.touch(Path.cwd())
        except Exception:
            pass  # touching is best-effort; never block the actual command

    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
