"""plugin_call — structured plugin invocation, no curl synthesis needed.

Flow: validate subscription → load manifest → find action → validate params
→ host-pin check → inject vault auth → execute HTTP (or exec) → audit log → return result.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlparse

from ..context import ToolContext, ToolResult
from ..helpers import _truncate

# Audit log lives alongside the vault and plugin catalog.
_AUDIT_LOG_PATH = None  # resolved lazily


def _audit_log_path():
    global _AUDIT_LOG_PATH
    if _AUDIT_LOG_PATH is None:
        from xli.config import GLOBAL_CONFIG_DIR
        _AUDIT_LOG_PATH = GLOBAL_CONFIG_DIR / "plugin-audit.log"
    return _AUDIT_LOG_PATH


def _audit(
    plugin_id: str,
    action_id: str,
    params: dict[str, str],
    secrets: dict[str, str],
    *,
    status: str,
    response_bytes: int = 0,
) -> None:
    """Append one audit line. Params are redacted before writing."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Redact secret values from the params dict.
    safe_params = {}
    for k, v in params.items():
        redacted = v
        for secret_val in secrets.values():
            if secret_val and secret_val in redacted:
                redacted = redacted.replace(secret_val, "***")
        safe_params[k] = redacted
    entry = {
        "ts": ts,
        "plugin": plugin_id,
        "action": action_id,
        "params": safe_params,
        "status": status,
        "response_bytes": response_bytes,
    }
    try:
        path = _audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except Exception:
        pass  # audit is best-effort; never block the tool call


def _inject_secrets(
    text: str, secrets: dict[str, str]
) -> str:
    """Replace ${VAR} and $VAR references with vault values."""
    for var, val in secrets.items():
        text = text.replace(f"${{{var}}}", val)
        text = re.sub(rf"\${var}(?![A-Z0-9_])", val, text)
    return text


def _redact_secrets(text: str, secrets: dict[str, str]) -> str:
    """Replace secret values with *** for logging."""
    for val in secrets.values():
        if val:
            text = text.replace(val, "***")
    return text


def _load_vault_secrets(plugin_id: str) -> dict[str, str]:
    """Load vault secrets for a plugin. Returns {} on any failure."""
    try:
        from xli.vault import Vault
        vault = Vault.unlock(create_if_missing=False)
        return vault.get(plugin_id)
    except Exception:
        return {}


