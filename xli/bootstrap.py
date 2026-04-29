"""One-time bootstrap: provision a swarm of worker API keys via the xAI
Management REST API and persist them into ~/.config/xli/config.json.

Endpoint shape supplied by the user:
    POST   https://management-api.x.ai/auth/teams/{teamId}/api-keys
    GET    https://management-api.x.ai/auth/teams/{teamId}/api-keys
    DELETE https://management-api.x.ai/auth/teams/{teamId}/api-keys/{keyId}
    Authorization: Bearer {management_api_key}

Team ID discovery:
    1. cfg.team_id (from config) takes precedence.
    2. Otherwise, GET /auth/teams and pick the first/only team.
    3. If neither works, ask the user to set team_id in config.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from xli.config import GLOBAL_CONFIG_FILE, GlobalConfig

MANAGEMENT_HOST = "https://management-api.x.ai"

DEFAULT_ACLS = ["api-key:model:*", "api-key:endpoint:*"]
DEFAULT_QPS = 30
DEFAULT_QPM = 2000
DEFAULT_TPM: Optional[int] = None
# Sensible expiry: long enough not to annoy, short enough to cap blast radius.
DEFAULT_EXPIRE_DAYS = 180

# Pacing + retry — provisioning N keys at once gets throttled.
INTER_CREATE_DELAY_SEC = 1.5
MAX_429_RETRIES = 5


def _expire_iso(days: Optional[int]) -> Optional[str]:
    if not days or days <= 0:
        return None
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _extract_api_key_id(resp: dict) -> Optional[str]:
    """Pull the api_key_id (server-side identifier) from a create/list/get response."""
    if not isinstance(resp, dict):
        return None
    for k in ("apiKeyId", "api_key_id", "id"):
        v = resp.get(k)
        if isinstance(v, str) and v:
            return v
    return None


class BootstrapError(RuntimeError):
    pass


def _auth_headers(mgmt_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {mgmt_key}",
        "Content-Type": "application/json",
    }


def _request(method: str, url: str, mgmt_key: str, *, json_body: Optional[dict] = None) -> Any:
    resp = httpx.request(
        method, url,
        headers=_auth_headers(mgmt_key),
        json=json_body,
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise BootstrapError(
            f"{method} {url} → {resp.status_code}\n{resp.text[:500]}"
        )
    if not resp.content:
        return None
    try:
        return resp.json()
    except Exception:
        return resp.text


def discover_team_id(cfg: GlobalConfig) -> str:
    """Resolve the team_id to use for key operations.

    cfg.team_id wins if set. Otherwise fetch GET /auth/teams and pick a team
    (single team → use it; multiple → ask user to set team_id explicitly).
    """
    if cfg.team_id:
        return cfg.team_id
    if not cfg.management_api_key:
        raise BootstrapError("management_api_key not set in config.json")

    data = _request("GET", f"{MANAGEMENT_HOST}/auth/teams", cfg.management_api_key)
    # Tolerate either a list response or an envelope { "teams": [...] }.
    teams = data if isinstance(data, list) else (data or {}).get("teams", [])
    if not teams:
        raise BootstrapError(
            "no teams found via GET /auth/teams. Set `team_id` explicitly in your config."
        )
    if len(teams) > 1:
        names = ", ".join(t.get("name", t.get("team_id", "?")) for t in teams)
        raise BootstrapError(
            f"multiple teams found ({names}). Set `team_id` explicitly in config."
        )
    t = teams[0]
    # xAI Management API uses camelCase; tolerate snake_case too just in case.
    return t.get("teamId") or t.get("team_id") or t.get("id") or ""


def create_api_key(
    mgmt_key: str,
    team_id: str,
    name: str,
    *,
    qps: int = DEFAULT_QPS,
    qpm: int = DEFAULT_QPM,
    tpm: Optional[int] = DEFAULT_TPM,
    expire_days: Optional[int] = DEFAULT_EXPIRE_DAYS,
) -> dict:
    """Create one API key with 429 backoff. Returns the response dict containing
    the newly-minted secret (xAI shows it once on creation only) plus the
    server-side api_key_id (needed for rotate/expire/delete later).
    """
    body: dict[str, Any] = {
        "name": name,
        "acls": DEFAULT_ACLS,
        "qps": qps,
        "qpm": qpm,
    }
    if tpm is not None:
        body["tpm"] = str(tpm)
    expire_iso = _expire_iso(expire_days)
    if expire_iso:
        body["expireTime"] = expire_iso

    url = f"{MANAGEMENT_HOST}/auth/teams/{team_id}/api-keys"
    headers = _auth_headers(mgmt_key)

    for attempt in range(MAX_429_RETRIES):
        resp = httpx.post(url, headers=headers, json=body, timeout=30.0)
        if resp.status_code == 429:
            wait = 2 ** attempt + 1
            time.sleep(wait)
            continue
        if resp.status_code >= 400:
            raise BootstrapError(
                f"POST {url} → {resp.status_code}\n{resp.text[:500]}"
            )
        return resp.json() if resp.content else {}
    raise BootstrapError(f"create_api_key {name}: 429 after {MAX_429_RETRIES} retries")


def rotate_api_key(mgmt_key: str, api_key_id: str) -> dict:
    """Rotate a key's secret in place. Returns response (should contain new secret)."""
    return _request(
        "POST",
        f"{MANAGEMENT_HOST}/auth/api-keys/{api_key_id}/rotate",
        mgmt_key,
    )


