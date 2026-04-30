"""Persona management for `xli chat`.

A persona is a named conversational personality with:
  - A system prompt at ~/.config/xli/personas/<name>.md  (config — hand-editable)
  - A backing project at ~/.xli/chat/<name>/             (state — Collection-synced)

The two locations follow Linux convention: config separate from state. The
project dir is a normal XLI project (with .xli/project.json, manifest, and a
remote Collection) so all the existing sync / RAG machinery applies. Each
persona's conversation turns live as `turns/<ts>.md` files inside the project
and sync to the Collection — that's the long-term searchable memory.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from xli.config import GLOBAL_CONFIG_DIR

PERSONAS_DIR = GLOBAL_CONFIG_DIR / "personas"
CHAT_STATE_DIR = Path.home() / ".xli" / "chat"

# Used both as the prompt for the auto-created `default` persona AND as the
# template the editor opens with for `xli chat --new <name>`.
DEFAULT_PROMPT = """You are a helpful general-purpose assistant.

Be terse, accurate, and useful. State results, don't predict them. If you don't
know something, say so — don't fabricate. When the user asks for code or runs a
real-world task, prefer to actually do it (with the tools available) over
describing what you would do.

Edit this file in $EDITOR to change the personality. The system prompt is
appended with a small fixed footer telling you about your tools and memory."""

# Appended to every persona's prompt at chat-start so the model knows what
# tools and memory it has. Kept short — the persona prompt is the user-facing
# voice; this is the operational reality.
TOOL_FOOTER = """

---

You have read-only access to your conversation memory with this user via the
`search_project` tool — it RAG-searches every prior turn that has been synced.
Use it when context from past sessions might help. The most recent turns are
already loaded inline in this conversation.

You also have web_search, x_search, code_execute (xAI server-side), and the
standard file ops (read_file/write_file/edit_file/list_dir/glob/grep/bash)
scoped to this persona's working directory. New files you create persist
across sessions and become searchable memory."""


# Persona names go on disk as filenames; keep them tame.
_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def is_valid_name(name: str) -> bool:
    return bool(_VALID_NAME.match(name))


@dataclass
class Persona:
    name: str

    @property
    def prompt_path(self) -> Path:
        return PERSONAS_DIR / f"{self.name}.md"

    @property
    def project_root(self) -> Path:
        """Working directory + project root for this persona's chat."""
        return CHAT_STATE_DIR / self.name

    @property
    def turns_dir(self) -> Path:
        return self.project_root / "turns"

    def exists(self) -> bool:
        return self.prompt_path.exists()

    def read_prompt(self) -> str:
        return self.prompt_path.read_text()

    def write_prompt(self, text: str) -> None:
        PERSONAS_DIR.mkdir(parents=True, exist_ok=True)
        self.prompt_path.write_text(text)

    def system_prompt(self) -> str:
        """Persona prompt + the fixed tool/memory footer."""
        return self.read_prompt().rstrip() + TOOL_FOOTER

    def first_line(self) -> str:
        """One-line summary of the prompt for `--list`."""
        for line in self.read_prompt().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line[:100]
        return "(empty)"

    def collection_id(self) -> Optional[str]:
        """Return this persona's xAI collection_id, or None if not yet
        initialized. The persona project (and its Collection) are created
        lazily on first `xli chat <name>` — until then there's nothing to
        attach to."""
        proj_json = self.project_root / ".xli" / "project.json"
        if not proj_json.exists():
            return None
        import json
        try:
            data = json.loads(proj_json.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        cid = data.get("collection_id")
        return cid if cid else None

    def touch_used(self) -> None:
        """Record that this persona was used most recently. Used for naked
        `xli chat` (no name) to pick the right persona."""
        marker = PERSONAS_DIR / ".last-used"
        PERSONAS_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text(self.name)


def list_personas() -> list[Persona]:
    if not PERSONAS_DIR.exists():
        return []
    out = []
    for p in sorted(PERSONAS_DIR.glob("*.md")):
        out.append(Persona(name=p.stem))
    return out


def last_used() -> Optional[Persona]:
    marker = PERSONAS_DIR / ".last-used"
    if not marker.exists():
        return None
    name = marker.read_text().strip()
    if not name or not is_valid_name(name):
        return None
    p = Persona(name)
    return p if p.exists() else None


def open_in_editor(path: Path) -> int:
    """Open `path` in $EDITOR (or vi as fallback). Returns exit status.

    Caller is responsible for ensuring the file exists with starter content
    before calling this."""
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    try:
        return subprocess.call([editor, str(path)])
    except FileNotFoundError:
        # Editor not installed — fall back to vi which is universally present.
        return subprocess.call(["vi", str(path)])


def create_persona(name: str, *, prompt: Optional[str] = None) -> Persona:
    """Create a new persona file with the given prompt (or DEFAULT_PROMPT).
    Does NOT init the project dir — caller does that next."""
    if not is_valid_name(name):
        raise ValueError(
            f"invalid persona name: {name!r}. Use letters, digits, _ . - only "
            "(start with letter/digit; max 64 chars)."
        )
    p = Persona(name)
    if p.exists():
        raise FileExistsError(f"persona {name!r} already exists at {p.prompt_path}")
    p.write_prompt(prompt if prompt is not None else DEFAULT_PROMPT)
    return p


def delete_persona(name: str) -> tuple[bool, bool]:
    """Remove the prompt file and the project state dir.

    Returns (prompt_removed, state_removed). Caller is responsible for
    deleting the remote Collection (use the project's collection_id and
    `xli gc` style cleanup).
    """
    import shutil
    p = Persona(name)
    prompt_removed = False
    state_removed = False
    if p.prompt_path.exists():
        p.prompt_path.unlink()
        prompt_removed = True
    if p.project_root.exists():
        shutil.rmtree(p.project_root)
        state_removed = True
    return prompt_removed, state_removed