def t_plugin_call(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from xli.plugin import Plugin
    from xli.plugin_manifest import PluginManifest, TRUST_ALWAYS_CONFIRM

    # --- arg extraction ---
    plugin_id = (args.get("plugin") or "").strip()
    action_id = (args.get("action") or "").strip()
    user_params = args.get("params") or {}

    if not plugin_id:
        return ToolResult("plugin_call: 'plugin' is required", is_error=True)
    if not action_id:
        return ToolResult("plugin_call: 'action' is required", is_error=True)
    if not isinstance(user_params, dict):
        return ToolResult("plugin_call: 'params' must be a JSON object", is_error=True)

    # --- subscription check ---
    if plugin_id not in ctx.subscribed_plugins:
        return ToolResult(
            f"plugin {plugin_id!r} is not subscribed. "
            f"Subscribed: {ctx.subscribed_plugins or '(none)'}. "
            f"Ask the user to /lib subscribe {plugin_id}.",
            is_error=True,
        )

    # --- load manifest ---
    p = Plugin(id=plugin_id)
    if not p.exists():
        return ToolResult(
            f"plugin {plugin_id!r} subscribed but file missing on disk.",
            is_error=True,
        )
    manifest = p.manifest()
    if manifest is None:
        return ToolResult(
            f"plugin {plugin_id!r} has no structured actions. "
            f"Use plugin_get + bash for legacy plugins.",
            is_error=True,
        )

    # --- find action ---
    action = manifest.get_action(action_id)
    if action is None:
        return ToolResult(
            f"plugin {plugin_id!r} has no action {action_id!r}. "
            f"Available: {manifest.action_ids}",
            is_error=True,
        )

    # --- validate params ---
    merged, errors = action.resolve_params(user_params)
    if errors:
        return ToolResult(
            f"plugin_call param errors:\n" + "\n".join(f"  - {e}" for e in errors),
            is_error=True,
        )

    # --- load vault secrets for auth injection ---
    secrets = _load_vault_secrets(plugin_id)

    # --- trust gate ---
    if manifest.trust == TRUST_ALWAYS_CONFIRM and not ctx.yolo:
        if ctx.console is None:
            return ToolResult(
                f"plugin_call refused: {plugin_id}/{action_id} requires confirmation "
                "but no console attached. Use --yolo.",
                is_error=True,
            )
        ctx.console.print(
            f"  [yellow]⚠ plugin_call[/yellow] {plugin_id}/{action_id} "
            f"(effect={manifest.effect})"
        )
        try:
            answer = input("  approve? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "y":
            return ToolResult(
                f"plugin_call denied by user ({plugin_id}/{action_id}).",
                is_error=True,
            )

    # --- dispatch ---
    if action.is_exec:
        return _exec_action(ctx, plugin_id, action, merged, secrets)
    else:
        return _http_action(ctx, plugin_id, action, merged, secrets)


def _http_action(
    ctx: ToolContext,
    plugin_id: str,
    action,
    merged: dict[str, str],
    secrets: dict[str, str],
) -> ToolResult:
    """Execute an HTTP action using urllib (no external deps)."""
    import urllib.request
    import urllib.error

    url = action.url
    # Inject secrets into URL template (for APIs that embed keys in the URL).
    if secrets:
        url = _inject_secrets(url, secrets)

    # Inject secrets into param values (e.g. const: "${API_KEY}").
    resolved_params = {}
    for k, v in merged.items():
        resolved_params[k] = _inject_secrets(v, secrets) if secrets else v

    # Build headers, inject secrets.
    headers = {"User-Agent": "xli-plugin-call/1.0"}
    for hk, hv in action.headers.items():
        headers[hk] = _inject_secrets(hv, secrets) if secrets else hv

    # Host-pin check: resolved URL host must match declared template host.
    declared_hosts = action.allowed_hosts
    if declared_hosts:
        try:
            actual_host = urlparse(url).hostname
        except Exception:
            actual_host = None
        if actual_host and actual_host not in declared_hosts:
            return ToolResult(
                f"plugin_call refused: resolved host {actual_host!r} not in "
                f"declared hosts {declared_hosts}. Possible manifest tampering.",
                is_error=True,
            )

    # Build final URL.
    method = action.method.upper()
    if method == "GET":
        sep = "&" if "?" in url else "?"
        final_url = url + sep + urlencode(resolved_params) if resolved_params else url
        data = None
    else:
        final_url = url
        data = json.dumps(resolved_params).encode("utf-8")
        if "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"

    req = urllib.request.Request(final_url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        if secrets:
            body = _redact_secrets(body, secrets)
        _audit(plugin_id, action.id, merged, secrets, status=f"http_{e.code}", response_bytes=len(body))
        return ToolResult(
            f"HTTP {e.code} from {plugin_id}/{action.id}:\n{_truncate(body)}",
            is_error=True,
        )
    except urllib.error.URLError as e:
        _audit(plugin_id, action.id, merged, secrets, status="url_error")
        return ToolResult(
            f"plugin_call network error ({plugin_id}/{action.id}): {e.reason}",
            is_error=True,
        )
    except Exception as e:
        _audit(plugin_id, action.id, merged, secrets, status="error")
        return ToolResult(
            f"plugin_call error ({plugin_id}/{action.id}): {e}",
            is_error=True,
        )

    # Redact secrets from response body.
    if secrets:
        body = _redact_secrets(body, secrets)

    _audit(plugin_id, action.id, merged, secrets, status="ok", response_bytes=len(body))
    return ToolResult(_truncate(body))


def _exec_action(
    ctx: ToolContext,
    plugin_id: str,
    action,
    merged: dict[str, str],
    secrets: dict[str, str],
) -> ToolResult:
    """Execute a subprocess-based action (e.g. xmpp_send)."""
    command = action.command
    if not command:
        return ToolResult(
            f"exec action {plugin_id}/{action.id} has no 'command' template.",
            is_error=True,
        )

    # Inject params into the command template as {PARAM_NAME}.
    for k, v in merged.items():
        command = command.replace(f"{{{k}}}", v)

    # Inject secrets.
    if secrets:
        command = _inject_secrets(command, secrets)

    # Build env with secrets.
    env = None
    if secrets:
        env = {**os.environ, **secrets}

    try:
        proc = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except subprocess.TimeoutExpired:
        _audit(plugin_id, action.id, merged, secrets, status="timeout")
        return ToolResult(
            f"exec action {plugin_id}/{action.id} timed out after 60s",
            is_error=True,
        )

    out = proc.stdout
    if proc.stderr:
        out += "\n--- stderr ---\n" + proc.stderr

    # Redact secrets.
    if secrets:
        out = _redact_secrets(out, secrets)

    status = "ok" if proc.returncode == 0 else f"exit_{proc.returncode}"
    _audit(plugin_id, action.id, merged, secrets, status=status, response_bytes=len(out))
    return ToolResult(
        _truncate(out),
        is_error=proc.returncode != 0,
    )
