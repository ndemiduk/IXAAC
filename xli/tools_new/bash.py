from __future__ import annotations

import re
import subprocess
from typing import Any

from .context import ToolContext, ToolResult
from .helpers import _truncate

VALID_INTENTS = {"read-only", "modifies-system", "network", "modifies-project"}
INTENT_READ_ONLY = "read-only"
GATED_INTENTS = {"network", "modifies-system"}

WORKER_FORBIDDEN_PATTERNS = [
    (r"(?:^|[;&|\n(`])\s*(?:rm|mv|cp|touch|dd|tee|chmod|chown|mkdir|mkfifo|ln|truncate|shred)\b", "file-mutating command"),
    (r"\bsed\s+-[a-zA-Z]*i\b", "sed -i (in-place edit)"),
    (r"\bsed\s+--in-place\b", "sed --in-place"),
    (r"\bawk\s+.*-i\s+inplace\b", "awk -i inplace"),
    (r"(?:^|[;&|\n(`])\s*(?:kill|killall|pkill)\b", "process-control command"),
    (r"(?:^|[;&|\n(`])\s*(?:pip3?|npm|pnpm|yarn|apt(?:-get)?|dnf|yum|brew|cargo|gem|go|composer|gradle|mvn|gh)\s+(?:install|add|update|upgrade|remove|uninstall|publish)\b", "package-manager mutation"),
    (r"\bgit\s+(?:commit|push|checkout|reset|clean|rebase|merge|add|rm|mv|cherry-pick|revert|stash|init|am)\b", "git mutating subcommand"),
    (r"(?:^|[;&|\n(`])\s*(?:eval|exec|source)\s", "eval/exec/source (shell-string execution)"),
    (r"(?:^|[;&|\n(`])\s*\.\s+\S", "dot-source"),
]

def _check_read_only_command(cmd: str) -> tuple[bool, str]:
    """Return (ok, reason). ok=False means cmd contains an obvious mutation
    pattern that should not be running under intent=read-only. reason names
    which class of mutation tripped."""
    for pattern, reason in WORKER_FORBIDDEN_PATTERNS:
        if re.search(pattern, cmd):
            return (False, reason)
    return (True, "")


def t_bash(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    cmd = args["command"]
    intent = args.get("intent", "")
    timeout = int(args.get("timeout", 60))

    # Validate intent (required, must be one of the four)
    if intent not in VALID_INTENTS:
        return ToolResult(
            f"bash refused: missing or invalid `intent` (got {intent!r}). "
            f"Required values: {sorted(VALID_INTENTS)}. Declare what this command is meant to do.",
            is_error=True,
        )

    # Workers may only run read-only commands.
    if ctx.is_worker and intent != INTENT_READ_ONLY:
        return ToolResult(
            f"bash refused: workers may only run intent={INTENT_READ_ONLY!r} commands; "
            f"got {intent!r}. Report this back to the orchestrator instead.",
            is_error=True,
        )

    # Defense-in-depth on the intent gate. Catches casual misclassifications.
    # Obfuscation needs real sandboxes. Don't panic.
    if intent == INTENT_READ_ONLY:
        ok, reason = _check_read_only_command(cmd)
        if not ok:
            return ToolResult(
                f"bash refused: command matches a known mutation pattern "
                f"({reason}) but intent={intent!r}. Re-declare with the "
                "appropriate non-read-only intent, or rephrase if this is "
                "genuinely read-only and the pattern matched in error.",
                is_error=True,
            )

    # Reasoning models occasionally emit HTML-escaped bash (`&amp;` for `&`,
    # `&lt;&lt;&lt;` for `<<<`, etc.) — silently breaks heredocs and URLs in
    # non-obvious ways. Decode BEFORE the gate prompt so the user approves the
    # command we're actually going to run, not the encoded version.
    if any(ent in cmd for ent in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;", "&apos;")):
        import html as _html
        decoded = _html.unescape(cmd)
        if decoded != cmd:
            if ctx.console is not None:
                ctx.console.print(
                    "  [yellow]⚠ model emitted HTML-escaped bash — auto-decoded "
                    "(consider switching to a non-reasoning orchestrator)[/yellow]"
                )
            cmd = decoded

    # Gate riskier intents on a human y/N (skipped in yolo mode).
    if intent in GATED_INTENTS and not ctx.yolo:
        if ctx.console is None:
            return ToolResult(
                f"bash refused: intent={intent!r} requires confirmation but no console "
                "is attached (running headless). Use --yolo or simpler intent.",
                is_error=True,
            )
        ctx.console.print(
            f"  [yellow]⚠ intent={intent}[/yellow]  [bold]{cmd}[/bold]"
        )
        try:
            answer = input("  approve? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "y":
            return ToolResult(
                f"bash denied by user (intent={intent}). "
                "Try a different approach or ask the user for permission first.",
                is_error=True,
            )

    # Inject vault-stored secrets for any subscribed plugin's $VAR / ${VAR}
    # referenced in the command. Cheap when nothing matches — env_for_command
    # short-circuits before unlocking the vault.
    env = None
    if ctx.subscribed_plugins:
        from xli.plugin import Plugin
        from xli.vault import env_for_command
        plugins = [Plugin(id=pid) for pid in ctx.subscribed_plugins]
        overrides = env_for_command(cmd, plugins)
        if overrides:
            import os as _os
            env = {**_os.environ, **overrides}

    try:
        # Pin to /bin/bash, NOT /bin/sh (which is dash on Debian/Ubuntu and
        # rejects bash-only constructs like <<< herestrings, [[ ]] tests, and
        # `${var,,}` case ops). Agents (and our plugin docs) routinely emit
        # bash-specific syntax — surfacing it as a "bash" tool that ran in dash
        # would be a constant footgun. Both Linux and macOS ship /bin/bash.
        proc = subprocess.run(
            cmd,
            shell=True,
            executable="/bin/bash",
            cwd=ctx.project.project_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(f"command timed out after {timeout}s", is_error=True)
    out = proc.stdout
    if proc.stderr:
        out += "\n--- stderr ---\n" + proc.stderr
    out += f"\n--- exit {proc.returncode} ---"
    # any successful command might have touched files
    if proc.returncode == 0:
        ctx.dirty_paths.add("__rescan__")
    return ToolResult(_truncate(out), is_error=proc.returncode != 0)
