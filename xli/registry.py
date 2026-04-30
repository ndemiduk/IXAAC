"""Global registry of XLI-initialized projects.

We need this because the xAI Collections API has no idea what's a "live"
project vs an orphaned one. Each `xli init` records (path, collection_id, name)
here so `xli gc` can cross-reference with the cloud and offer to clean up
collections whose local project has been deleted.

Stored at ~/.config/xli/projects.json.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from xli.config import GLOBAL_CONFIG_DIR

REGISTRY_FILE = GLOBAL_CONFIG_DIR / "projects.json"


@dataclass
class RegistryEntry:
    path: str           # absolute project root path at registration time
    collection_id: str
    name: str
    created_at: str


@dataclass
class Registry:
    entries: list[RegistryEntry] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Registry":
        if not REGISTRY_FILE.exists():
            return cls()
        data = json.loads(REGISTRY_FILE.read_text())
        return cls(entries=[RegistryEntry(**e) for e in data.get("entries", [])])

    def save(self) -> None:
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        REGISTRY_FILE.write_text(
            json.dumps(
                {"entries": [asdict(e) for e in self.entries]},
                indent=2,
                sort_keys=True,
            )
        )
        REGISTRY_FILE.chmod(0o600)

    def find_by_path(self, path: Path | str) -> Optional[RegistryEntry]:
        p = str(Path(path).resolve())
        return next((e for e in self.entries if e.path == p), None)

    def find_by_collection(self, collection_id: str) -> Optional[RegistryEntry]:
        # Empty collection_id is the sentinel for local-only projects; matching
        # on it would collide every local-only entry into one registry slot.
        if not collection_id:
            return None
        return next((e for e in self.entries if e.collection_id == collection_id), None)

    def upsert(self, entry: RegistryEntry) -> None:
        existing = self.find_by_path(entry.path) or self.find_by_collection(
            entry.collection_id
        )
        if existing:
            self.entries.remove(existing)
        self.entries.append(entry)

    def remove(self, collection_id: str) -> bool:
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.collection_id != collection_id]
        return len(self.entries) != before
