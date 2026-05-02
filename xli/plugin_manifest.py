"""Structured plugin manifests — action specs parsed from frontmatter.

A plugin manifest is the machine-executable layer of a plugin: typed actions
with validated params that plugin_call can invoke directly, no curl synthesis
needed. Plugins without an ``actions:`` block in their frontmatter are
"legacy" and still work via plugin_get + bash; the manifest is simply None.

The frontmatter is parsed with PyYAML (safe_load) to handle the nested
``actions:`` structure. The simpler parse_frontmatter in plugin.py continues
to work for flat fields; this module handles the structured bits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

import yaml


# --------------------------------------------------------------------------- #
#  Effect / trust — replaces the flat "risk" field
# --------------------------------------------------------------------------- #

EFFECT_READ_ONLY = "read-only"
EFFECT_EXTERNAL_WRITE = "external-write"
EFFECT_LOCAL_SYSTEM = "local-system"
EFFECT_DESTRUCTIVE = "destructive"
VALID_EFFECTS = {EFFECT_READ_ONLY, EFFECT_EXTERNAL_WRITE, EFFECT_LOCAL_SYSTEM, EFFECT_DESTRUCTIVE}

TRUST_SUBSCRIPTION = "subscription"
TRUST_ALWAYS_CONFIRM = "always-confirm"
VALID_TRUSTS = {TRUST_SUBSCRIPTION, TRUST_ALWAYS_CONFIRM}

# Legacy risk → (effect, trust) mapping for plugins that haven't migrated yet.
_RISK_MIGRATION = {
    "low": (EFFECT_READ_ONLY, TRUST_SUBSCRIPTION),
    "medium": (EFFECT_EXTERNAL_WRITE, TRUST_SUBSCRIPTION),
    "high": (EFFECT_EXTERNAL_WRITE, TRUST_ALWAYS_CONFIRM),
}


# --------------------------------------------------------------------------- #
#  Param spec
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ParamSpec:
    """One parameter in an action."""
    name: str
    description: str = ""
    required: bool = False
    default: Optional[str] = None
    const: Optional[str] = None      # value injected automatically (not user-supplied)
    enum: Optional[list[str]] = None  # allowed values

    def validate(self, value: Any) -> str | None:
        """Return an error message if ``value`` is invalid, else None."""
        if self.const is not None:
            return None  # const params are never user-supplied
        if self.enum is not None and str(value) not in self.enum:
            return f"param {self.name!r}: value {value!r} not in {self.enum}"
        return None


# --------------------------------------------------------------------------- #
#  Action spec
# --------------------------------------------------------------------------- #

VALID_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "exec"}


@dataclass(frozen=True)
class ActionSpec:
    """One invocable action in a plugin manifest."""
    id: str
    description: str
    method: str                                # HTTP verb or "exec"
    url: str = ""                              # URL template for HTTP actions
    command: str = ""                          # command template for exec actions
    params: dict[str, ParamSpec] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    response_shape: str = ""                   # brief hint for the model

    @property
    def is_exec(self) -> bool:
        return self.method == "exec"

    @property
    def allowed_hosts(self) -> set[str]:
        """Hosts extracted from the url template — used for runtime pinning."""
        if self.is_exec or not self.url:
            return set()
        try:
            return {urlparse(self.url).hostname}
        except Exception:
            return set()

    def resolve_params(self, user_params: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
        """Merge user params with defaults/consts. Returns (merged, errors)."""
        merged: dict[str, str] = {}
        errors: list[str] = []

        for name, spec in self.params.items():
            if spec.const is not None:
                merged[name] = spec.const
                continue
            if name in user_params:
                err = spec.validate(user_params[name])
                if err:
                    errors.append(err)
                else:
                    merged[name] = str(user_params[name])
            elif spec.default is not None:
                merged[name] = spec.default
            elif spec.required:
                errors.append(f"missing required param: {name!r}")

        # Warn about unknown params (typos).
        unknown = set(user_params) - set(self.params)
        for u in sorted(unknown):
            errors.append(f"unknown param: {u!r}")

        return merged, errors


# --------------------------------------------------------------------------- #
#  Plugin manifest
# --------------------------------------------------------------------------- #

@dataclass
class PluginManifest:
    """Structured manifest parsed from a plugin's frontmatter."""
    plugin_id: str
    effect: str = EFFECT_READ_ONLY
    trust: str = TRUST_SUBSCRIPTION
    actions: list[ActionSpec] = field(default_factory=list)

    def get_action(self, action_id: str) -> ActionSpec | None:
        for a in self.actions:
            if a.id == action_id:
                return a
        return None

    @property
    def action_ids(self) -> list[str]:
        return [a.id for a in self.actions]


