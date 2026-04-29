"""Walk a project tree, honoring .gitignore + .xliignore + sane defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from pathspec import PathSpec

DEFAULT_IGNORES = [
    # VCS + xli internals
    ".git/",
    ".xli/",
    # Python virtualenvs + caches
    "venv/",
    ".venv/",
    "env/",
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".tox/",
    "*.egg-info/",
    # JS / TS build outputs
    "node_modules/",
    ".next/",
    ".nuxt/",
    ".svelte-kit/",
    ".turbo/",
    ".vercel/",
    ".astro/",
    "out/",
    # Generic build / dist / cache dirs
    "dist/",
    "build/",
    "target/",
    ".cache/",
    "coverage/",
    # OS / editor cruft + secrets + logs
    ".DS_Store",
    "*.log",
    ".env",
    ".env.*",
]


def load_ignore_spec(project_root: Path, extra_patterns: list[str] | None = None) -> PathSpec:
    patterns = list(DEFAULT_IGNORES)
    for fname in (".gitignore", ".xliignore"):
        f = project_root / fname
        if f.exists():
            patterns.extend(
                line for line in f.read_text().splitlines()
                if line and not line.lstrip().startswith("#")
            )
    if extra_patterns:
        patterns.extend(extra_patterns)
    return PathSpec.from_lines("gitwildmatch", patterns)


def is_probably_text(path: Path, sniff_bytes: int = 8192) -> bool:
    """Heuristic: file is text if no NUL byte in the first chunk."""
    try:
        with path.open("rb") as f:
            chunk = f.read(sniff_bytes)
    except OSError:
        return False
    return b"\x00" not in chunk


def walk_project(
    project_root: Path,
    spec: PathSpec,
    *,
    max_bytes: int = 1_000_000,
) -> Iterator[Path]:
    """Yield every file in the project that should be tracked."""
    for path in project_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(project_root)
        rel_posix = rel.as_posix()
        if spec.match_file(rel_posix) or spec.match_file(rel_posix + "/"):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > max_bytes:
            continue
        # xAI Collections rejects empty uploads with "Empty stream received".
        # Skip 0-byte files entirely — they carry no RAG signal anyway.
        if size == 0:
            continue
        if not is_probably_text(path):
            continue
        yield path
