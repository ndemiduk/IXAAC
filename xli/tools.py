"""Agent tool implementations.

Each tool is a function that takes (project_root, args_dict, ctx) and returns
ToolResult(content, dirty_paths). Dirty paths are queued for end-of-turn sync.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from xli.client import Clients
from xli.config import GlobalConfig, ProjectConfig

# How much of a long output to keep before truncating, per tool call.
MAX_OUTPUT_BYTES = 10_000


# Worker read-only guard. Catches the classics (rm, pip install, git push, eval).
# Base64 obfuscation still wins — that's why we have sandboxes. Don't panic.
_WORKER_CMD_BOUNDARY = r"(?:^|[;&|\n(`])"

WORKER_FORBIDDEN_PATTERNS: list[tuple[str, str]] = [
    # File mutation commands at command position
    (rf"{_WORKER_CMD_BOUNDARY}\s*(?:rm|mv|cp|touch|dd|tee|chmod|chown|mkdir|mkfifo|ln|truncate|shred)\b", "file-mutating command"),
    # In-place text editors (sed without -i is read-only)
    (r"\bsed\s+-[a-zA-Z]*i\b", "sed -i (in-place edit)"),
    (r"\bsed\s+--in-place\b", "sed --in-place"),
    (r"\bawk\s+.*-i\s+inplace\b", "awk -i inplace"),
    # Process control
    (rf"{_WORKER_CMD_BOUNDARY}\s*(?:kill|killall|pkill)\b", "process-control command"),
    # Package managers (install / publish / etc.)
    (rf"{_WORKER_CMD_BOUNDARY}\s*(?:pip3?|npm|pnpm|yarn|apt(?:-get)?|dnf|yum|brew|cargo|gem|go|composer|gradle|mvn|gh)\s+(?:install|add|update|upgrade|remove|uninstall|publish)\b", "package-manager mutation"),
    # VCS write subcommands
    (r"\bgit\s+(?:commit|push|checkout|reset|clean|rebase|merge|add|rm|mv|cherry-pick|revert|stash|init|am)\b", "git mutating subcommand"),
    # Shell-string execution helpers — these can hide anything above
    (rf"{_WORKER_CMD_BOUNDARY}\s*(?:eval|exec|source)\s", "eval/exec/source (shell-string execution)"),
    (rf"{_WORKER_CMD_BOUNDARY}\s*\.\s+\S", "dot-source"),
]


def _check_read_only_command(cmd: str) -> tuple[bool, str]:
    """Return (ok, reason). ok=False means cmd contains an obvious mutation
    pattern that should not be running under intent=read-only. reason names
    which class of mutation tripped."""
    for pattern, reason in WORKER_FORBIDDEN_PATTERNS:
        if re.search(pattern, cmd):
            return (False, reason)
    return (True, "")


@dataclass
class ToolContext:
    project: ProjectConfig
    clients: Clients
    cfg: GlobalConfig
    pool: Any = None        # ClientPool when running in main agent; None for workers
    console: Any = None     # rich.Console for ad-hoc UI; None in workers
    dirty_paths: set[str] = field(default_factory=set)
    yolo: bool = False      # skip per-intent confirmation gate when True
    is_worker: bool = False # workers cannot run intent > read-only
    # Additional collection_ids to include in search_project alongside the
    # project's own collection. Populated from Agent.attached_refs — the
    # /ref slash command attaches persona memories for cross-session recall.
    extra_collection_ids: list[str] = field(default_factory=list)
    # Subscribed plugin IDs for this session. plugin_search and plugin_get
    # only see plugins in this list (mandatory subscription model — keeps
    # the active set bounded as the catalog grows).
    subscribed_plugins: list[str] = field(default_factory=list)
    # Server-tool sub-call usage (Responses API). Server tools fire one-shot
    # responses.create() calls outside the main chat loop; the agent drains
    # these accumulators after each tool batch into its CallStats.
    server_prompt_tokens: int = 0
    server_completion_tokens: int = 0
    server_cost: float = 0.0
    server_calls: int = 0
    # Simple per-turn result cache for read-only / deterministic tools.
    # Key: (tool_name, sorted_args_json). Value: result_text.
    # Prevents re-emitting identical large outputs (read_file, grep, read_plan_notes)
    # when the agent re-explores the same thing.
    _tool_result_cache: dict = field(default_factory=dict)
    # Per-file consecutive edit_file failure counter. Reset on success. Used
    # to break the read-edit-fail-re-read cascade we observed eating ~15
    # iterations on a single edit job: after 2 consecutive failures on the
    # same file, edit_file injects the file content into the error so the
    # model can see ground truth without another read round-trip.
    _edit_failures: dict = field(default_factory=dict)

    def record_server_usage(
        self, model: str, prompt_tokens: int, completion_tokens: int
    ) -> None:
        from xli.cost import estimate_cost
        self.server_prompt_tokens += prompt_tokens
        self.server_completion_tokens += completion_tokens
        c = estimate_cost(self.cfg.pricing, model, prompt_tokens, completion_tokens)
        if c is not None:
            self.server_cost += c
        self.server_calls += 1

    def drain_server_usage(self) -> tuple[int, int, float, int]:
        """Return accumulated server-tool usage and reset the counters."""
        out = (
            self.server_prompt_tokens,
            self.server_completion_tokens,
            self.server_cost,
            self.server_calls,
        )
        self.server_prompt_tokens = 0
        self.server_completion_tokens = 0
        self.server_cost = 0.0
        self.server_calls = 0
        return out

    def _get_cached_result(self, name: str, args: dict) -> Optional[str]:
        """Return cached result text for a deterministic read-only tool call, or None."""
        if name not in {"read_file", "list_dir", "glob", "grep", "search_project",
                        "read_plan_notes", "plugin_search", "plugin_get"}:
            return None
        try:
            key = (name, json.dumps(args, sort_keys=True))
        except Exception:
            return None
        return self._tool_result_cache.get(key)

    def _cache_result(self, name: str, args: dict, result_text: str) -> None:
        """Store result for future identical calls in this turn."""
        if name not in {"read_file", "list_dir", "glob", "grep", "search_project",
                        "read_plan_notes", "plugin_search", "plugin_get"}:
            return
        try:
            key = (name, json.dumps(args, sort_keys=True))
            self._tool_result_cache[key] = result_text
        except Exception:
            pass


# Bash intents — declared by the agent on every bash call so the human can
# see what the call is *meant* to do (transparency) and so we gate the
# riskier ones with a y/N prompt (cheap circuit-breaker for hallucinations).
INTENT_READ_ONLY = "read-only"             # ls, cat, grep, find, git status/log/diff, pytest
INTENT_MODIFIES_PROJECT = "modifies-project"  # git add/commit, file rewrites via shell, sed -i in tree
INTENT_MODIFIES_SYSTEM = "modifies-system"    # apt, sudo, anything outside project, system config
INTENT_NETWORK = "network"                  # curl, wget, pip install, npm install, git push/pull/fetch

VALID_INTENTS = {INTENT_READ_ONLY, INTENT_MODIFIES_PROJECT, INTENT_MODIFIES_SYSTEM, INTENT_NETWORK}
GATED_INTENTS = {INTENT_MODIFIES_SYSTEM, INTENT_NETWORK}


@dataclass
class ToolResult:
    content: str
    is_error: bool = False


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_BYTES:
        return text
    keep = MAX_OUTPUT_BYTES // 2
    return (
        text[:keep]
        + f"\n\n... [truncated {len(text) - MAX_OUTPUT_BYTES} bytes] ...\n\n"
        + text[-keep:]
    )


def _resolve_in_project(ctx: ToolContext, relpath: str) -> Path:
    """Resolve a relpath, refusing anything that escapes the project root."""
    root = ctx.project.project_root.resolve()
    target = (root / relpath).resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"path escapes project root: {relpath}")
    return target


def _mark_dirty(ctx: ToolContext, path: Path) -> None:
    rel = path.resolve().relative_to(ctx.project.project_root.resolve()).as_posix()
    ctx.dirty_paths.add(rel)


def _ignore_spec(ctx: ToolContext):
    """Lazily load + cache the project's ignore spec on the ToolContext.
    Used by the read tools (read_file, grep, glob) so that .env, secrets,
    .gitignored content, build outputs, etc. are not silently exposed to the
    agent — same surface that the sync engine excludes from the Collection.
    """
    spec = getattr(ctx, "_ignore_spec_cache", None)
    if spec is None:
        from xli.ignore import load_ignore_spec
        extras = list(getattr(ctx.project, "extra_ignores", None) or [])
        spec = load_ignore_spec(ctx.project.project_root, extras)
        # Stash on the context so subsequent tools in the same turn reuse it.
        ctx._ignore_spec_cache = spec
    return spec


def _is_ignored(ctx: ToolContext, relpath: str) -> bool:
    """Check whether a project-relative path matches the ignore spec.
    Tests both file form ("foo/bar.env") and dir form ("foo/bar/") so that
    directory rules like ".env/" cover their contents too."""
    spec = _ignore_spec(ctx)
    return spec.match_file(relpath) or spec.match_file(relpath + "/")


# --------------------------------------------------------------------------- #
#  Tool implementations
# --------------------------------------------------------------------------- #

def t_read_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = _resolve_in_project(ctx, args["path"])
    if not path.exists():
        return ToolResult(f"file not found: {args['path']}", is_error=True)
    if not path.is_file():
        return ToolResult(f"not a file: {args['path']}", is_error=True)
    rel = path.relative_to(ctx.project.project_root.resolve()).as_posix()
    if _is_ignored(ctx, rel):
        return ToolResult(
            f"refused: {args['path']} is in the project's ignore list "
            "(.gitignore / .xliignore / built-in defaults). Likely contains "
            "secrets, build artifacts, or non-content data the user does not "
            "want exposed. If you genuinely need this file, ask the user — "
            "they can move it or whitelist it via .xliignore negation rules.",
            is_error=True,
        )
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return ToolResult(f"read error: {e}", is_error=True)
    offset = int(args.get("offset", 0))
    limit = int(args.get("limit", 0)) or None
    lines = text.splitlines()
    if offset or limit:
        end = offset + limit if limit else len(lines)
        lines = lines[offset:end]
    numbered = "\n".join(f"{i+offset+1:6}\t{ln}" for i, ln in enumerate(lines))
    return ToolResult(_truncate(numbered))


def t_write_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = _resolve_in_project(ctx, args["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args["content"], encoding="utf-8")
    _mark_dirty(ctx, path)
    return ToolResult(f"wrote {args['path']} ({len(args['content'])} bytes)")


def _normalize_indent(text: str, start: int, new: str) -> str:
    """When fuzzy-matching swaps content, drop any leading whitespace the
    model put on `new` if the matched span starts after the line's existing
    indent.

    The bug this fixes: fuzzy regex matches "from xli.cost import X"
    identically whether the file has 8-space, tab, or mixed indentation. The
    model's `new_string` carries its own (possibly wrong) leading whitespace —
    and `text[:m.start()]` already contains the file's actual indent for that
    line. Splicing `new` in verbatim therefore *doubles* the indent
    (8-spaces + tab + content), which is the exact garbage we observed in
    trace-005/006 that broke imports and triggered the recovery cascade.

    The right fix is to strip `new`'s leading whitespace in the "match starts
    at the line's indent" case — the file's indent is already preserved in
    text[:m.start()] so adding more is wrong every time.

    Scope: single-line `new` only. For multi-line `new` we trust the model's
    intra-block indentation; rewriting it would change semantics.
    """
    if "\n" in new:
        return new
    line_start = text.rfind("\n", 0, start) + 1
    matched_indent = text[line_start:start]
    # Strip new's leading whitespace whenever the matched span starts at the
    # logical beginning of a line — either at column 0 or right after pure
    # whitespace indent. In both cases text[:start] already preserves the
    # file's actual indent (empty, tabs, spaces — whatever it is) and any
    # leading whitespace on `new` is additive: it doubles existing indent or
    # invents indent where none belongs. The trace-007 case was a column-0
    # top-level import where the model's `new` had a spurious tab — exactly
    # the bug an empty-indent guard misses.
    if matched_indent.strip() != "":
        # Match starts mid-line after non-whitespace — leave new as-is.
        return new
    return new.lstrip(" \t")


def _whitespace_fuzzy_pattern(old: str) -> Optional[re.Pattern]:
    """Build a regex that matches `old` with whitespace runs treated as `\\s+`.

    Models frequently lose or gain a tab/space when echoing a file fragment
    back into edit_file's old_string — exact matching then fails and triggers
    a cascade of re-reads. Collapsing whitespace runs eliminates the most
    common class of failure without changing semantic intent.

    Returns None when the input is empty or the constructed pattern is
    invalid (defensive — callers fall back to the exact-match failure path).
    """
    if not old or not old.strip():
        return None
    parts = [p for p in re.split(r"\s+", old) if p]
    if not parts:
        return None
    pat = r"\s+".join(re.escape(p) for p in parts)
    try:
        return re.compile(pat, re.DOTALL)
    except re.error:
        return None


def t_edit_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    rel = args["path"]
    path = _resolve_in_project(ctx, rel)
    if not path.exists():
        return ToolResult(f"file not found: {rel}", is_error=True)
    text = path.read_text(encoding="utf-8")
    old = args["old_string"]
    new = args["new_string"]
    replace_all = bool(args.get("replace_all", False))

    # Fast path: exact substring match (the historical behavior).
    if old in text:
        if not replace_all and text.count(old) > 1:
            ctx._edit_failures[rel] = ctx._edit_failures.get(rel, 0) + 1
            return ToolResult(
                f"old_string is not unique ({text.count(old)} occurrences); "
                "use replace_all=true or include more context",
                is_error=True,
            )
        new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        path.write_text(new_text, encoding="utf-8")
        _mark_dirty(ctx, path)
        ctx._edit_failures.pop(rel, None)
        return ToolResult(f"edited {rel}")

    # Fuzzy fallback: whitespace-tolerant match. Cuts the failure cascade
    # we measured (1.16M-tok runaway turn was driven by 6 exact-match
    # failures, each triggering 2-3 diagnostic reads).
    rx = _whitespace_fuzzy_pattern(old)
    matches = list(rx.finditer(text)) if rx is not None else []
    if matches:
        if not replace_all and len(matches) > 1:
            ctx._edit_failures[rel] = ctx._edit_failures.get(rel, 0) + 1
            return ToolResult(
                f"old_string (whitespace-tolerant match) found {len(matches)} occurrences; "
                "use replace_all=true or include more context",
                is_error=True,
            )
        if replace_all:
            new_text = rx.sub(lambda _m: new, text)
        else:
            m = matches[0]
            new_text = text[: m.start()] + _normalize_indent(text, m.start(), new) + text[m.end() :]
        path.write_text(new_text, encoding="utf-8")
        _mark_dirty(ctx, path)
        ctx._edit_failures.pop(rel, None)
        return ToolResult(f"edited {rel} (whitespace-tolerant match)")

    # Both exact and fuzzy failed. Track the failure and, on the second
    # consecutive miss, attach a head of the current file content so the
    # model has ground truth in the same iteration — no read round-trip.
    fails = ctx._edit_failures.get(rel, 0) + 1
    ctx._edit_failures[rel] = fails
    if fails >= 2:
        # Cap at ~1500 chars: agent.py's MAX_TOOL_RESULT_CHARS truncates
        # tool results in history anyway, but the current iteration sees
        # the full payload, which is what matters for breaking the spiral.
        head_lines = text.splitlines()[:120]
        head = "\n".join(f"{i+1:6}\t{ln}" for i, ln in enumerate(head_lines))
        if len(head) > 1500:
            head = head[:1500] + "\n[... truncated; read_file for more]"
        return ToolResult(
            f"old_string not found in {rel} (consecutive failure #{fails}). "
            f"current file head:\n{head}",
            is_error=True,
        )
    return ToolResult(f"old_string not found in {rel}", is_error=True)


def t_list_dir(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = _resolve_in_project(ctx, args.get("path", "."))
    if not path.is_dir():
        return ToolResult(f"not a directory: {args.get('path', '.')}", is_error=True)
    entries = []
    for child in sorted(path.iterdir()):
        marker = "/" if child.is_dir() else ""
        entries.append(f"{child.name}{marker}")
    return ToolResult("\n".join(entries) or "(empty)")


def t_glob(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pattern = args["pattern"]
    root = ctx.project.project_root
    matches = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if not fnmatch.fnmatch(rel, pattern):
            continue
        # Same ignore filter as sync — keeps secrets / build outputs out of
        # results even though glob is read-only.
        if _is_ignored(ctx, rel):
            continue
        matches.append(rel)
    matches.sort()
    if not matches:
        return ToolResult("(no matches)")
    return ToolResult(_truncate("\n".join(matches)))


def t_grep(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pattern = args["pattern"]
    glob_pat = args.get("glob")
    case_insensitive = bool(args.get("case_insensitive", False))
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return ToolResult(f"invalid regex: {e}", is_error=True)
    root = ctx.project.project_root
    out_lines: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if glob_pat and not fnmatch.fnmatch(rel, glob_pat):
            continue
        # Skip ignored paths so grep can't surface .env / build / secrets
        # content the sync engine excludes from the Collection.
        if _is_ignored(ctx, rel):
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if rx.search(line):
                        out_lines.append(f"{rel}:{i}: {line.rstrip()}")
        except OSError:
            continue
    if not out_lines:
        return ToolResult("(no matches)")
    return ToolResult(_truncate("\n".join(out_lines)))


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


def t_web_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from xli.server_tools import web_search
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult("web_search: 'query' is required", is_error=True)
    try:
        text = web_search(
            ctx.clients, ctx.cfg, query,
            allowed_domains=args.get("allowed_domains") or None,
            excluded_domains=args.get("excluded_domains") or None,
            sink=ctx,
        )
    except Exception as e:
        return ToolResult(f"web_search failed: {type(e).__name__}: {e}", is_error=True)
    return ToolResult(_truncate(text))


def t_x_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from xli.server_tools import x_search
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult("x_search: 'query' is required", is_error=True)
    try:
        text = x_search(
            ctx.clients, ctx.cfg, query,
            allowed_x_handles=args.get("allowed_x_handles") or None,
            excluded_x_handles=args.get("excluded_x_handles") or None,
            from_date=args.get("from_date") or None,
            to_date=args.get("to_date") or None,
            enable_image_understanding=args.get("enable_image_understanding"),
            enable_video_understanding=args.get("enable_video_understanding"),
            sink=ctx,
        )
    except Exception as e:
        return ToolResult(f"x_search failed: {type(e).__name__}: {e}", is_error=True)
    return ToolResult(_truncate(text))


def t_code_execute(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from xli.server_tools import code_execute
    task = (args.get("task") or args.get("code") or "").strip()
    if not task:
        return ToolResult("code_execute: 'task' is required", is_error=True)
    try:
        text = code_execute(ctx.clients, ctx.cfg, task, sink=ctx)
    except Exception as e:
        return ToolResult(f"code_execute failed: {type(e).__name__}: {e}", is_error=True)
    return ToolResult(_truncate(text))


def t_plugin_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from xli.plugin import (
        NO_PLUGIN_MATCH_MARKER,
        Plugin,
        search_plugins,
    )
    intent = (args.get("intent") or "").strip()
    if not intent:
        return ToolResult("plugin_search: 'intent' is required", is_error=True)
    if not ctx.subscribed_plugins:
        return ToolResult(
            f"{NO_PLUGIN_MATCH_MARKER} for intent={intent!r}\n"
            "No plugins are subscribed for this project. The user can install/subscribe "
            "plugins with `xli plugin --new` and `/lib subscribe <id>`. "
            "Tell the user no plugin is available — do NOT fabricate plugin output."
        )
    available = [Plugin(id=pid) for pid in ctx.subscribed_plugins]
    available = [p for p in available if p.exists()]
    matches = search_plugins(intent, available, limit=5)
    if not matches:
        cats = sorted({c for p in available for c in p.categories()})
        cat_line = (
            "Categories of subscribed plugins: " + ", ".join(cats)
            if cats else
            "No plugin categories declared on subscribed plugins."
        )
        return ToolResult(
            f"{NO_PLUGIN_MATCH_MARKER} for intent={intent!r}\n"
            f"{cat_line}\n"
            "Suggest: install/subscribe a plugin that fits, or fall back to "
            "web_search/bash. Do NOT fabricate plugin output."
        )
    out = ["Top matches (call plugin_get for full content of any candidate):"]
    for p, score in matches:
        cats = ", ".join(p.categories()) or "—"
        out.append(
            f"- [{p.id}] (score={score:.1f}, risk={p.risk()}, categories={cats})\n"
            f"    {p.name()}: {p.description() or '(no description)'}"
        )
    return ToolResult("\n".join(out))


def t_plugin_get(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from xli.plugin import Plugin
    name = (args.get("name") or "").strip()
    if not name:
        return ToolResult("plugin_get: 'name' is required", is_error=True)
    if name not in ctx.subscribed_plugins:
        return ToolResult(
            f"plugin {name!r} is not subscribed for this project. "
            f"Subscribed: {ctx.subscribed_plugins or '(none)'}. "
            f"Use plugin_search first or ask the user to /lib subscribe {name}.",
            is_error=True,
        )
    p = Plugin(id=name)
    if not p.exists():
        return ToolResult(
            f"plugin {name!r} is subscribed but the file is missing on disk "
            "(orphan subscription). Tell the user — do NOT fabricate.",
            is_error=True,
        )
    try:
        text = p.read_raw()
    except OSError as e:
        return ToolResult(f"read failed: {e}", is_error=True)
    return ToolResult(_truncate(text))


def t_search_project(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = args["query"]
    limit = int(args.get("limit", 6))
    mode = args.get("retrieval_mode", ctx.cfg.retrieval_mode)

    # Build the collection set: project's own + any /ref-attached personas.
    # Filter empty (local-only projects have empty collection_id) and dedupe
    # while preserving order so the project's own results come first.
    seen: set[str] = set()
    collection_ids: list[str] = []
    for cid in [ctx.project.collection_id, *ctx.extra_collection_ids]:
        if cid and cid not in seen:
            seen.add(cid)
            collection_ids.append(cid)
    if not collection_ids:
        return ToolResult(
            "(no collections to search — local-only project with no /ref attachments)",
            is_error=True,
        )
    try:
        resp = ctx.clients.xai.collections.search(
            query=query,
            collection_ids=collection_ids,
            limit=limit,
            retrieval_mode=mode,
        )
    except Exception as e:
        return ToolResult(f"search failed: {e}", is_error=True)
    chunks = list(getattr(resp, "results", None) or getattr(resp, "chunks", []) or [])
    if not chunks:
        return ToolResult("(no results)")
    out = []
    for i, ch in enumerate(chunks, 1):
        # SearchResponse chunk shape may have .chunk.text and .file_metadata.name
        name = getattr(getattr(ch, "file_metadata", None), "name", "?")
        text = (
            getattr(ch, "text", None)
            or getattr(getattr(ch, "chunk", None), "text", "")
            or ""
        )
        score = getattr(ch, "score", None)
        header = f"[{i}] {name}" + (f"  (score={score:.3f})" if isinstance(score, float) else "")
        chunk_text = text.strip()
        if len(chunk_text) > 900:
            chunk_text = chunk_text[:850] + "\n... [truncated]"
        out.append(header + "\n" + chunk_text)
    return ToolResult(_truncate("\n\n---\n\n".join(out)))


# Filename for the plan-mode scratchpad — lives under <project>/.xli/.
# Append-only via t_plan_note. Auto-loaded into the plan-mode preamble at
# every iteration so the planner stays coherent across long investigations
# and survives /exit + max-iter aborts.
PLAN_NOTES_FILENAME = "plan-notes.md"
PLANS_ARCHIVE_DIR = "plans"


def t_plan_note(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Append a timestamped note to the plan-mode scratchpad.

    Hard-scoped: the path is fixed (<project>/.xli/plan-notes.md), there is
    no path argument, no edit, no delete. Append-only is load-bearing — it
    means the planner cannot accidentally clobber its own earlier notes.
    Optional `return_notes_after` returns the full current scratchpad content
    after the append (useful for staying coherent without a separate call).
    """
    from datetime import datetime, timezone
    text = (args.get("text") or "").strip()
    if not text:
        return ToolResult("plan_note: 'text' is required", is_error=True)

    return_notes = bool(args.get("return_notes_after", False))

    notes_path = ctx.project.xli_dir / PLAN_NOTES_FILENAME
    notes_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    block = f"\n## {ts}\n\n{text}\n"
    with notes_path.open("a", encoding="utf-8") as f:
        f.write(block)

    if return_notes:
        try:
            content = notes_path.read_text(encoding="utf-8").strip()
            return ToolResult(content or "(empty)")
        except OSError as e:
            return ToolResult(f"noted, but read failed: {e}", is_error=True)

    line_count = sum(1 for _ in notes_path.open("r", encoding="utf-8"))
    return ToolResult(f"noted ({line_count} lines in plan-notes.md)")


