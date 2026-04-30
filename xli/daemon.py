"""XMPP command daemon — Phase 2 of the iXaac multi-machine fabric.

Listens for OMEMO-encrypted DMs from a JID whitelist and dispatches messages:
- Built-in `kill` verb shuts the daemon down cleanly
- Verb scripts in `~/.config/xli/verbs/<name>.sh` run as subprocess; stdout
  becomes the OMEMO-encrypted reply
- Anything else falls back to `xli ask <workspace> <prompt>`, which runs a
  one-shot iXaac agent turn in the named (or most-recently-active) workspace

A workspace prefix `[alias] <message>` overrides the agent fallback target so
"[isaac2] grep me the auth module" runs the agent in that specific workspace.

Designed as a long-lived process. Tailscale-only substrate is assumed (Prosody
binds to the tailnet IP in `/etc/prosody/prosody.cfg.lua`). Every inbound
message is audit-logged in append-only JSONL.

This module mirrors the OMEMO setup in `~/.config/xli/bin/xmpp_send.py` but
runs its own dedicated state file (`daemon-omemo-state.json`) — daemon and
sender JIDs are different identities and must not share OMEMO state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import tomllib
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# OMEMO / slixmpp deps live in the dedicated venv at ~/.config/xli/bin/venv/.
# When invoked via `xli daemon --xmpp` from the project venv, those deps are
# NOT on sys.path. The CLI shim takes care of re-execing through the OMEMO
# venv's interpreter — see `cmd_daemon` in xli/cli.py.
from omemo.storage import Just, Maybe, Nothing, Storage
from omemo.types import JSONType
from slixmpp.clientxmpp import ClientXMPP
from slixmpp.jid import JID
from slixmpp.plugins import register_plugin
from slixmpp.stanza import Message
from slixmpp_omemo import TrustLevel, XEP_0384


# --------------------------------------------------------------------------- #
#  Defaults & limits
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "xli" / "daemon.toml"
DEFAULT_VERBS_DIR = Path.home() / ".config" / "xli" / "verbs"
DEFAULT_AUDIT_LOG = Path.home() / ".local" / "share" / "xli" / "daemon-audit.log"
DEFAULT_OMEMO_STATE = Path.home() / ".config" / "xli" / "daemon-omemo-state.json"

MAX_REPLY_CHARS = 1500       # Conversations renders more, but huge replies are unwieldy
VERB_TIMEOUT_S = 30          # Per-verb hard timeout
AGENT_TIMEOUT_S = 300        # Agent-fallback hard timeout (5 min)


# --------------------------------------------------------------------------- #
#  Config (TOML)
# --------------------------------------------------------------------------- #

@dataclass
class DaemonConfig:
    jid: str
    password_env: str
    state_file: Path
    verbs_dir: Path
    audit_log: Path
    allowed_jids: list[str]
    max_per_minute: int
    lockout_threshold: int
    lockout_duration_s: int
    fallback_enabled: bool
    fallback_workspace: str   # alias or path; "" = most-recent-project from registry

    @classmethod
    def load(cls, path: Path) -> "DaemonConfig":
        with open(path, "rb") as f:
            data = tomllib.load(f)
        d = data.get("daemon", {})
        wl = data.get("whitelist", {})
        rl = data.get("rate_limit", {})
        af = data.get("agent_fallback", {})
        return cls(
            jid=d["jid"],
            password_env=d.get("password_env", "XMPP_DAEMON_PASSWORD"),
            state_file=Path(d.get("state_file", str(DEFAULT_OMEMO_STATE))).expanduser(),
            verbs_dir=Path(d.get("verbs_dir", str(DEFAULT_VERBS_DIR))).expanduser(),
            audit_log=Path(d.get("audit_log", str(DEFAULT_AUDIT_LOG))).expanduser(),
            allowed_jids=list(wl.get("allowed_jids", [])),
            max_per_minute=int(rl.get("max_per_minute", 10)),
            lockout_threshold=int(rl.get("lockout_threshold", 5)),
            lockout_duration_s=int(rl.get("lockout_duration_s", 300)),
            fallback_enabled=bool(af.get("enabled", True)),
            fallback_workspace=str(af.get("default_workspace", "")),
        )


# --------------------------------------------------------------------------- #
#  OMEMO storage (mirrors xmpp_send.py)
# --------------------------------------------------------------------------- #

class JsonStorage(Storage):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self._data: dict[str, JSONType] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text("utf8"))
            except Exception:
                pass

    async def _load(self, key: str) -> Maybe[JSONType]:
        if key in self._data:
            return Just(self._data[key])
        return Nothing()

    async def _store(self, key: str, value: JSONType) -> None:
        self._data[key] = value
        self._path.write_text(json.dumps(self._data), encoding="utf8")

    async def _delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._path.write_text(json.dumps(self._data), encoding="utf8")


class XEP_0384Impl(XEP_0384):
    default_config = {
        "json_file_path": None,
        "fallback_message": "This message is OMEMO encrypted.",
    }

    def plugin_init(self) -> None:
        self._json_storage = JsonStorage(Path(self.json_file_path))
        super().plugin_init()

    @property
    def storage(self) -> Storage:
        return self._json_storage

    @property
    def _btbv_enabled(self) -> bool:
        return True

    async def _devices_blindly_trusted(self, blindly_trusted, identifier):
        for d in blindly_trusted:
            logging.info(f"BTBV trusted device: {d.bare_jid}/{d.device_id}")

    async def _prompt_manual_trust(self, manually_trusted, identifier):
        # BTBV is enabled; in a long-running daemon we should never block on
        # stdin to ask. Distrust new manual-tier devices by default.
        sm = await self.get_session_manager()
        for d in manually_trusted:
            await sm.set_trust(d.bare_jid, d.identity_key, TrustLevel.DISTRUSTED.value)


register_plugin(XEP_0384Impl)


# --------------------------------------------------------------------------- #
#  Rate limiter
# --------------------------------------------------------------------------- #

class RateLimiter:
    """Per-JID sliding-window rate limit + repeated-deny lockout.

    Keeps a 60-second deque of accept timestamps per JID. If the JID submits
    more than `max_per_minute` accepts within the window, subsequent messages
    are denied. After `lockout_threshold` consecutive denials, the JID is
    locked out for `lockout_duration_s` seconds (no messages accepted at all).
    """

    def __init__(self, max_per_minute: int, lockout_threshold: int, lockout_duration_s: int):
        self.max_per_minute = max_per_minute
        self.lockout_threshold = lockout_threshold
        self.lockout_duration_s = lockout_duration_s
        self.windows: dict[str, deque[float]] = defaultdict(deque)
        self.consecutive_denied: dict[str, int] = defaultdict(int)
        self.lockout_until: dict[str, float] = {}

    def check(self, jid: str) -> tuple[bool, Optional[str]]:
        now = time.time()
        if jid in self.lockout_until:
            if now < self.lockout_until[jid]:
                left = int(self.lockout_until[jid] - now)
                return (False, f"locked out, {left}s remaining")
            del self.lockout_until[jid]
            self.consecutive_denied[jid] = 0

        window = self.windows[jid]
        cutoff = now - 60
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= self.max_per_minute:
            self.consecutive_denied[jid] += 1
            if self.consecutive_denied[jid] >= self.lockout_threshold:
                self.lockout_until[jid] = now + self.lockout_duration_s
                self.consecutive_denied[jid] = 0
                return (False, f"rate limit hit too often; locked out {self.lockout_duration_s}s")
            return (False, f"rate limit ({self.max_per_minute}/min) exceeded")

        window.append(now)
        self.consecutive_denied[jid] = 0
        return (True, None)


# --------------------------------------------------------------------------- #
#  The daemon
# --------------------------------------------------------------------------- #

WORKSPACE_PREFIX_RE = re.compile(r"^\[([\w][\w.-]*)\]\s*(.*)$", flags=re.DOTALL)


class CommandDaemon(ClientXMPP):
    def __init__(self, cfg: DaemonConfig, password: str):
        super().__init__(cfg.jid, password)
        self.cfg = cfg
        self.rate_limiter = RateLimiter(
            cfg.max_per_minute, cfg.lockout_threshold, cfg.lockout_duration_s
        )
        self.shutdown_requested = False
        self.add_event_handler("session_start", self._on_session_start)
        self.add_event_handler("message", self._on_message)

    async def _on_session_start(self, _event: Any) -> None:
        self.send_presence()
        await self.get_roster()
        logging.info(
            f"daemon online as {self.cfg.jid}; "
            f"whitelist={self.cfg.allowed_jids or '(empty — all messages will be rejected)'}"
        )

    async def _on_message(self, stanza: Message) -> None:
        if stanza["type"] not in ("chat", "normal"):
            return
        sender = JID(stanza["from"]).bare

        xep_0384: XEP_0384 = self["xep_0384"]
        ns = xep_0384.is_encrypted(stanza)
        if ns is None:
            self._audit(sender, None, "rejected: unencrypted")
            self._send_plain(sender, "[daemon] OMEMO required; message rejected.")
            return

        if sender not in self.cfg.allowed_jids:
            self._audit(sender, None, "rejected: not in whitelist")
            return  # silent — don't ack to non-whitelisted JIDs

        ok, reason = self.rate_limiter.check(sender)
        if not ok:
            self._audit(sender, None, f"rate-limited: {reason}")
            await self._encrypted_reply(sender, f"[daemon] {reason}")
            return

        try:
            inner, _device_info = await xep_0384.decrypt_message(stanza)
            body = (inner["body"] or "").strip()
        except Exception as e:
            logging.exception("decrypt failed")
            self._audit(sender, None, f"decrypt error: {type(e).__name__}: {e}")
            await self._encrypted_reply(sender, f"[daemon] decryption failed: {type(e).__name__}")
            return

        if not body:
            return

        self._audit(sender, body, "received")

        try:
            reply = await self._dispatch(sender, body)
        except Exception as e:
            logging.exception("dispatch error")
            reply = f"[daemon] error: {type(e).__name__}: {e}"
            self._audit(sender, body, f"dispatch error: {e}")

        if reply:
            await self._encrypted_reply(sender, reply[:MAX_REPLY_CHARS])

        if self.shutdown_requested:
            await asyncio.sleep(2)  # let final reply flush
            self.disconnect()

    async def _dispatch(self, sender: str, body: str) -> str:
        # Built-in: kill (always available, never delegated to a verb file)
        first_word = body.split(maxsplit=1)[0].lower() if body else ""
        if first_word == "kill":
            self.shutdown_requested = True
            self._audit(sender, body, "shutdown requested")
            return "[daemon] shutting down."

        # Optional workspace prefix: "[alias] message..." overrides the agent
        # fallback target. The prefix is stripped before further dispatch.
        workspace_override: Optional[str] = None
        m = WORKSPACE_PREFIX_RE.match(body)
        if m:
            workspace_override = m.group(1)
            body = m.group(2).strip()
            first_word = body.split(maxsplit=1)[0].lower() if body else ""

        # Verb script lookup
        if first_word:
            verb_path = self.cfg.verbs_dir / f"{first_word}.sh"
            if verb_path.exists() and os.access(verb_path, os.X_OK):
                args = body.split()[1:]
                self._audit(sender, body, f"verb: {first_word}")
                return await self._run_verb(verb_path, args)

        # Agent fallback
        if not self.cfg.fallback_enabled:
            verbs = self._list_verbs()
            return f"[daemon] unknown verb '{first_word}'. Verbs: {verbs}"

        workspace = workspace_override or self.cfg.fallback_workspace
        self._audit(sender, body, f"agent fallback (ws={workspace or 'auto'})")
        return await self._run_agent(body, workspace)

    def _list_verbs(self) -> str:
        if not self.cfg.verbs_dir.is_dir():
            return "(none — daemon's verbs_dir does not exist)"
        names = sorted(p.stem for p in self.cfg.verbs_dir.glob("*.sh") if os.access(p, os.X_OK))
        return ", ".join(names) if names else "(none — verbs_dir is empty)"

    async def _run_verb(self, path: Path, args: list[str]) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                str(path), *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=VERB_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"[daemon] verb timed out after {VERB_TIMEOUT_S}s"
        except Exception as e:
            return f"[daemon] verb error: {type(e).__name__}: {e}"

        out = stdout.decode("utf-8", "replace").rstrip()
        err = stderr.decode("utf-8", "replace").rstrip()
        if proc.returncode != 0 and not out:
            return f"[verb exit {proc.returncode}] {err[:500]}" if err else f"[verb exit {proc.returncode}]"
        if err:
            out = f"{out}\n[stderr] {err[:200]}" if out else f"[stderr] {err[:500]}"
        return out or f"(verb returned no output, exit {proc.returncode})"

    async def _run_agent(self, prompt: str, workspace_key: str) -> str:
        """Spawn `xli ask <prompt>` and capture its reply.

        We rely on `xli` being on PATH. The `--workspace` flag is forwarded so
        the agent runs in the right project context.
        """
        cmd = ["xli", "ask"]
        if workspace_key:
            cmd.extend(["--workspace", workspace_key])
        cmd.append(prompt)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=AGENT_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"[daemon] agent timed out after {AGENT_TIMEOUT_S}s"
        except FileNotFoundError:
            return "[daemon] `xli` not on PATH; agent fallback unavailable"
        except Exception as e:
            return f"[daemon] agent error: {type(e).__name__}: {e}"

        out = stdout.decode("utf-8", "replace").rstrip()
        err = stderr.decode("utf-8", "replace").rstrip()
        if proc.returncode != 0:
            return f"[agent exit {proc.returncode}] {(err or out)[:800]}"
        return out or "(agent returned no output)"

    def _send_plain(self, to_jid: str, body: str) -> None:
        """Plaintext reply — only for cases where OMEMO isn't viable
        (e.g. unencrypted-rejected). Whitelisted senders always get
        encrypted replies via _encrypted_reply()."""
        msg = self.make_message(mto=JID(to_jid), mtype="chat")
        msg["body"] = body
        msg.send()

    async def _encrypted_reply(self, to_jid: str, body: str) -> None:
        xep_0384: XEP_0384 = self["xep_0384"]
        xep_0380 = self["xep_0380"]
        recipient = JID(to_jid)
        try:
            await xep_0384.refresh_device_lists({recipient}, force_download=True)
        except Exception:
            logging.exception("device list refresh failed")

        msg = self.make_message(mto=recipient, mtype="chat")
        msg["body"] = body
        msg.set_to(recipient)
        msg.set_from(self.boundjid)
        try:
            messages, _errors = await xep_0384.encrypt_message(msg, {recipient})
        except Exception:
            logging.exception("encrypt_message failed; reply dropped")
            return
        for ns, m in messages.items():
            m["eme"]["namespace"] = ns
            m["eme"]["name"] = xep_0380.mechanisms[ns]
            m.send()

    def _audit(self, sender: str, body: Optional[str], status: str) -> None:
        try:
            self.cfg.audit_log.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            line = json.dumps({
                "ts": ts,
                "from": sender,
                "body": (body or "")[:500],
                "status": status,
            })
            with self.cfg.audit_log.open("a") as f:
                f.write(line + "\n")
        except Exception:
            logging.exception("audit log write failed")


# --------------------------------------------------------------------------- #
#  Entry point — invoked by `xli daemon --xmpp`
# --------------------------------------------------------------------------- #

def run(config_path: Path = DEFAULT_CONFIG_PATH) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not config_path.exists():
        print(f"error: daemon config not found at {config_path}", file=sys.stderr)
        print("see ~/.config/xli/daemon.toml.example for a template", file=sys.stderr)
        return 3

    try:
        cfg = DaemonConfig.load(config_path)
    except (KeyError, ValueError, tomllib.TOMLDecodeError) as e:
        print(f"error: invalid daemon config at {config_path}: {e}", file=sys.stderr)
        return 3

    password = os.environ.get(cfg.password_env)
    if not password:
        print(f"error: {cfg.password_env} not set in env", file=sys.stderr)
        return 3

    if not cfg.allowed_jids:
        print(
            "error: whitelist.allowed_jids is empty — daemon would reject all "
            "messages. Refusing to start.", file=sys.stderr,
        )
        return 3

    cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
    if not cfg.state_file.exists():
        cfg.state_file.touch(mode=0o600)
    cfg.audit_log.parent.mkdir(parents=True, exist_ok=True)

    xmpp = CommandDaemon(cfg, password)
    xmpp.register_plugin("xep_0030")
    xmpp.register_plugin("xep_0060")
    xmpp.register_plugin("xep_0163")
    xmpp.register_plugin("xep_0199")
    xmpp.register_plugin("xep_0380")
    xmpp.register_plugin(
        "xep_0384",
        {"json_file_path": str(cfg.state_file)},
        module=sys.modules[__name__],
    )

    xmpp.add_event_handler("disconnected", lambda *_: xmpp.loop.stop())

    if not xmpp.connect():
        print("error: failed to connect to XMPP server", file=sys.stderr)
        return 2

    try:
        xmpp.loop.run_forever()
    except KeyboardInterrupt:
        logging.info("interrupted; disconnecting")
        try:
            xmpp.disconnect()
        except Exception:
            pass
        return 0

    return 0


if __name__ == "__main__":
    # Allow direct invocation:  python3 -m xli.daemon  or  python3 daemon.py
    # The CLI's `xli daemon --xmpp` re-execs through the OMEMO venv's Python
    # against this script.
    cfg_arg = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    sys.exit(run(cfg_arg))
