"""Global + per-project configuration."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

GLOBAL_CONFIG_DIR = Path.home() / ".config" / "xli"
GLOBAL_CONFIG_FILE = GLOBAL_CONFIG_DIR / "config.json"

PROJECT_DIR_NAME = ".xli"
PROJECT_CONFIG_FILE = "project.json"
MANIFEST_FILE = "manifest.json"

DEFAULT_MODEL = "grok-4-1-fast-reasoning"
DEFAULT_WORKER_MODEL = "grok-4-1-fast-non-reasoning"
DEFAULT_RETRIEVAL_MODE = "hybrid"

# Temperature defaults — orchestrator runs warmer (creative planning + tool
# strategy), workers run colder (precise, repeatable execution).
DEFAULT_ORCH_TEMP = 0.7
DEFAULT_WORKER_TEMP = 0.3

		# Follow-through temperature decay. After the first 2 iterations of a turn,
	# the orchestrator switches from creative-planning mode to execution mode:
	# tools chosen, context loaded, just need to act and verify. A/B testing (n=5)
	# showed ~7% mean iter reduction and ~5.5% token reduction but with *increased*
	# variance and no statistical significance (t~1.6). The dramatic "2-4× fewer
	# iters" and variance-compression claims were not reproduced; structural
	# changes (iteration cap, indent handling, system prompt) drove the bulk of
	# the 6× improvement from trace-005. Kept as a small marginal knob.
DEFAULT_FOLLOW_THROUGH_TEMP = 0.3
DEFAULT_FOLLOW_THROUGH_ITER = 2  # iters 1..N use base; iter N+1 onward decays


@dataclass
class KeyPair:
    api_key: str
    management_api_key: Optional[str] = None
    label: Optional[str] = None


# Single template emitted when the user has no config yet.
# management key lives ONLY in env (XAI_...). Never on disk. 42 keys are revocable. Don't panic.
CONFIG_TEMPLATE = {
    "_comment": (
        "REQUIRED: export XAI_MANAGEMENT_API_KEY=... in your shell before running xli. "
        "It is the only privileged credential and should never live in a file. "
        "Run `xli setup` to provision chat keys; they will be saved here. "
        "`pricing` is optional — fill in per-model rates from your xAI dashboard."
    ),
    "orchestrator_model": DEFAULT_MODEL,
    "worker_model": DEFAULT_WORKER_MODEL,
    "model": DEFAULT_MODEL,
    "orchestrator_temperature": DEFAULT_ORCH_TEMP,
    "worker_temperature": DEFAULT_WORKER_TEMP,
    "follow_through_temperature": DEFAULT_FOLLOW_THROUGH_TEMP,
    "follow_through_iter_threshold": DEFAULT_FOLLOW_THROUGH_ITER,
    "retrieval_mode": DEFAULT_RETRIEVAL_MODE,
    "team_id": "",
    "keys": [],
    "max_tool_iterations": 25,
    "max_worker_iterations": 10,
    "max_parallel_workers": 8,
    "max_file_bytes": 1_000_000,
    "pricing": {
        # Per-model rates in USD per million tokens. cached_input_per_million
        # is optional and defaults to input_per_million × 0.1 — a sensible
        # approximation matching the OpenAI-compatible prompt-cache discount.
        # Long agent turns frequently hit 80-95% cache, so leaving this off
        # over-states real cost by ~4×. Verify against your xAI dashboard.
        # "grok-4-1-fast-reasoning":     {"input_per_million": 0.0, "cached_input_per_million": 0.0, "output_per_million": 0.0},
        # "grok-4-1-fast-non-reasoning": {"input_per_million": 0.0, "cached_input_per_million": 0.0, "output_per_million": 0.0},
    },
}


@dataclass
class GlobalConfig:
    """All credentials and tunables live here. One file, one source of truth.

    Keys are listed in `keys[]`; the first entry is the primary (used for
    sync + the main agent). Workers round-robin through the rest. A top-level
    `management_api_key` is read from the XAI_MANAGEMENT_API_KEY environment
    variable. It is intentionally NOT loaded from disk — that one credential
    is privileged (creates other keys, manages collections) and should never
    live in a config file. Chat keys in `keys[]` can be auto-provisioned via
    `xli setup` and are revocable via `xli bootstrap --revoke`.
    """
    keys: list = field(default_factory=list)            # list[dict] | list[str]
    management_api_key: Optional[str] = None             # default for all entries
    team_id: Optional[str] = None                        # for bootstrap key creation
    model: str = DEFAULT_MODEL                           # legacy fallback
    orchestrator_model: Optional[str] = None             # main agent model
    worker_model: Optional[str] = None                   # subagent model
    router_model: Optional[str] = None                   # cheap/fast classifier for initial chatter
    orchestrator_temperature: float = DEFAULT_ORCH_TEMP
    worker_temperature: float = DEFAULT_WORKER_TEMP
    follow_through_temperature: float = DEFAULT_FOLLOW_THROUGH_TEMP
    follow_through_iter_threshold: int = DEFAULT_FOLLOW_THROUGH_ITER
    retrieval_mode: str = DEFAULT_RETRIEVAL_MODE
    max_file_bytes: int = 1_000_000
    max_tool_iterations: int = 25  # Lower default + tool_choice="none" safety net + edit-failure rule bounds spirals. Progressive nudges at ~6/12/20.
    max_worker_iterations: int = 10
    max_parallel_workers: int = 8
    pricing: dict = field(default_factory=dict)          # model -> {input_per_million, output_per_million}
    models_detected_at: Optional[str] = None             # ISO ts; set by `xli setup` auto-detection
    secondary_ai: dict = field(default_factory=dict)     # {"provider": "anthropic"|"openai", "model": str, "api_key_env": str} — opt-in only, empty = off

    def orchestrator(self) -> str:
        """Model used by the main agent. Falls back to `model`."""
        return self.orchestrator_model or self.model

    def worker(self) -> str:
        """Model used by dispatched workers. Falls back to orchestrator/model."""
        return self.worker_model or self.orchestrator_model or self.model

    def router(self) -> str:
        """Cheap/fast model used for initial chatter classification before full orchestrator."""
        return self.router_model or self.worker_model or self.orchestrator_model or self.model

    def orchestrator_temp(self) -> float:
        """Orchestrator sampling temperature. Use `is not None` so a deliberate
        0.0 (deterministic mode) is preserved instead of being coerced."""
        return (
            self.orchestrator_temperature
            if self.orchestrator_temperature is not None
            else DEFAULT_ORCH_TEMP
        )

    def worker_temp(self) -> float:
        return (
            self.worker_temperature
            if self.worker_temperature is not None
            else DEFAULT_WORKER_TEMP
        )

    def get_model_for_role(self, role: str = "orchestrator") -> str:
        """Single dispatcher used by Agent and WorkerAgent so the call sites
        are uniform and any future role (e.g. 'planner') only needs one
        new branch here."""
        if role == "worker":
            return self.worker()
        if role == "orchestrator":
            return self.orchestrator()
        if role == "router":
            return self.router()
        raise ValueError(f"unknown model role: {role!r}")

    @classmethod
    def load(cls) -> "GlobalConfig":
        cfg = cls()
        if GLOBAL_CONFIG_FILE.exists():
            data = json.loads(GLOBAL_CONFIG_FILE.read_text())
            for k, v in data.items():
                if hasattr(cfg, k) and v is not None:
                    setattr(cfg, k, v)
        # Management key: env always wins. We deliberately ignore it from the
        # file (security: never persist privileged creds on disk). If a legacy
        # config still has one, callers can detect that with `mgmt_key_in_file()`.
        env_mgmt = os.environ.get("XAI_MANAGEMENT_API_KEY", "").strip()
        cfg.management_api_key = env_mgmt or None
        return cfg

    @staticmethod
    def mgmt_key_in_file() -> bool:
        """True if the on-disk config still contains a non-empty management_api_key.
        Used to surface a legacy/security warning."""
        if not GLOBAL_CONFIG_FILE.exists():
            return False
        try:
            data = json.loads(GLOBAL_CONFIG_FILE.read_text())
        except Exception:
            return False
        v = data.get("management_api_key")
        return bool(v and isinstance(v, str) and v.strip())

    def save(self) -> None:
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        GLOBAL_CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def write_template(cls) -> Path:
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not GLOBAL_CONFIG_FILE.exists():
            GLOBAL_CONFIG_FILE.write_text(json.dumps(CONFIG_TEMPLATE, indent=2))
        GLOBAL_CONFIG_FILE.chmod(0o600)
        return GLOBAL_CONFIG_FILE

    def key_pairs(self) -> list[KeyPair]:
        """Resolve `keys[]` into KeyPair list, primary first."""
        out: list[KeyPair] = []
        for i, entry in enumerate(self.keys):
            label = None
            if isinstance(entry, str):
                api_key, mgmt = entry, None
            elif isinstance(entry, dict):
                api_key = entry.get("api_key") or ""
                mgmt = entry.get("management_api_key")
                label = entry.get("label")
            else:
                continue
            if not api_key:
                continue
            out.append(
                KeyPair(
                    api_key=api_key,
                    management_api_key=mgmt or self.management_api_key,
                    label=label or ("primary" if i == 0 else f"k{i}"),
                )
            )
        return out


@dataclass
class ProjectConfig:
    project_root: Path
    name: str
    collection_id: str
    created_at: str
    extra_ignores: list[str] = field(default_factory=list)
    conversation_id: Optional[str] = None  # stable per-project UUID; used for xAI prompt-cache key
    # Local-only mode: no remote Collection is provisioned and sync is a no-op.
    # Tools available are everything except search_project (no RAG index to query).
    # Use for ad-hoc / file-management workflows in directories you don't want to
    # upload (private docs, mixed-content folders, ephemeral scratch).
    local_only: bool = False

    @property
    def xli_dir(self) -> Path:
        return self.project_root / PROJECT_DIR_NAME

    @property
    def config_path(self) -> Path:
        return self.xli_dir / PROJECT_CONFIG_FILE

    @property
    def manifest_path(self) -> Path:
        return self.xli_dir / MANIFEST_FILE

    @classmethod
    def load(cls, project_root: Path) -> Optional["ProjectConfig"]:
        import uuid

        path = project_root / PROJECT_DIR_NAME / PROJECT_CONFIG_FILE
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        cfg = cls(
            project_root=project_root,
            name=data["name"],
            collection_id=data["collection_id"],
            created_at=data["created_at"],
            extra_ignores=data.get("extra_ignores", []),
            conversation_id=data.get("conversation_id"),
            local_only=data.get("local_only", False),
        )
        # Backfill: legacy projects (created before this field existed) get a
        # stable UUID assigned + persisted on first load. From then on it stays.
        if not cfg.conversation_id:
            cfg.conversation_id = uuid.uuid4().hex
            cfg.save()
        return cfg

    def save(self) -> None:
        self.xli_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(
                {
                    "name": self.name,
                    "collection_id": self.collection_id,
                    "created_at": self.created_at,
                    "extra_ignores": self.extra_ignores,
                    "conversation_id": self.conversation_id,
                    "local_only": self.local_only,
                },
                indent=2,
            )
        )