# --------------------------------------------------------------------------- #
#  Parsing
# --------------------------------------------------------------------------- #

def _parse_param(name: str, raw: Any) -> ParamSpec:
    """Parse a single param spec from YAML."""
    if isinstance(raw, dict):
        return ParamSpec(
            name=name,
            description=str(raw.get("description", "")),
            required=bool(raw.get("required", False)),
            default=str(raw["default"]) if "default" in raw else None,
            const=str(raw["const"]) if "const" in raw else None,
            enum=[str(v) for v in raw["enum"]] if "enum" in raw else None,
        )
    # Shorthand: `param_name: true` means required, `param_name: "value"` means const
    if raw is True:
        return ParamSpec(name=name, required=True)
    if isinstance(raw, (str, int, float)):
        return ParamSpec(name=name, const=str(raw))
    return ParamSpec(name=name)


def _parse_action(raw: dict) -> ActionSpec | None:
    """Parse one action dict from YAML. Returns None if malformed."""
    aid = raw.get("id")
    if not aid or not isinstance(aid, str):
        return None
    method = str(raw.get("method", "GET")).upper()
    if method not in VALID_METHODS:
        return None

    params: dict[str, ParamSpec] = {}
    raw_params = raw.get("params") or {}
    if isinstance(raw_params, dict):
        for pname, pval in raw_params.items():
            params[pname] = _parse_param(pname, pval)

    headers: dict[str, str] = {}
    raw_headers = raw.get("headers") or {}
    if isinstance(raw_headers, dict):
        headers = {str(k): str(v) for k, v in raw_headers.items()}

    return ActionSpec(
        id=aid,
        description=str(raw.get("description", "")),
        method=method,
        url=str(raw.get("url", "")),
        command=str(raw.get("command", "")),
        params=params,
        headers=headers,
        response_shape=str(raw.get("response_shape", "")),
    )


def _resolve_effect_trust(meta: dict) -> tuple[str, str]:
    """Extract effect/trust, falling back to legacy risk mapping."""
    effect = meta.get("effect", "")
    trust = meta.get("trust", "")
    if effect in VALID_EFFECTS and trust in VALID_TRUSTS:
        return effect, trust
    # Fall back to legacy risk field.
    risk = meta.get("risk", "low")
    return _RISK_MIGRATION.get(risk, (EFFECT_READ_ONLY, TRUST_SUBSCRIPTION))


def parse_manifest(raw_text: str) -> PluginManifest | None:
    """Parse a full plugin markdown file and extract the structured manifest.

    Returns None if the plugin has no ``actions:`` block (legacy plugin).
    """
    lines = raw_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None

    fm_text = "\n".join(lines[1:end])
    try:
        meta = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return None

    if not isinstance(meta, dict):
        return None

    raw_actions = meta.get("actions")
    if not raw_actions or not isinstance(raw_actions, list):
        return None  # no actions → legacy plugin

    actions = []
    for raw in raw_actions:
        if isinstance(raw, dict):
            spec = _parse_action(raw)
            if spec is not None:
                actions.append(spec)

    if not actions:
        return None

    plugin_id = str(meta.get("id", ""))
    effect, trust = _resolve_effect_trust(meta)

    return PluginManifest(
        plugin_id=plugin_id,
        effect=effect,
        trust=trust,
        actions=actions,
    )
