"""Plugin management — markdown files describing external APIs/services.

A plugin is a markdown file with YAML-ish frontmatter at
`~/.config/xli/plugins/<id>.md`. The frontmatter holds machine-readable
metadata (id, name, description, categories, risk, auth_type, env vars);
the body holds prose docs + curl examples that the agent reads to figure
out how to actually invoke the API.

Subscription is per-project (or per-persona): `<project>/.xli/plugins.txt`
lists active plugin IDs, one per line. The agent only sees subscribed
plugins via `plugin_search` and `plugin_get` — keeps the active set
bounded as the catalog grows.

This module ships the L1 ("read-and-bash") tier: agent reads the plugin
doc, composes its own curl via the bash tool. No `plugin_call` RPC, no
auto-invocation. Add encryption/wizard/bulk-import in later passes.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Optional

from xli.config import GLOBAL_CONFIG_DIR

PLUGINS_DIR = GLOBAL_CONFIG_DIR / "plugins"

# Per-project / per-persona subscription file lives at:
#   <project_root>/.xli/plugins.txt
# One plugin id per line; comment lines start with #.
SUBSCRIPTION_FILENAME = "plugins.txt"

# Validation — plugin IDs go on disk as filenames; keep them tame.
_VALID_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def is_valid_id(name: str) -> bool:
    return bool(_VALID_ID.match(name))


# Default risk levels — see proposal for semantics.
RISK_LOW = "low"        # read-only public APIs
RISK_MEDIUM = "medium"  # credentialed reads, rate-limited writes
RISK_HIGH = "high"      # public-facing writes (post tweet, send DM, etc.)
VALID_RISKS = {RISK_LOW, RISK_MEDIUM, RISK_HIGH}


# Starter template — opens in $EDITOR when the user runs `xli plugin --new`.
# The example is a real-ish weather API so the user sees the expected shape.
DEFAULT_PLUGIN_TEMPLATE = """---
id: <fill-in>
name: <Display Name>
description: <one-line summary of what this API does>
categories: [misc]
risk: low
auth_type: query_param
auth_env_vars:
  - <ENV_VAR_NAME>
---

# <Display Name>

<Prose description: what this API does, free vs paid tier, rate limits, etc.>

## Auth setup

<How to get credentials — link to signup page if free, or set up steps for paid.>

Store the key in the encrypted vault — the bash tool injects it into curl
calls automatically when this plugin is subscribed and the command references
the variable:

```bash
xli auth set <plugin-id> <ENV_VAR_NAME>=<your-key>
```

## Usage

### Action 1 — short verb describing what this call does

```bash
curl "https://api.example.com/v1/endpoint?param={PARAM}&key=${ENV_VAR_NAME}"
```

Parameters:
- `PARAM`: <what the agent should fill in here>

### Action 2

```bash
curl "https://api.example.com/v1/other?param={PARAM}" \\
  -H "Authorization: Bearer ${ENV_VAR_NAME}"
```

## Response shape

<JSON / XML / etc., briefly. Link to upstream docs for full schema.>

## Cost / rate limits

