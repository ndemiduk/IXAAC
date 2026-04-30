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


# Force-include carve-outs — applied LAST so user .gitignore patterns can't
# accidentally re-ignore files iXaac depends on shipping to the Collection.
# Specifically: archived plan investigations need to flow through sync to be
# RAG-searchable. A user's `*.md` or `.xli/` line in .gitignore would otherwise
# silently disable the plans-sync feature.
FORCE_INCLUDE_PATTERNS = [
    "!.xli/plans/",
    "!.xli/plans/**",
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
    # Carve-outs go last — user gitignore can't override these.
    patterns.extend(FORCE_INCLUDE_PATTERNS)
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


def walk_paths_only(project_root: Path, spec: PathSpec) -> Iterator[Path]:
    """Walk every file in the tree, respecting ignores — NO content sniffing,
    NO size cap, NO binary skip.

    For `--snapshot` mode where we just want paths and sizes for grep-based
    structural search. Critical: walk_project's `is_probably_text` opens every
    file to read 8KB, which is O(N) network round-trips on a NAS. This walker
    only stat()s files, so it's roughly as fast as `find`.
    """
    for path in project_root.rglob("*"):
        if not path.is_file():
            continue
        rel_posix = path.relative_to(project_root).as_posix()
        if spec.match_file(rel_posix) or spec.match_file(rel_posix + "/"):
            continue
        yield path