def update_api_key_expiration(
    mgmt_key: str, team_id: str, api_key_id: str, expire_days: Optional[int]
) -> dict:
    """Update the expireTime on an existing key. None/0 = no expiry."""
    body: dict[str, Any] = {}
    iso = _expire_iso(expire_days)
    if iso:
        body["expireTime"] = iso
    else:
        body["expireTime"] = None
    return _request(
        "PATCH",
        f"{MANAGEMENT_HOST}/auth/teams/{team_id}/api-keys/{api_key_id}",
        mgmt_key,
        json_body=body,
    )


def list_api_keys(mgmt_key: str, team_id: str) -> list[dict]:
    data = _request(
        "GET",
        f"{MANAGEMENT_HOST}/auth/teams/{team_id}/api-keys",
        mgmt_key,
    )
    if isinstance(data, list):
        return data
    return (data or {}).get("api_keys", []) or (data or {}).get("keys", [])


def delete_api_key(mgmt_key: str, team_id: str, key_id: str) -> None:
    _request(
        "DELETE",
        f"{MANAGEMENT_HOST}/auth/teams/{team_id}/api-keys/{key_id}",
        mgmt_key,
    )


def extract_api_key_string(create_response: dict) -> Optional[str]:
    """Pull the actual secret key out of a create response.

    The exact field name isn't documented to me — try the most likely shapes.
    """
    if not isinstance(create_response, dict):
        return None
    for k in ("api_key", "apiKey", "key", "secret", "token", "value"):
        v = create_response.get(k)
        if isinstance(v, str) and v:
            return v
    # nested envelope: { "api_key": { "value": "..." } } or similar
    nested = create_response.get("api_key")
    if isinstance(nested, dict):
        for k in ("value", "secret", "key"):
            v = nested.get(k)
            if isinstance(v, str) and v:
                return v
    return None


def discover_models(
    mgmt_key: str,
    team_id: str,
    chat_key: Optional[str] = None,
    chat_keys: Optional[list[str]] = None,
) -> list[str]:
    """Return the list of model IDs available via the OpenAI-compatible
    /v1/models endpoint.

    Tries every key in `chat_keys` (or just `chat_key` if that's all we have)
    until one returns a 2xx — pools often have a dead key or two and we
    shouldn't fail discovery just because keys[0] is stale.

    Returns [] if no key works — discovery is a nice-to-have, not load-bearing.
    """
    keys: list[str] = []
    if chat_keys:
        keys.extend(k for k in chat_keys if k)
    elif chat_key:
        keys.append(chat_key)
    if not keys:
        return []

    last_error: Optional[str] = None
    for k in keys:
        try:
            resp = httpx.get(
                "https://api.x.ai/v1/models",
                headers={"Authorization": f"Bearer {k}"},
                timeout=15.0,
            )
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            continue
        if resp.status_code < 400:
            return _normalize_model_list(resp.json())
        last_error = f"{resp.status_code}: {resp.text[:120]}"
    # All keys failed; surface the last error to caller via stderr-only side channel
    # (we still return [] so the caller's "discovery is best-effort" path runs)
    import sys as _sys
    print(f"[xli] model discovery: all {len(keys)} chat key(s) rejected. last error: {last_error}", file=_sys.stderr)
    return []


def _normalize_model_list(data: Any) -> list[str]:
    """Pull model IDs out of any plausible response shape."""
    if not data:
        return []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("data") or data.get("models") or data.get("items") or []
    else:
        return []
    out: list[str] = []
    for item in items:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            mid = item.get("id") or item.get("name") or item.get("model")
            if isinstance(mid, str) and mid:
                out.append(mid)
    return out


