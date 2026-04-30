"""Sync engine — make the xAI Collection mirror the local project."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

from xli.client import Clients, iter_collection_documents
from xli.config import GlobalConfig, ProjectConfig
from xli.ignore import load_ignore_spec, walk_paths_only, walk_project
from xli.manifest import FileEntry, Manifest, hash_file

# Per-op retry on rate limiting. Same policy as bootstrap.create_api_key —
# exponential backoff, give up after a handful of tries.
MAX_RATE_LIMIT_RETRIES = 5

# xAI metadata fields are dict[str, str]; we use these keys.
# These must be declared in the collection's `field_definitions` at create time
# (xAI rejects undeclared fields on update_document — see init_project below).
META_RELPATH = "xli_relpath"
META_SHA256 = "xli_sha256"

# Field schema declared when creating a new collection, so update_document(fields=...)
# is accepted later on. Existing collections that were created without these
# fields fall back to a no-fields update via _is_unknown_field_error.
FIELD_DEFINITIONS = [
    {
        "key": META_RELPATH,
        "required": False,
        "inject_into_chunk": False,
        "unique": False,
        "description": "XLI project-relative path",
    },
    {
        "key": META_SHA256,
        "required": False,
        "inject_into_chunk": False,
        "unique": False,
        "description": "XLI sha256 of file contents",
    },
]


def _is_unknown_field_error(exc: Exception) -> bool:
    """Detect the xAI 'Unknown field' / 'field_definitions' rejection."""
    s = str(exc)
    return "Unknown field" in s or "field_definitions" in s


def _short_error(exc: Exception) -> str:
    """Trim verbose multi-line gRPC errors down to a one-liner.

    xai-sdk surfaces _InactiveRpcError with a 5-line `repr` that includes
    duplicated debug strings. We extract just `details = "..."` (the
    grpc_message) when present, falling back to the first non-empty line.
    """
    s = str(exc)
    # Single-line already? Pass through.
    if "\n" not in s:
        return s
    import re
    m = re.search(r'details\s*=\s*"([^"]+)"', s)
    if m:
        # Surface the gRPC status code too if present, for triage.
        status = re.search(r'status\s*=\s*StatusCode\.(\w+)', s)
        if status:
            return f"{status.group(1)}: {m.group(1)}"
        return m.group(1)
    # Fallback: first non-empty line.
    for line in s.splitlines():
        line = line.strip()
        if line:
            return line
    return s


def _is_rate_limited(exc: Exception) -> bool:
    """Detect a 429 / rate-limit error from xai-sdk.

    The SDK doesn't expose a typed exception for this, so we sniff the message.
    """
    s = str(exc).lower()
    return "429" in s or "rate" in s and "limit" in s or "resource_exhausted" in s


def _with_rate_limit_retry(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run fn(*args, **kwargs); on 429 sleep + retry up to MAX_RATE_LIMIT_RETRIES."""
    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RATE_LIMIT_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if _is_rate_limited(e) and attempt < MAX_RATE_LIMIT_RETRIES - 1:
                wait = 2 ** attempt + 1
                logger.warning(f"rate-limited; backing off {wait}s (attempt {attempt+1})")
                time.sleep(wait)
                continue
            raise
    if last_exc is not None:
        raise last_exc


def _safe_upload(clients, collection_id: str, name: str, data: bytes, fields: dict):
    """upload_document with an undeclared-fields fallback for legacy collections."""
    try:
        return clients.xai.collections.upload_document(
            collection_id=collection_id, name=name, data=data, fields=fields,
        )
    except Exception as e:
        if _is_unknown_field_error(e):
            logger.warning(
                f"collection {collection_id} predates field_definitions — "
                "uploading {name} without metadata fields. Re-init the project "
                "for clean schema-aware sync."
            )
            return clients.xai.collections.upload_document(
                collection_id=collection_id, name=name, data=data, fields=None,
            )
        raise


def _safe_update(clients, collection_id: str, file_id: str, name: str, data: bytes, fields: dict):
    """update_document with an undeclared-fields fallback for legacy collections."""
    try:
        return clients.xai.collections.update_document(
            collection_id=collection_id, file_id=file_id,
            name=name, data=data, fields=fields,
        )
    except Exception as e:
        if _is_unknown_field_error(e):
            return clients.xai.collections.update_document(
                collection_id=collection_id, file_id=file_id,
                name=name, data=data, fields=None,
            )
        raise


@dataclass
class SyncStats:
    uploaded: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"uploaded={self.uploaded} updated={self.updated} "
            f"deleted={self.deleted} unchanged={self.unchanged} failed={self.failed}"
        )