def t_read_plan_notes(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Return the current content of .xli/plan-notes.md.

    PLAN-MODE ONLY. Lets the agent re-read its own scratchpad after several
    plan_note calls instead of only seeing the snapshot injected at turn start.
    """
    notes_path = ctx.project.xli_dir / PLAN_NOTES_FILENAME
    if not notes_path.exists():
        return ToolResult("(empty — first plan_note call will create it)")
    try:
        content = notes_path.read_text(encoding="utf-8").strip()
        return ToolResult(content or "(empty)")
    except OSError as e:
        return ToolResult(f"read error: {e}", is_error=True)


def t_summarize_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Compact structural summary of a file instead of dumping raw source.

    Greatly reduces token usage during investigation. Use `focus` to get
    exactly what you need (e.g. "signatures", "imports", "classes", "functions").
    Much lower token cost than read_file.
    """
    path = _resolve_in_project(ctx, args["path"])
    if not path.exists() or not path.is_file():
        return ToolResult(f"file not found: {args['path']}", is_error=True)

    rel = path.relative_to(ctx.project.project_root.resolve()).as_posix()
    if _is_ignored(ctx, rel):
        return ToolResult(
            f"refused: {args['path']} is in the project's ignore list",
            is_error=True,
        )

    focus = (args.get("focus") or "all").lower().strip()

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return ToolResult(f"read error: {e}", is_error=True)

    lines = text.splitlines()
    total_lines = len(lines)

    if path.suffix == ".py":
        summary = _summarize_python_file(rel, text, total_lines, focus)
    else:
        summary = _summarize_generic(rel, lines, total_lines)

    return ToolResult(_truncate(summary))


def _summarize_python_file(rel: str, text: str, total_lines: int, focus: str = "all") -> str:
    """Python-specific summary using AST. Supports focus modes for lower token use."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _summarize_generic(rel, text.splitlines(), total_lines)

    imports = []
    classes = []
    functions = []

    for node in tree.body:  # only top-level nodes
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = ", ".join(a.name for a in node.names)
            imports.append(f"from {mod} import {names}")
        elif isinstance(node, ast.ClassDef):
            bases = [b.id if isinstance(b, ast.Name) else "..." for b in node.bases]
            base_str = f"({', '.join(bases)})" if bases else ""
            doc = ast.get_docstring(node) or ""
            first_doc = doc.split("\n")[0][:80] if doc else ""
            classes.append(f"class {node.name}{base_str}: {first_doc}")
        elif isinstance(node, ast.FunctionDef):
            args = [arg.arg for arg in node.args.args]
            if node.args.vararg:
                args.append("*" + node.args.vararg.arg)
            if node.args.kwarg:
                args.append("**" + node.args.kwarg.arg)
            doc = ast.get_docstring(node) or ""
            first_doc = doc.split("\n")[0][:60] if doc else ""
            functions.append(f"def {node.name}({', '.join(args)}): {first_doc}")

    out = [f"File: {rel} ({total_lines} lines)\n"]

    if focus in ("all", "imports") and imports:
        out.append("Imports:\n" + "\n".join(f"  - {imp}" for imp in imports[:15]))
    if focus in ("all", "classes", "signatures") and classes:
        out.append("\nClasses:\n" + "\n".join(f"  - {c}" for c in classes[:12]))
    if focus in ("all", "functions", "signatures") and functions:
        out.append("\nTop-level functions:\n" + "\n".join(f"  - {f}" for f in functions[:15]))

    # Fallback if focus filtered everything out
    if len(out) == 1:
        if focus == "imports" and not imports:
            return _summarize_generic(rel, text.splitlines(), total_lines)
        # default to showing what we have
        if classes:
            out.append("\nClasses:\n" + "\n".join(f"  - {c}" for c in classes[:12]))
        if functions:
            out.append("\nTop-level functions:\n" + "\n".join(f"  - {f}" for f in functions[:15]))

    return "\n".join(out)


def _summarize_generic(rel: str, lines: list[str], total_lines: int) -> str:
    """Generic fallback for non-Python or when AST fails."""
    head = "\n".join(f"{i+1:4}: {ln}" for i, ln in enumerate(lines[:25]))
    tail_start = max(1, total_lines - 7)
    tail = "\n".join(f"{i+1:4}: {ln}" for i, ln in enumerate(lines[-8:], start=tail_start))
    return f"File: {rel} ({total_lines} lines)\n\n--- head ---\n{head}\n\n--- tail ---\n{tail}"


# --------------------------------------------------------------------------- #
#  Tool registry + JSON schema for tool-use
# --------------------------------------------------------------------------- #

ToolFn = Callable[[ToolContext, dict[str, Any]], ToolResult]

REGISTRY: dict[str, ToolFn] = {
    "read_file": t_read_file,
    "write_file": t_write_file,
    "edit_file": t_edit_file,
    "list_dir": t_list_dir,
    "glob": t_glob,
    "grep": t_grep,
    "bash": t_bash,
    "search_project": t_search_project,
    "web_search": t_web_search,
    "x_search": t_x_search,
    "code_execute": t_code_execute,
    "plugin_search": t_plugin_search,
    "plugin_get": t_plugin_get,
    "plan_note": t_plan_note,
	    "read_plan_notes": t_read_plan_notes,
    "summarize_file": t_summarize_file,
}

# Workers are read-only investigators: same toolset minus mutation + no swarm.
WORKER_REGISTRY: dict[str, ToolFn] = {
    name: fn
    for name, fn in REGISTRY.items()
    if name not in {"write_file", "edit_file"}
}

# Tools that are safe to execute concurrently within a single tool_calls batch.
# Read-only + idempotent. dispatch_subagent is included since worker fan-out
# is the original parallel use-case.
PARALLEL_SAFE: set[str] = {
    "read_file",
    "list_dir",
    "glob",
    "grep",
    "search_project",
    "web_search",
    "x_search",
    "code_execute",
    "plugin_search",
    "plugin_get",
    "dispatch_subagent",
    "summarize_file",
}

# Tools available in PLAN MODE.
#
# Refined invariant: plan mode cannot mutate project content but may write to
# iXaac's own working files. Concretely: every tool here is either read-only
# OR is the scoped scratchpad (plan_note), which can only append to a fixed
# path under .xli/. Bash, dispatch_subagent, write_file, edit_file all stay
# excluded — they can touch project content.
PLAN_MODE_TOOLS: set[str] = {
    "read_file",
    "list_dir",
    "glob",
    "grep",
    "search_project",
    "web_search",
    "x_search",
    "plugin_search",
    "plugin_get",
    "plan_note",
    "read_plan_notes",
    "summarize_file",
}


def tool_schemas() -> list[dict]:
    """Return OpenAI-compatible tool schemas for chat.completions."""
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "Read a UTF-8 text file from the project (line-numbered output). "
                    "When you have already located the relevant function or class via grep "
                    "or summarize_file, prefer a single comprehensive read covering the entire "
                    "definition plus surrounding context (typically 80-200 lines) rather than "
                    "multiple small overlapping windows. Use offset/limit only for genuinely "
                    "narrow slices."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Project-relative path"},
                        "offset": {"type": "integer", "description": "0-based start line"},
                        "limit": {"type": "integer", "description": "Max lines to return"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "summarize_file",
                "description": "Compact structural summary of a file (focus on signatures/imports/classes/functions). Cheaper than read_file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Project-relative path"},
                        "focus": {
                            "type": "string",
                            "description": "What to extract: 'all' (default), 'signatures', 'imports', 'classes', 'functions'",
                            "enum": ["all", "signatures", "imports", "classes", "functions"]
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Create or overwrite a file with the given content. Marks file dirty for sync.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "Replace exact substring in a file. Errors if old_string is not unique unless replace_all=true.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "List immediate children of a directory.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "default '.'"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "glob",
                "description": "Recursive glob. Pattern uses fnmatch syntax against project-relative posix paths.",
                "parameters": {
                    "type": "object",
                    "properties": {"pattern": {"type": "string"}},
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "grep",
                "description": "Recursive regex search across project files. Returns 'path:line: match'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Python regex"},
                        "glob": {"type": "string", "description": "Optional fnmatch filter"},
                        "case_insensitive": {"type": "boolean"},
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": (
                    "Run a shell command in the project root. Must honestly declare `intent` "
                    "(read-only / modifies-project / modifies-system / network). "
                    "Lying about intent is a serious bug."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "intent": {
                            "type": "string",
                            "enum": ["read-only", "modifies-project", "modifies-system", "network"],
                            "description": "Honest category of what the command does.",
                        },
                        "timeout": {"type": "integer", "description": "Seconds, default 60"},
                    },
                    "required": ["command", "intent"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_project",
                "description": "Hybrid RAG search over the project's codebase. Returns top matching chunks + filenames.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "description": "default 10"},
                        "retrieval_mode": {
                            "type": "string",
                            "enum": ["hybrid", "semantic", "keyword"],
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the live web (current docs, versions, errors outside project). Prefer search_project for in-project info.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural language query"},
                        "allowed_domains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional whitelist of domains to search.",
                        },
                        "excluded_domains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional blacklist of domains to skip.",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "x_search",
                "description": "Search X (Twitter) posts for real-time chatter or announcements. Prefer web_search for stable docs.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural language query about X posts"},
                        "allowed_x_handles": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Restrict to these handles (max 10).",
                        },
                        "excluded_x_handles": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Exclude these handles (max 10).",
                        },
                        "from_date": {"type": "string", "description": "ISO8601 start date (e.g. 2025-01-01)"},
                        "to_date": {"type": "string", "description": "ISO8601 end date"},
                        "enable_image_understanding": {"type": "boolean"},
                        "enable_video_understanding": {"type": "boolean"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "plugin_search",
                "description": "Search subscribed plugins by intent. Returns matches with id/score/risk. Use plugin_get to read full docs.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "description": "What data or action you need (e.g. current weather, recent filings).",
                        },
                    },
                    "required": ["intent"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "plugin_get",
                "description": "Read full markdown of a subscribed plugin (endpoints, auth, examples).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Plugin id (from plugin_search results)"},
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "plan_note",
                "description": "PLAN-MODE ONLY. Append a note to the persistent scratchpad (.xli/plan-notes.md).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Note text to append (timestamp added automatically).",
                        },
                        "return_notes_after": {
                            "type": "boolean",
                            "description": "If true, return full updated scratchpad instead of just line count.",
                        },
                    },
                    "required": ["text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_plan_notes",
                "description": "PLAN-MODE ONLY. Read the current content of .xli/plan-notes.md scratchpad.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "code_execute",
                "description": "Execute Python in xAI sandbox (NumPy/Pandas/etc preinstalled). For project code use bash instead.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "Description of the computation, or Python code to run.",
                        },
                    },
                    "required": ["task"],
                },
            },
        },
    ]


def worker_tool_schemas() -> list[dict]:
    """Schemas for workers — same as main minus write_file/edit_file."""
    blocked = {"write_file", "edit_file"}
    return [s for s in tool_schemas() if s["function"]["name"] not in blocked]


def plan_mode_schemas() -> list[dict]:
    """Schemas available in plan mode — read-only investigation only."""
    return [s for s in tool_schemas() if s["function"]["name"] in PLAN_MODE_TOOLS]


def dispatch_subagent_schema() -> dict:
    """Schema for the dispatch_subagent tool — only available to the main agent."""
    return {
        "type": "function",
        "function": {
            "name": "dispatch_subagent",
            "description": (
                "Dispatch a read-only worker agent. Workers run in parallel, see only the brief, "
                "can use search/read tools but cannot modify files. Returns worker summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The investigation task. Be specific.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional snippets, file excerpts, or background the worker needs.",
                    },
                },
                "required": ["task"],
            },
        },
    }