def pick_best_models(available: list[str]) -> tuple[Optional[str], Optional[str]]:
    """Heuristic best-of-class picker. Returns (orchestrator, worker).

    Strategy: walk explicit preference tiers, take the first non-empty tier,
    then pick the lexically-latest entry within that tier.

    Tiers err strongly toward STABLE models — an experimental flagship is
    worse than a stable fast model for production use. Users can always
    override via `xli models set`.
    """
    def _is_stable(m: str) -> bool:
        return "experimental" not in m and "beta" not in m

    def _is_reasoning_only(m: str) -> bool:
        return "reasoning" in m and "non-reasoning" not in m

    def _is_non_reasoning(m: str) -> bool:
        return "non-reasoning" in m

    def _is_fast(m: str) -> bool:
        return "fast" in m

    def _is_flagship(m: str) -> bool:
        # Bare model name like `grok-4`, `grok-4.1` — no fast/reasoning suffix
        return not _is_fast(m) and "reasoning" not in m

    # Orchestrator preference — stable non-fast reasoning → stable flagship →
    # stable fast reasoning → any reasoning → any stable non-flagship.
    # Reasoning beats bare flagship: an orchestrator's job is planning/tool
    # selection, where chain-of-thought matters more than raw breadth. (e.g.
    # grok-4.20-reasoning should beat a bare grok-4 for the main agent role.)
    orch_tiers = [
        [m for m in available if _is_stable(m) and _is_reasoning_only(m) and not _is_fast(m)],
        [m for m in available if _is_stable(m) and _is_flagship(m)],
        [m for m in available if _is_stable(m) and _is_reasoning_only(m)],
        [m for m in available if _is_reasoning_only(m)],
        [m for m in available if _is_stable(m) and not _is_non_reasoning(m)],
    ]
    orch = next((sorted(tier, reverse=True)[0] for tier in orch_tiers if tier), None)

    # Worker preference — stable fast non-reasoning → any fast non-reasoning →
    # stable non-reasoning → any non-reasoning.
    worker_tiers = [
        [m for m in available if _is_stable(m) and _is_fast(m) and _is_non_reasoning(m)],
        [m for m in available if _is_fast(m) and _is_non_reasoning(m)],
        [m for m in available if _is_stable(m) and _is_non_reasoning(m)],
        [m for m in available if _is_non_reasoning(m)],
    ]
    worker = next((sorted(tier, reverse=True)[0] for tier in worker_tiers if tier), None)

    return orch, worker


def set_models_in_config(
    orchestrator_model: Optional[str],
    worker_model: Optional[str],
    *,
    auto_detected: bool = False,
) -> Path:
    """Persist model picks. If auto_detected, also stamp models_detected_at."""
    raw = json.loads(GLOBAL_CONFIG_FILE.read_text())
    if orchestrator_model:
        raw["orchestrator_model"] = orchestrator_model
    if worker_model:
        raw["worker_model"] = worker_model
    if auto_detected:
        raw["models_detected_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    GLOBAL_CONFIG_FILE.write_text(json.dumps(raw, indent=2))
    GLOBAL_CONFIG_FILE.chmod(0o600)
    return GLOBAL_CONFIG_FILE


def set_team_id_in_config(team_id: str) -> Path:
    """Cache a discovered team_id back into config.json so future bootstrap
    calls skip the discovery round-trip."""
    raw = json.loads(GLOBAL_CONFIG_FILE.read_text())
    raw["team_id"] = team_id
    GLOBAL_CONFIG_FILE.write_text(json.dumps(raw, indent=2))
    GLOBAL_CONFIG_FILE.chmod(0o600)
    return GLOBAL_CONFIG_FILE


def append_keys_to_config(new_keys: list[dict]) -> Path:
    """Append `new_keys` to `keys[]` in config.json without disturbing any
    other fields (preserves `_comment`, pricing, model fields, etc.)."""
    raw = json.loads(GLOBAL_CONFIG_FILE.read_text())
    keys = raw.get("keys") or []
    keys.extend(new_keys)
    raw["keys"] = keys
    GLOBAL_CONFIG_FILE.write_text(json.dumps(raw, indent=2))
    GLOBAL_CONFIG_FILE.chmod(0o600)
    return GLOBAL_CONFIG_FILE


def update_key_in_config(label: str, *, api_key: Optional[str] = None,
                         api_key_id: Optional[str] = None,
                         expire_time: Optional[str] = None) -> bool:
    """Patch the entry whose label == label, in place. Returns True if updated."""
    raw = json.loads(GLOBAL_CONFIG_FILE.read_text())
    keys = raw.get("keys") or []
    found = False
    for entry in keys:
        if isinstance(entry, dict) and entry.get("label") == label:
            if api_key is not None:
                entry["api_key"] = api_key
            if api_key_id is not None:
                entry["api_key_id"] = api_key_id
            if expire_time is not None:
                entry["expire_time"] = expire_time
            found = True
            break
    if found:
        raw["keys"] = keys
        GLOBAL_CONFIG_FILE.write_text(json.dumps(raw, indent=2))
        GLOBAL_CONFIG_FILE.chmod(0o600)
    return found


def remove_keys_from_config(matcher) -> tuple[Path, int]:
    """Remove every entry in keys[] for which matcher(entry) is True.

    matcher receives the raw dict from the file. Returns (file_path, n_removed).
    """
    raw = json.loads(GLOBAL_CONFIG_FILE.read_text())
    keys = raw.get("keys") or []
    kept = [k for k in keys if not matcher(k)]
    n_removed = len(keys) - len(kept)
    raw["keys"] = kept
    GLOBAL_CONFIG_FILE.write_text(json.dumps(raw, indent=2))
    GLOBAL_CONFIG_FILE.chmod(0o600)
    return GLOBAL_CONFIG_FILE, n_removed