def scan_local(project: ProjectConfig, cfg: GlobalConfig) -> dict[str, FileEntry]:
    """Walk the project, hashing each tracked file."""
    spec = load_ignore_spec(project.project_root, project.extra_ignores)
    out: dict[str, FileEntry] = {}
    for path in walk_project(project.project_root, spec, max_bytes=cfg.max_file_bytes):
        rel = path.relative_to(project.project_root).as_posix()
        try:
            stat = path.stat()
            out[rel] = FileEntry(
                sha256=hash_file(path),
                size=stat.st_size,
                mtime=stat.st_mtime,
            )
        except OSError as e:
            logger.warning(f"skip {rel}: {e}")
    return out


def fetch_collection_state(clients: Clients, collection_id: str) -> dict[str, dict]:
    """Pull every doc in the collection. Returns relpath -> {file_id, sha256, name}.

    Falls back to keying by document name if our metadata fields are missing
    (e.g. docs uploaded by something else).
    """
    state: dict[str, dict] = {}
    for doc in iter_collection_documents(clients.xai, collection_id):
        fm = doc.file_metadata
        # `fields` is a map<string,string> per the DocumentMetadata proto
        meta = dict(doc.fields) if doc.fields else {}
        rel = meta.get(META_RELPATH) or fm.name
        state[rel] = {
            "file_id": fm.file_id,
            "sha256": meta.get(META_SHA256, ""),
            "name": fm.name,
        }
    return state


def _do_upload(clients, collection_id: str, project_root: Path, rel: str, fields: dict) -> str:
    data = (project_root / rel).read_bytes()
    resp = _with_rate_limit_retry(_safe_upload, clients, collection_id, rel, data, fields)
    return resp.file_metadata.file_id


def _do_update(clients, collection_id: str, file_id: str, project_root: Path, rel: str, fields: dict) -> str:
    data = (project_root / rel).read_bytes()
    _with_rate_limit_retry(_safe_update, clients, collection_id, file_id, rel, data, fields)
    return file_id


def _do_delete(clients, collection_id: str, file_id: str) -> None:
    _with_rate_limit_retry(
        clients.xai.collections.remove_document,
        collection_id=collection_id,
        file_id=file_id,
    )


def sync_project(
    clients: Clients,
    project: ProjectConfig,
    cfg: GlobalConfig,
    *,
    dry_run: bool = False,
) -> SyncStats:
    """Reconcile the collection to match local files.

    Algorithm: for each local file, if the remote sha256 matches we skip;
    if it differs we update; if remote is missing we upload. Anything in the
    collection that has no local counterpart is removed. Mutating operations
    fan out across cfg.max_parallel_workers threads with 429 backoff per op.

    Local-only projects skip the network entirely and rebuild the optional
    file index in-place — the source of truth is always the local tree.
    """
    if project.local_only:
        if (project.xli_dir / "index.txt").exists() and not dry_run:
            write_file_index(project, cfg)
        return SyncStats()
    stats = SyncStats()
    manifest = Manifest.load(project.manifest_path)
    local = scan_local(project, cfg)
    remote = fetch_collection_state(clients, project.collection_id)

    # Phase 1: classify each local file against remote state. File bytes are
    # NOT read here — each upload/update task reads its own file inside the
    # thread pool so peak memory stays bounded to max_workers * max_file_bytes
    # rather than total_changed_files * max_file_bytes.
    uploads: list[tuple[str, FileEntry, dict]] = []
    updates: list[tuple[str, FileEntry, str, dict]] = []

    for rel, entry in local.items():
        remote_doc = remote.get(rel)
        if remote_doc and remote_doc["sha256"] == entry.sha256:
            entry.file_id = remote_doc["file_id"]
            entry.last_synced = time.time()
            manifest.set(rel, entry)
            stats.unchanged += 1
            continue

        if dry_run:
            if remote_doc:
                stats.updated += 1
            else:
                stats.uploaded += 1
            continue

        fields = {META_RELPATH: rel, META_SHA256: entry.sha256}
        if remote_doc:
            updates.append((rel, entry, remote_doc["file_id"], fields))
        else:
            uploads.append((rel, entry, fields))

    deletes: list[tuple[str, str]] = [
        (rel, doc["file_id"]) for rel, doc in remote.items() if rel not in local
    ]

    if dry_run:
        stats.deleted = len(deletes)
        return stats

    # Phase 2: fan out mutating operations across a thread pool. Each future
    # returns enough metadata for the main thread to update the manifest and
    # stats — no shared-state locking needed.
    total_ops = len(uploads) + len(updates) + len(deletes)
    if total_ops == 0:
        manifest.save()
        return stats

    max_workers = max(1, min(cfg.max_parallel_workers, total_ops))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs: dict[Any, tuple[str, str, Optional[FileEntry]]] = {}
        for rel, entry, fields in uploads:
            f = ex.submit(_do_upload, clients, project.collection_id, project.project_root, rel, fields)
            futs[f] = ("upload", rel, entry)
        for rel, entry, fid, fields in updates:
            f = ex.submit(_do_update, clients, project.collection_id, fid, project.project_root, rel, fields)
            futs[f] = ("update", rel, entry)
        for rel, fid in deletes:
            f = ex.submit(_do_delete, clients, project.collection_id, fid)
            futs[f] = ("delete", rel, None)

        for fut in as_completed(futs):
            op, rel, entry = futs[fut]
            try:
                result = fut.result()
            except Exception as e:
                stats.failed += 1
                short = _short_error(e)
                stats.errors.append(f"{op} {rel}: {short}")
                logger.error(f"{op} {rel}: {short}")
                continue

            if op == "upload" and entry is not None:
                entry.file_id = result  # file_id from upload response
                entry.last_synced = time.time()
                manifest.set(rel, entry)
                stats.uploaded += 1
            elif op == "update" and entry is not None:
                entry.file_id = result  # same file_id as before
                entry.last_synced = time.time()
                manifest.set(rel, entry)
                stats.updated += 1
            elif op == "delete":
                manifest.remove(rel)
                stats.deleted += 1

    manifest.save()
    return stats