<Free tier: N requests/min. Paid: $X/M calls. Etc.>
"""


# --------------------------------------------------------------------------- #
#  Frontmatter parser
# --------------------------------------------------------------------------- #

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML-ish frontmatter from a markdown file.

    Supports the subset we need: `key: value` strings, inline lists
    (`key: [a, b, c]`), and multi-line lists (`key:\\n  - a\\n  - b`).

    Returns `(metadata_dict, body)`. If no frontmatter is present, returns
    `({}, full_text)`. Malformed frontmatter falls back to empty metadata.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ({}, text)

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return ({}, text)  # unterminated, treat as no frontmatter

    fm_lines = lines[1:end]
    body = "\n".join(lines[end + 1:])

    metadata: dict = {}
    current_list: Optional[list] = None

    for raw in fm_lines:
        stripped = raw.lstrip()
        if not stripped or stripped.startswith("#"):
            continue

        # List item — must be inside a multi-line list context.
        if stripped.startswith("- "):
            if current_list is None:
                continue
            current_list.append(stripped[2:].strip())
            continue

        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()

        if not value:
            # Multi-line value — start a list, attach it to the key.
            current_list = []
            metadata[key] = current_list
            continue

        if value.startswith("[") and value.endswith("]"):
            # Inline list: `[a, b, c]`.
            inner = value[1:-1]
            metadata[key] = [item.strip() for item in inner.split(",") if item.strip()]
            current_list = None
            continue

        metadata[key] = value
        current_list = None

    return (metadata, body.lstrip("\n"))


# --------------------------------------------------------------------------- #
#  Plugin
# --------------------------------------------------------------------------- #

@dataclass
class Plugin:
    """One plugin loaded from disk. Lazy: parses frontmatter on demand."""
    id: str

    @property
    def path(self) -> Path:
        return PLUGINS_DIR / f"{self.id}.md"

    def exists(self) -> bool:
        return self.path.exists()

    def read_raw(self) -> str:
        """Read the .md file. Returns empty string on missing or unreadable file
        (callers should prefer .exists() first for strict checks)."""
        return self._raw

    @cached_property
    def _raw(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError, OSError):
            return ""
        except Exception:
            # Unexpected encoding or other I/O error — degrade gracefully
            return ""

    @cached_property
    def parsed(self) -> tuple[dict, str]:
        return parse_frontmatter(self.read_raw())

    def metadata(self) -> dict:
        """Parse frontmatter metadata. Uses YAML for plugins with actions:
        blocks (the simple parser can't handle nested dicts), falls back
        to the lightweight parser otherwise."""
        raw = self.read_raw()
        lines = raw.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i
                break
        if end is None:
            return {}
        fm_text = "\n".join(lines[1:end])
        # If there's an actions: block, use YAML for correct nested parsing.
        if "\nactions:" in fm_text or fm_text.startswith("actions:"):
            try:
                import yaml
                meta = yaml.safe_load(fm_text)
                return meta if isinstance(meta, dict) else {}
            except Exception:
                pass
        return self.parsed[0]

    def manifest(self):
        """Structured action manifest, or None for legacy plugins."""
        from xli.plugin_manifest import parse_manifest
        return parse_manifest(self.read_raw())

    def body(self) -> str:
        return self.parsed[1]

    def name(self) -> str:
        return self.metadata().get("name") or self.id

    def description(self) -> str:
        return self.metadata().get("description") or ""

    def categories(self) -> list[str]:
        v = self.metadata().get("categories") or []
        return v if isinstance(v, list) else [str(v)]

    def risk(self) -> str:
        r = self.metadata().get("risk", RISK_LOW)
        return r if r in VALID_RISKS else RISK_LOW

    def auth_env_vars(self) -> list[str]:
        v = self.metadata().get("auth_env_vars") or []
        return v if isinstance(v, list) else [str(v)]

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


def list_plugins() -> list[Plugin]:
    """All installed plugins (the global catalog), sorted by id."""
    if not PLUGINS_DIR.exists():
        return []
    return [Plugin(id=p.stem) for p in sorted(PLUGINS_DIR.glob("*.md"))]


def create_plugin(plugin_id: str, *, content: Optional[str] = None) -> Plugin:
    if not is_valid_id(plugin_id):
        raise ValueError(
            f"invalid plugin id: {plugin_id!r}. Use letters, digits, _ . - only "
            "(start with letter/digit; max 64 chars)."
        )
    p = Plugin(id=plugin_id)
    if p.exists():
        raise FileExistsError(f"plugin {plugin_id!r} already exists at {p.path}")
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    text = content if content is not None else DEFAULT_PLUGIN_TEMPLATE.replace(
        "<fill-in>", plugin_id
    )
    p.path.write_text(text)
    return p


def delete_plugin(plugin_id: str) -> bool:
    p = Plugin(id=plugin_id)
    if not p.exists():
        return False
    p.path.unlink()
    return True


def open_in_editor(path: Path) -> int:
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    try:
        return subprocess.call([editor, str(path)])
    except FileNotFoundError:
        return subprocess.call(["vi", str(path)])


# --------------------------------------------------------------------------- #
#  Stock plugin pack — opt-in seed catalog shipped with the package
# --------------------------------------------------------------------------- #

def list_stock_plugins() -> list[tuple[str, str]]:
    """Return [(plugin_id, markdown_content)] for every stock plugin shipped
    in xli/stock_plugins/. Sorted by id for deterministic install order.

    Read via importlib.resources so this works the same in editable installs
    and in wheel installs (the .md files are declared as package data in
    pyproject.toml).
    """
    from importlib.resources import files
    out: list[tuple[str, str]] = []
    pkg = files("xli.stock_plugins")
    for entry in sorted(pkg.iterdir(), key=lambda p: p.name):
        if entry.name.endswith(".md"):
            out.append((entry.name[:-3], entry.read_text()))
    return out


def install_stock_plugins(*, force: bool = False) -> tuple[list[str], list[str]]:
    """Copy stock plugins into PLUGINS_DIR. Returns (installed, skipped).

    `force=False` (default) preserves any plugin the user has already edited
    — only missing plugin ids get written. `force=True` overwrites unconditionally
    (intended for upgrading the seed pack after the user has installed an old version).
    """
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    skipped: list[str] = []
    for pid, content in list_stock_plugins():
        dest = PLUGINS_DIR / f"{pid}.md"
        if dest.exists() and not force:
            skipped.append(pid)
            continue
        dest.write_text(content)
        installed.append(pid)
    return (installed, skipped)


# --------------------------------------------------------------------------- #
#  Subscription file (per-project)
# --------------------------------------------------------------------------- #

def load_subscriptions(xli_dir: Path) -> list[str]:
    """Read subscribed plugin IDs from `<xli_dir>/plugins.txt`.
    One id per line; '#' starts a comment. Whitespace stripped.
    Returns [] if the file doesn't exist.
    """
    f = xli_dir / SUBSCRIPTION_FILENAME
    if not f.exists():
        return []
    out: list[str] = []
    seen: set[str] = set()
    try:
        for raw in f.read_text().splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or line in seen:
                continue
            if not is_valid_id(line):
                continue
            seen.add(line)
            out.append(line)
    except OSError:
        return []
    return out


def save_subscriptions(xli_dir: Path, plugin_ids: list[str]) -> Path:
    """Persist subscribed plugin IDs. Returns the file path."""
    xli_dir.mkdir(parents=True, exist_ok=True)
    f = xli_dir / SUBSCRIPTION_FILENAME
    header = (
        "# xli plugin subscriptions for this project — one plugin id per line.\n"
        "# Manage with `/lib subscribe <id>` / `/lib unsubscribe <id>` from any REPL.\n\n"
    )
    body = "\n".join(plugin_ids) + ("\n" if plugin_ids else "")
    f.write_text(header + body)
    return f


def add_subscription(xli_dir: Path, plugin_id: str) -> bool:
    """Add `plugin_id` to subscriptions if not already there. Returns True
    if added, False if already subscribed."""
    current = load_subscriptions(xli_dir)
    if plugin_id in current:
        return False
    current.append(plugin_id)
    save_subscriptions(xli_dir, current)
    return True


def remove_subscription(xli_dir: Path, plugin_id: str) -> bool:
    """Remove `plugin_id` from subscriptions. Returns True if removed,
    False if it wasn't subscribed."""
    current = load_subscriptions(xli_dir)
    if plugin_id not in current:
        return False
    current = [p for p in current if p != plugin_id]
    save_subscriptions(xli_dir, current)
    return True


def subscribed_plugins(xli_dir: Path) -> list[Plugin]:
    """Resolve subscribed plugin IDs to Plugin objects; silently skip ones
    that no longer exist on disk (orphan subscription)."""
    out = []
    for pid in load_subscriptions(xli_dir):
        p = Plugin(id=pid)
        if p.exists():
            out.append(p)
    return out


# --------------------------------------------------------------------------- #
#  Search (the core of /get and plugin_search)
# --------------------------------------------------------------------------- #

# Marker the agent sees in `plugin_search` results when nothing matches.
# Hard-coded so the model can be system-prompted to recognize and report it
# rather than fabricate plugin output.
NO_PLUGIN_MATCH_MARKER = "NO_PLUGIN_MATCH"


_STOPWORDS = {
    "a", "an", "and", "or", "but", "the", "to", "of", "in", "on", "at",
    "for", "from", "with", "by", "is", "are", "was", "were", "be", "been",
    "i", "me", "my", "you", "your", "we", "our", "this", "that", "these",
    "those", "it", "its", "do", "does", "did", "have", "has", "had",
    "get", "find", "send", "post", "show", "tell", "give", "make",
    "what", "where", "when", "who", "how", "why",
}


def search_plugins(intent: str, plugins: list[Plugin], limit: int = 5) -> list[tuple[Plugin, float]]:
    """Score subscribed plugins against an intent string and return top N.

    L1 implementation: lowercase keyword overlap weighted by category and
    description matches. Crude but works for tens of plugins. At scale (200+)
    swap for a real RAG over a per-user catalog Collection — flagged in the
    proposal under "Scale considerations."

    Tokens of length < 3 and common stopwords are dropped — otherwise "a"
    and "the" cause spurious matches against any description containing them.
    """
    if not plugins or not intent.strip():
        return []
    tokens = [
        t.lower() for t in re.findall(r"[a-zA-Z0-9]+", intent)
        if len(t) >= 3 and t.lower() not in _STOPWORDS
    ]
    if not tokens:
        return []

    scored: list[tuple[Plugin, float]] = []
    for p in plugins:
        try:
            meta = p.metadata()
        except OSError:
            continue
        haystack_parts = [
            p.id.lower(),
            (meta.get("name") or "").lower(),
            (meta.get("description") or "").lower(),
            " ".join(meta.get("categories") or []).lower(),
        ]
        # Include action ids, descriptions, AND param names + descriptions
        # in the haystack. Param descriptions often contain example values
        # (e.g. "bitcoin, ethereum" in coingecko price.ids) that the
        # plugin-level description doesn't mention.
        action_text = ""
        manifest = p.manifest()
        if manifest:
            parts = []
            for a in manifest.actions:
                parts.append(a.id)
                parts.append(a.description)
                if a.response_shape:
                    parts.append(a.response_shape)
                for pname, pspec in a.params.items():
                    parts.append(pname)
                    if pspec.description:
                        parts.append(pspec.description)
            action_text = " ".join(parts).lower()
        haystack = " ".join(haystack_parts) + " " + action_text
        if not haystack.strip():
            continue
        # Score: count of tokens that appear, weighted by where they hit.
        score = 0.0
        for tok in tokens:
            if tok in haystack_parts[0]:  # id match
                score += 3.0
            elif tok in haystack_parts[1]:  # name match
                score += 2.5
            elif tok in haystack_parts[3]:  # category match
                score += 2.0
            elif tok in haystack_parts[2]:  # description match
                score += 1.0
            elif tok in action_text:       # action-level match
                score += 1.5
        if score > 0:
            scored.append((p, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]
