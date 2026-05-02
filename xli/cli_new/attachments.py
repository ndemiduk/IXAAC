"""REPL attachment helpers — refs (personas) and docs.

Used by both the REPL loops (cmd_code, _chat_run_session in repl.py) and the
slash-command handlers (slash_commands.py). Lives at the leaf of the
attachments→slash_commands→repl dep chain so neither layer needs to know
about the other.

State model:
- agent.attached_refs: list[tuple[name, collection_id]]
- agent.attached_docs: list[tuple[name, content]]
- Persistence: .xli/refs.txt, .xli/docs.txt — newline-separated names; the
  full content/collection-id is re-resolved on load.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from rich.console import Console

from xli.agent import Agent
from xli.config import ProjectConfig
from xli.persona import Persona, is_valid_name

console = Console()


def attachment_tag(agent) -> str:
    """Compact prompt-line indicator for /ref + /doc attachments.

    Returns '+1r', '+2d', '+1r/2d', or '' (when nothing is attached). Trailing
    '!' is added when any attached doc exceeds INLINE_SOFT_CAP_BYTES — a
    persistent reminder that the system prompt is heavy on every turn, since
    the at-attach-time warning is easy to forget.
    """
    parts = []
    if agent.attached_refs:
        parts.append(f"{len(agent.attached_refs)}r")
    if agent.attached_docs:
        from xli.doc import INLINE_SOFT_CAP_BYTES
        oversized = any(len(c) > INLINE_SOFT_CAP_BYTES for _, c in agent.attached_docs)
        parts.append(f"{len(agent.attached_docs)}d" + ("!" if oversized else ""))
    return ("+" + "/".join(parts)) if parts else ""


def archive_plan_notes(project: ProjectConfig, *, label: str) -> Optional[Path]:
    """Move .xli/plan-notes.md to .xli/plans/<label>-<timestamp>.md.

    Called on /execute (label='approved'), /cancel (label='cancelled'), and
    when the user opts not to resume an existing scratchpad on /plan
    (label='abandoned'). Returns the archived path, or None if no notes
    existed to archive.
    """
    from datetime import datetime, timezone
    notes_path = project.xli_dir / "plan-notes.md"
    if not notes_path.exists() or notes_path.stat().st_size == 0:
        if notes_path.exists():
            notes_path.unlink()  # empty file leftover, just clean it up
        return None
    archive_dir = project.xli_dir / "plans"
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H-%M-%S")
    dest = archive_dir / f"{label}-{ts}.md"
    notes_path.rename(dest)
    return dest


def load_attached_refs(project: ProjectConfig, agent: Agent) -> None:
    """Load persistent ref attachments from .xli/refs.txt."""
    refs_file = project.xli_dir / 'refs.txt'
    if not refs_file.exists():
        return
    with refs_file.open('r') as f:
        names = [line.strip() for line in f if line.strip()]
    loaded = 0
    for name in names:
        if not is_valid_name(name):
            console.print(f"[yellow]Skipping invalid persona name in refs.txt: {name}[/yellow]")
            continue
        persona = Persona(name)
        if not persona.exists():
            console.print(f"[yellow]Skipping missing persona in refs.txt: {name}[/yellow]")
            continue
        cid = persona.collection_id()
        if not cid:
            console.print(f"[yellow]Skipping persona without collection in refs.txt: {name}[/yellow]")
            continue
        if any(n == name for n, _ in agent.attached_refs):
            continue
        agent.attached_refs.append((name, cid))
        loaded += 1
    if loaded > 0:
        console.print(f"[dim]Loaded {loaded} persistent ref(s) from .xli/refs.txt[/dim]")


def save_attached_refs(project: ProjectConfig, agent: Agent) -> None:
    """Save current ref attachments to .xli/refs.txt for persistence."""
    refs_file = project.xli_dir / 'refs.txt'
    names = sorted(name for name, _ in agent.attached_refs)
    if not names:
        if refs_file.exists():
            refs_file.unlink()
        return
    with refs_file.open('w') as f:
        for name in names:
            f.write(f"{name}\n")


def load_attached_docs(project: ProjectConfig, agent: Agent) -> None:
    """Load persistent doc attachments from .xli/docs.txt."""
    from xli.doc import Doc, is_valid_name as is_valid_doc_name
    docs_file = project.xli_dir / 'docs.txt'
    if not docs_file.exists():
        return
    with docs_file.open('r') as f:
        names = [line.strip() for line in f if line.strip()]
    loaded = 0
    for name in names:
        if not is_valid_doc_name(name):
            console.print(f"[yellow]Skipping invalid doc name in docs.txt: {name}[/yellow]")
            continue
        doc = Doc(name)
        if not doc.exists():
            console.print(f"[yellow]Skipping missing doc in docs.txt: {name}[/yellow]")
            continue
        content = doc.read()
        if any(n == name for n, _ in agent.attached_docs):
            continue
        agent.attached_docs.append((name, content))
        loaded += 1
    if loaded > 0:
        console.print(f"[dim]Loaded {loaded} persistent doc(s) from .xli/docs.txt[/dim]")


def save_attached_docs(project: ProjectConfig, agent: Agent) -> None:
    """Save current doc attachments to .xli/docs.txt for persistence."""
    docs_file = project.xli_dir / 'docs.txt'
    names = sorted(name for name, _ in agent.attached_docs)
    if not names:
        if docs_file.exists():
            docs_file.unlink()
        return
    with docs_file.open('w') as f:
        for name in names:
            f.write(f"{name}\n")