def write_file_index(
    project: ProjectConfig,
    cfg: GlobalConfig,
    *,
    on_progress: Optional[Callable[[int, str], None]] = None,
) -> int:
    """Walk the project tree (respecting ignores) and write
    `<root>/.xli/index.txt` containing every tracked relpath plus its byte size,
    one per line: `<size>\\t<relpath>`. Returns the count.

    Uses `walk_paths_only` — NO content sniffing, NO size cap, NO binary skip.
    A 150k-file NAS over the network can be indexed in ~30 seconds because we
    only stat() each file (one network round-trip per file's metadata, vs the
    8KB read per file that walk_project would do).

    `on_progress(count, last_relpath)` fires every 1000 files so the caller
    can show a live counter — without it, large trees look like the process
    is hung.
    """
    spec = load_ignore_spec(project.project_root, project.extra_ignores)
    rows: list[str] = []
    n = 0
    last_rel = ""
    for path in walk_paths_only(project.project_root, spec):
        rel = path.relative_to(project.project_root).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        rows.append(f"{size}\t{rel}")
        n += 1
        last_rel = rel
        if on_progress is not None and n % 1000 == 0:
            on_progress(n, rel)
    if on_progress is not None and n % 1000 != 0:
        on_progress(n, last_rel)
    project.xli_dir.mkdir(parents=True, exist_ok=True)
    (project.xli_dir / "index.txt").write_text("\n".join(rows) + "\n")
    return len(rows)


def init_project(
    clients: Clients,
    project_root: Path,
    *,
    name: Optional[str] = None,
    existing_collection_id: Optional[str] = None,
    local_only: bool = False,
    snapshot: bool = False,
) -> ProjectConfig:
    """Create the .xli/ directory, create or reuse a collection, register it.

    `local_only=True` skips Collection provisioning entirely — for ad-hoc /
    file-management workflows where you don't want any content uploaded.
    `snapshot=True` writes a paths+sizes index at .xli/index.txt for fast
    grep-based structural search (most useful in local-only mode on big trees).
    """
    import uuid
    from datetime import datetime, timezone

    from xli.registry import Registry, RegistryEntry

    project_root = project_root.resolve()
    name = name or project_root.name

    if local_only:
        coll_id = ""
    elif existing_collection_id:
        coll_id = existing_collection_id
    else:
        meta = clients.xai.collections.create(
            name=f"xli/{name}",
            field_definitions=FIELD_DEFINITIONS,
        )
        coll_id = meta.collection_id

    created_at = datetime.now(timezone.utc).isoformat()
    project = ProjectConfig(
        project_root=project_root,
        name=name,
        collection_id=coll_id,
        created_at=created_at,
        conversation_id=uuid.uuid4().hex,  # stable per-project cache key
        local_only=local_only,
    )
    project.save()

    # Note: snapshot index is written by the caller (cmd_init) so the walk
    # can be wrapped in a live progress widget — on a 150k-file NAS the walk
    # takes long enough that silent execution looks like a hang.
    _ = snapshot  # kept in signature for backward compat; no-op here

    registry = Registry.load()
    registry.upsert(
        RegistryEntry(
            path=str(project_root),
            collection_id=coll_id,
            name=name,
            created_at=created_at,
        )
    )
    registry.save()

    return project
