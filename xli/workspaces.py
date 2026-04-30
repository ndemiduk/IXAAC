"""Workspace registry — directories where xli has been invoked or that the
user has explicitly registered for reference.

Distinct from `xli/registry.py`, which tracks xli-initialized projects with
cloud Collections (used by `xli gc`). `Workspaces` is a broader concept: it
also includes non-iXaac directories the user wants addressable (e.g. read-only
references, archived projects). The XMPP daemon (Phase 2) reads this to know
where to dispatch agent runs when a phone message doesn't name a verb.

Stored at ~/.config/xli/workspaces.json.

A workspace has a `kind`:
- "project"  — actively maintained, eligible for default agent fallback
- "snapshot" — archived / read-only reference; addressable by alias only,
                excluded from "most-recent-project" rotation
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from xli.config import GLOBAL_CONFIG_DIR

WORKSPACES_FILE = GLOBAL_CONFIG_DIR / "workspaces.json"

KIND_PROJECT = "project"
KIND_SNAPSHOT = "snapshot"
VALID_KINDS = {KIND_PROJECT, KIND_SNAPSHOT}

# Auto-touch skips these — running xli from these would just pollute the
# registry. Add new ones if a real-world session shows further noise.
_SKIP_PATHS = {
    Path.home().resolve(),
    Path("/").resolve(),
    Path("/tmp").resolve(),
    Path("/var/tmp").resolve(),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _canonical(path: Path | str) -> str:
    return str(Path(path).expanduser().resolve())


@dataclass
class WorkspaceEntry:
    path: str               # absolute, resolved
    kind: str               # "project" | "snapshot"
    alias: Optional[str]    # short name; unique across registry
    first_seen: str         # ISO 8601 with offset
    last_active: str        # ISO 8601 with offset
    notes: Optional[str] = None


@dataclass
class Workspaces:
    entries: list[WorkspaceEntry] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Workspaces":
        if not WORKSPACES_FILE.exists():
            return cls()
        try:
            data = json.loads(WORKSPACES_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls(entries=[WorkspaceEntry(**e) for e in data.get("entries", [])])

    def save(self) -> None:
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        WORKSPACES_FILE.write_text(
            json.dumps(
                {"entries": [asdict(e) for e in self.entries]},
                indent=2,
                sort_keys=True,
            )
        )
        try:
            WORKSPACES_FILE.chmod(0o600)
        except OSError:
            pass

    def find_by_path(self, path: str) -> Optional[WorkspaceEntry]:
        return next((e for e in self.entries if e.path == path), None)

    def find_by_alias(self, alias: str) -> Optional[WorkspaceEntry]:
        if not alias:
            return None
        return next((e for e in self.entries if e.alias == alias), None)

    def find(self, key: str) -> Optional[WorkspaceEntry]:
        """Look up by alias first, then by canonicalized path."""
        return self.find_by_alias(key) or self.find_by_path(_canonical(key))

    def projects(self) -> list[WorkspaceEntry]:
        return [e for e in self.entries if e.kind == KIND_PROJECT]

    def snapshots(self) -> list[WorkspaceEntry]:
        return [e for e in self.entries if e.kind == KIND_SNAPSHOT]

    def most_recent_project(self) -> Optional[WorkspaceEntry]:
        """The default target for agent-fallback dispatch in the XMPP daemon."""
        # Parse to datetime so mixed offset forms (e.g. "+00:00" vs "Z") sort
        # by actual instant, not lexicographic-on-string.
        def _key(e: WorkspaceEntry) -> datetime:
            try:
                return datetime.fromisoformat(e.last_active)
            except ValueError:
                return datetime.min.replace(tzinfo=timezone.utc)
        ps = sorted(self.projects(), key=_key, reverse=True)
        return ps[0] if ps else None


# --------------------------------------------------------------------------- #
#  Mutators
# --------------------------------------------------------------------------- #

def touch(path: Path | str, *, kind: str = KIND_PROJECT) -> Optional[WorkspaceEntry]:
    """Record activity in `path`. Creates an entry if missing, bumps
    `last_active` if present. Skips known noise paths and non-directories.
    Returns the entry, or None if skipped.

    Called at the top of every xli invocation that operates on a project.
    Failures are non-fatal — caller should not crash if this returns None.
    """
    p = Path(path).expanduser().resolve()
    if p in _SKIP_PATHS or not p.is_dir():
        return None
    canon = str(p)
    ws = Workspaces.load()
    entry = ws.find_by_path(canon)
    now = _now_iso()
    if entry:
        entry.last_active = now
    else:
        if kind not in VALID_KINDS:
            kind = KIND_PROJECT
        entry = WorkspaceEntry(
            path=canon,
            kind=kind,
            alias=None,
            first_seen=now,
            last_active=now,
            notes=None,
        )
        ws.entries.append(entry)
    ws.save()
    return entry


def add(
    path: Path | str,
    *,
    kind: str = KIND_PROJECT,
    alias: Optional[str] = None,
    notes: Optional[str] = None,
) -> WorkspaceEntry:
    """Explicit registration. Updates an existing entry in place if `path`
    already registered. Alias collisions clear the conflicting entry's alias."""
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise FileNotFoundError(f"not a directory: {p}")
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid kind: {kind!r} (use 'project' or 'snapshot')")
    canon = str(p)
    ws = Workspaces.load()
    entry = ws.find_by_path(canon)
    now = _now_iso()
    if entry:
        entry.kind = kind
        if alias is not None:
            entry.alias = alias
        if notes is not None:
            entry.notes = notes
        entry.last_active = now
    else:
        entry = WorkspaceEntry(
            path=canon,
            kind=kind,
            alias=alias,
            first_seen=now,
            last_active=now,
            notes=notes,
        )
        ws.entries.append(entry)
    if alias:
        for e in ws.entries:
            if e is not entry and e.alias == alias:
                e.alias = None
    ws.save()
    return entry


def set_kind(key: str, kind: str) -> WorkspaceEntry:
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid kind: {kind!r}")
    ws = Workspaces.load()
    entry = ws.find(key)
    if not entry:
        raise KeyError(f"no workspace: {key!r}")
    entry.kind = kind
    ws.save()
    return entry


def set_alias(key: str, alias: Optional[str]) -> WorkspaceEntry:
    ws = Workspaces.load()
    entry = ws.find(key)
    if not entry:
        raise KeyError(f"no workspace: {key!r}")
    if alias:
        for e in ws.entries:
            if e is not entry and e.alias == alias:
                e.alias = None
    entry.alias = alias
    ws.save()
    return entry


def remove(key: str) -> bool:
    ws = Workspaces.load()
    entry = ws.find(key)
    if not entry:
        return False
    ws.entries.remove(entry)
    ws.save()
    return True
