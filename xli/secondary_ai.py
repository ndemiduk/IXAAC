"""Secondary AI query for /2ndeye (MVP phases 1-3 only).

Single function query(messages, question, *, scope=None) -> str.
Implements anthropic and openai backends via urllib (no SDKs).
Config-driven, errors at boundary for missing config/env/non-2xx.
Never exposed as tool; slash-command only.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from xli.config import GlobalConfig


def query(messages: list[dict] | None, question: str, *, scope: str | None = None) -> str:
    """Send conversation slice + question to the configured secondary provider.

    messages: list of {"role": "user"|"assistant"|"system", "content": str} from history.
    question: the /2ndeye argument (appended as final user turn).
    scope: optional (e.g. "--last 5") for future slicing; ignored in MVP.
    Returns plain response text (caller adds [2ndeye · model] header).
    """
    cfg = GlobalConfig.load()
    sec = getattr(cfg, "secondary_ai", {}) or {}
    if not sec or not sec.get("provider"):
        raise RuntimeError(
            "secondary_ai not configured in ~/.config/xli/config.json. "
            "See 2ndeye.md (or proposals/2ndeye.md) for the schema and setup."
        )

    provider = str(sec.get("provider", "")).lower().strip()
    model = sec.get("model") or (
        "claude-3-5-sonnet-20241022" if provider == "anthropic" else "gpt-4o-mini"
    )
    api_key_env = sec.get("api_key_env")
    if not api_key_env:
        raise RuntimeError(
            f"secondary_ai.api_key_env missing in config for provider {provider}."
        )
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(
            f"{api_key_env} not set in environment. Export it and retry /2ndeye."
        )

    # Assemble chat messages: history + question as final user turn
    chat_messages: list[dict] = list(messages) if messages else []
    if question and str(question).strip():
        chat_messages.append({"role": "user", "content": str(question).strip()})
    if not chat_messages:
        chat_messages = [{"role": "user", "content": "Please provide a helpful response."}]

    if provider == "anthropic":
        # Anthropic: system is top-level; messages must start with user (filter system)
        system_prompt = None
        filtered: list[dict] = []
        for m in chat_messages:
            role = m.get("role")
            content = m.get("content", "")
            if role == "system":
                system_prompt = content
            elif role in ("user", "assistant"):
                filtered.append({"role": role, "content": content})
        if not filtered:
            filtered = [{"role": "user", "content": question or "Respond."}]
        payload = {
            "model": model,
            "max_tokens": 2048,
            "messages": filtered,
        }
        if system_prompt:
            payload["system"] = system_prompt
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    elif provider == "openai":
        payload = {
            "model": model,
            "messages": chat_messages,
            "max_tokens": 2048,
        }
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
    else:
        raise RuntimeError(
            f"Unsupported secondary_ai.provider={provider}. Use 'anthropic' or 'openai'."
        )

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body_bytes = resp.read()
            resp_json = json.loads(body_bytes.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        err_body = ""
        if e.fp:
            err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {provider}/{model}: {err_body[:400]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error to {provider}: {e.reason}") from e
    except Exception as e:
        raise RuntimeError(f"Unexpected error calling {provider}: {e}") from e

    # Extract plain text response
    if provider == "anthropic":
        blocks = resp_json.get("content", []) or []
        text = "".join(
            b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        choices = resp_json.get("choices", []) or []
        msg = choices[0].get("message", {}) if choices else {}
        text = msg.get("content", "") or str(resp_json)

    return text.strip() if text else "(empty response from secondary model)"
