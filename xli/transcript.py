"""Per-turn conversation persistence for `xli chat`.

Each turn is written as a single markdown file under `<project_root>/turns/`,
named with a sortable timestamp. The file contains the user's message and the
assistant's final reply (no tool-call noise — that's investigation detail, not
memory worth preserving). Files sync to the persona's Collection like any
other project file, becoming searchable via `search_project`.

The "last N turns" loaded as inline history at chat-start come from these
same files — read in order, parse out the user/assistant blocks, prepend after
the system prompt so the conversation feels continuous across sessions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Each turn renders as two markdown blocks. The block headers are stable so
# we can parse them back at load-time. Don't change without a migration.
_USER_HEADER = "## user"
_ASSISTANT_HEADER = "## assistant"
_BLOCK_RX = re.compile(
    rf"^{re.escape(_USER_HEADER)}\s*$\n(.+?)\n^{re.escape(_ASSISTANT_HEADER)}\s*$\n(.+)\Z",
    re.DOTALL | re.MULTILINE,
)


@dataclass
class Turn:
    timestamp: str  # ISO-ish, sortable
    user: str
    assistant: str

    def to_markdown(self) -> str:
        return (
            f"# turn — {self.timestamp}\n\n"
            f"{_USER_HEADER}\n{self.user}\n\n"
            f"{_ASSISTANT_HEADER}\n{self.assistant}\n"
        )

    def as_history_pair(self) -> list[dict]:
        """Two history entries (user + assistant) in the chat-completions shape."""
        return [
            {"role": "user", "content": self.user},
            {"role": "assistant", "content": self.assistant},
        ]


def turn_filename(ts: datetime, n: int) -> str:
    """Sortable + readable: 20260429T123456Z-042.md."""
    return ts.strftime("%Y%m%dT%H%M%SZ") + f"-{n:04d}.md"


def write_turn(turns_dir: Path, user: str, assistant: str) -> Path:
    """Append a new turn file. Returns the file path so the caller can mark
    it dirty for sync. Numbering is "next free slot" — counts existing files."""
    turns_dir.mkdir(parents=True, exist_ok=True)
    n = sum(1 for _ in turns_dir.glob("*.md")) + 1
    ts = datetime.now(timezone.utc)
    iso = ts.strftime("%Y-%m-%d %H:%M:%S UTC")
    turn = Turn(timestamp=iso, user=user.strip(), assistant=assistant.strip())
    path = turns_dir / turn_filename(ts, n)
    path.write_text(turn.to_markdown())
    return path


def load_recent_turns(turns_dir: Path, limit: int) -> list[Turn]:
    """Read the most recent `limit` turns from disk, in chronological order."""
    if not turns_dir.exists():
        return []
    files = sorted(turns_dir.glob("*.md"))
    if limit > 0:
        files = files[-limit:]
    out: list[Turn] = []
    for f in files:
        try:
            text = f.read_text()
        except OSError:
            continue
        m = _BLOCK_RX.search(text)
        if not m:
            continue
        # First line of the file is "# turn — <timestamp>"; pull the timestamp
        # back out for completeness, fall back to filename.
        first = text.splitlines()[0] if text else ""
        ts = first.split("—", 1)[1].strip() if "—" in first else f.stem
        out.append(Turn(timestamp=ts, user=m.group(1).strip(), assistant=m.group(2).strip()))
    return out


def turns_to_history(turns: list[Turn]) -> list[dict]:
    """Flatten a list of turns into chat-completions history entries."""
    history: list[dict] = []
    for t in turns:
        history.extend(t.as_history_pair())
    return history


def count_turns(turns_dir: Path) -> int:
    return sum(1 for _ in turns_dir.glob("*.md")) if turns_dir.exists() else 0


def clear_turns(turns_dir: Path) -> int:
    """Delete every turn file. Returns count removed. Caller should also
    sync afterward to propagate the deletes to the Collection."""
    if not turns_dir.exists():
        return 0
    n = 0
    for f in turns_dir.glob("*.md"):
        try:
            f.unlink()
            n += 1
        except OSError:
            continue
    return n
