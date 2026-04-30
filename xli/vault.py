"""Encrypted credential vault for plugin auth.

Per the design proposal: plugins authenticate via env vars, and those env
vars live in a single Fernet-encrypted JSON at ``~/.config/xli/vault.enc``
keyed by plugin id. Plaintext only exists in process memory during a call.

Master-key resolution order (first hit wins):

1. ``XLI_VAULT_KEY`` env var — raw 44-char Fernet key. Headless / CI path.
2. OS keyring (Secret Service / Keychain / Credential Locker) via the
   ``keyring`` library — service ``xli``, username ``vault-master``.
   The default for desktop sessions; zero user-managed key files.
3. ``~/.config/xli/.vault-key`` (chmod 0o400) — fallback for systems where
   keyring isn't available (broken Secret Service, sandboxed shells, etc.)

If none of those produces a key when one is needed, ``Vault.create()``
provisions a fresh Fernet key into the best available backend (keyring
preferred, falling back to the on-disk file). The first ``Vault.unlock()``
on an empty system therefore Just Works without explicit ``init``.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from xli.config import GLOBAL_CONFIG_DIR

VAULT_FILE = GLOBAL_CONFIG_DIR / "vault.enc"
KEY_FILE = GLOBAL_CONFIG_DIR / ".vault-key"

# Stable identifiers for the OS keyring entry. Keep these stable forever —
# changing them strands existing vaults until users migrate manually.
KEYRING_SERVICE = "xli"
KEYRING_USERNAME = "vault-master"

# Env-var fallback. Read once when no other source produced a key.
ENV_VAR = "XLI_VAULT_KEY"

BACKEND_ENV = "env"
BACKEND_KEYRING = "keyring"
BACKEND_FILE = "file"


class VaultError(Exception):
    """Anything that prevents read/write of the vault."""


# --------------------------------------------------------------------------- #
#  Master-key storage backends
# --------------------------------------------------------------------------- #

def _read_env_key() -> Optional[bytes]:
    raw = os.environ.get(ENV_VAR)
    return raw.encode() if raw else None


def _read_keyring_key() -> Optional[bytes]:
    """Try the OS keyring. Returns None on any failure (no backend available,
    backend rejects access, key missing, etc.) — caller falls through."""
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:
        return None
    try:
        v = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except KeyringError:
        return None
    except Exception:
        # Some backends raise non-KeyringError subclasses (D-Bus errors, etc.)
        # when the daemon is missing. Treat all of these as "no keyring."
        return None
    return v.encode() if v else None


def _write_keyring_key(key: bytes) -> bool:
    """Persist ``key`` into the OS keyring. Returns True on success."""
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:
        return False
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, key.decode())
    except (KeyringError, Exception):
        return False
    return True


def _read_file_key() -> Optional[bytes]:
    if not KEY_FILE.exists():
        return None
    try:
        return KEY_FILE.read_bytes().strip()
    except OSError:
        return None


def _write_file_key(key: bytes) -> None:
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Write atomically with own-only perms regardless of umask.
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o400)
    try:
        os.fchmod(fd, 0o400)
    except OSError:
        pass
    with os.fdopen(fd, "wb") as f:
        f.write(key)


def _resolve_key() -> tuple[Optional[bytes], Optional[str]]:
    """Return ``(key, backend)`` from the first source that has one, else
    ``(None, None)``. Backend is one of BACKEND_ENV/KEYRING/FILE."""
    if (k := _read_env_key()) is not None:
        return (k, BACKEND_ENV)
    if (k := _read_keyring_key()) is not None:
        return (k, BACKEND_KEYRING)
    if (k := _read_file_key()) is not None:
        return (k, BACKEND_FILE)
    return (None, None)


def _provision_key() -> tuple[bytes, str]:
    """Generate a fresh master key, store it via the best available backend,
    and return ``(key, backend_used)``. Tries keyring first, then file."""
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    if _write_keyring_key(key):
        return (key, BACKEND_KEYRING)
    _write_file_key(key)
    return (key, BACKEND_FILE)


# --------------------------------------------------------------------------- #
#  Vault
# --------------------------------------------------------------------------- #

@dataclass
class Vault:
    """Decrypted in-memory view of the vault. Mutations re-encrypt on save.

    Use ``Vault.unlock()`` to load. Use ``Vault.set(...)``/``unset(...)`` to
    mutate; both call ``save()`` internally so callers don't accidentally
    leave the on-disk vault stale.
    """
    _key: bytes
    _data: dict[str, dict[str, str]]
    backend: str  # which master-key backend produced the key

    # ---- factory ---- #

    @classmethod
    def unlock(cls, *, create_if_missing: bool = True) -> "Vault":
        """Decrypt the vault file and return a Vault instance.

        If no master key exists anywhere, ``create_if_missing=True`` (the
        default) provisions one via the best available backend and writes
        an empty encrypted vault. Set False to require explicit init.
        """
        from cryptography.fernet import Fernet, InvalidToken

        key, backend = _resolve_key()
        if key is None:
            if not create_if_missing:
                raise VaultError(
                    "no master key found — set $XLI_VAULT_KEY, or run any "
                    "`xli auth set` to provision one automatically."
                )
            key, backend = _provision_key()

        if VAULT_FILE.exists():
            try:
                token = VAULT_FILE.read_bytes()
            except OSError as e:
                raise VaultError(f"cannot read vault file: {e}") from e
            try:
                plaintext = Fernet(key).decrypt(token)
            except InvalidToken:
                raise VaultError(
                    f"vault decrypt failed — master key (backend={backend}) "
                    f"doesn't match {VAULT_FILE}. Check $XLI_VAULT_KEY, "
                    "the OS keyring entry, or ~/.config/xli/.vault-key."
                )
            try:
                data = json.loads(plaintext.decode())
            except json.JSONDecodeError as e:
                raise VaultError(f"vault contents corrupted: {e}") from e
            if not isinstance(data, dict):
                raise VaultError("vault contents corrupted (root is not a dict)")
        else:
            data = {}

        return cls(_key=key, _data=data, backend=backend)

    # ---- accessors ---- #

    def get(self, plugin_id: str) -> dict[str, str]:
        """Return the env-var dict for ``plugin_id``, or empty dict."""
        return dict(self._data.get(plugin_id, {}))

    def has(self, plugin_id: str, env_var: str) -> bool:
        return env_var in self._data.get(plugin_id, {})

    def list_plugins(self) -> list[str]:
        return sorted(self._data.keys())

    def list_keys(self, plugin_id: str) -> list[str]:
        return sorted(self._data.get(plugin_id, {}).keys())

    # ---- mutators ---- #

    def set(self, plugin_id: str, env_var: str, value: str) -> None:
        slot = self._data.setdefault(plugin_id, {})
        slot[env_var] = value
        self.save()

    def unset(self, plugin_id: str, env_var: Optional[str] = None) -> bool:
        """Remove one env var (or the whole plugin if ``env_var`` is None).
        Returns True if anything was removed."""
        if plugin_id not in self._data:
            return False
        if env_var is None:
            del self._data[plugin_id]
            self.save()
            return True
        if env_var not in self._data[plugin_id]:
            return False
        del self._data[plugin_id][env_var]
        if not self._data[plugin_id]:
            # Drop the empty plugin entry so list_plugins() stays clean.
            del self._data[plugin_id]
        self.save()
        return True

    # ---- persistence ---- #

    def save(self) -> None:
        from cryptography.fernet import Fernet
        VAULT_FILE.parent.mkdir(parents=True, exist_ok=True)
        token = Fernet(self._key).encrypt(json.dumps(self._data, sort_keys=True).encode())
        # Atomic-ish write: open with restrictive mode, then chmod again to
        # force 0o600 regardless of umask. Same pattern as the daemon audit log.
        fd = os.open(VAULT_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "wb") as f:
            f.write(token)


# --------------------------------------------------------------------------- #
#  Convenience for bash injection (the hot path — no need to load secrets
#  for every call, only when the command actually references one)
# --------------------------------------------------------------------------- #

def env_for_command(
    cmd: str,
    subscribed_plugins: list,  # list[Plugin] — annotated loosely to dodge cycle
) -> dict[str, str]:
    """Return env-var overrides that should be injected into ``cmd``'s subprocess.

    Scans ``cmd`` for ``${VAR}`` and ``$VAR`` references. For each match, if
    a subscribed plugin declares ``VAR`` in its ``auth_env_vars`` and the
    vault holds a value, emit it. Vault is unlocked lazily — if no referenced
    var resolves to a vault entry, we skip the decrypt entirely.

    Returns ``{}`` when nothing matches (the common case for read-only public
    APIs — agent uses bash without any plugin secrets at all).
    """
    if not subscribed_plugins:
        return {}

    import re as _re
    refs = set(_re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)", cmd))
    referenced = {a or b for (a, b) in refs}
    if not referenced:
        return {}

    declared: dict[str, str] = {}  # env_var -> plugin_id (first-declarer wins)
    for plugin in subscribed_plugins:
        try:
            auth_vars = plugin.auth_env_vars()
        except Exception:
            continue
        for v in auth_vars:
            if v in referenced and v not in declared:
                declared[v] = plugin.id

    if not declared:
        return {}

    try:
        vault = Vault.unlock(create_if_missing=False)
    except VaultError:
        return {}

    out: dict[str, str] = {}
    for var, pid in declared.items():
        secrets = vault.get(pid)
        if var in secrets:
            out[var] = secrets[var]
    return out
