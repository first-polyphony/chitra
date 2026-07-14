"""policy_config — optional deterministic completion-gate and dispatch policy."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Self

import structlog
import yaml
from pydantic import BaseModel, Field, model_validator

from chitra.completion_gate import _DEFERRAL_PHRASES

logger = structlog.get_logger(__name__)

POLICY_CONFIG_ENV_VAR = "CHITRA_POLICY_CONFIG"
_ALLOWED_EVIDENCE = frozenset({"deploy", "live_verify"})


class GatePolicy(BaseModel):
    """Configurable vocabulary and evidence requirements for the completion gate."""

    deferral_phrases: list[str] = Field(default_factory=lambda: list(_DEFERRAL_PHRASES))
    complete_todo_statuses: list[str] = Field(default_factory=lambda: ["done"])
    required_evidence: list[str] = Field(default_factory=lambda: ["deploy", "live_verify"])
    taxonomy_path: str | None = None

    @model_validator(mode="after")
    def validate_required_evidence(self) -> Self:
        """Reject evidence names the deterministic gate does not understand."""
        unknown = set(self.required_evidence) - _ALLOWED_EVIDENCE
        if unknown:
            raise ValueError(f"required_evidence contains unsupported values: {sorted(unknown)}")
        return self


class DispatchPolicy(BaseModel):
    """Configurable deterministic dispatch vocabulary."""

    banned_attribution_patterns: list[str] = Field(
        default_factory=lambda: [r"\boperator\b", r"\bthe monitor\b", r"\bchitra (wants|says|needs|relays)\b"]
    )
    extra_idle_input_regexes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_regexes(self) -> Self:
        """Fail at config load time for malformed policy regexes."""
        for pattern in self.banned_attribution_patterns + self.extra_idle_input_regexes:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
        return self


class UsagePolicy(BaseModel):
    """Operator-configurable thresholds and concurrency controls for usage."""

    pause_5h_pct: float = 92.0
    pause_7d_pct: float = 95.0
    warn_5h_pct: float = 80.0
    warn_7d_pct: float = 90.0
    max_running: int | None = None
    auto_resume: bool = True

    @model_validator(mode="after")
    def validate_thresholds(self) -> Self:
        """Reject impossible usage-policy values at configuration load time."""
        for name in ("pause_5h_pct", "pause_7d_pct", "warn_5h_pct", "warn_7d_pct"):
            value = getattr(self, name)
            if not 0 < value <= 100:
                raise ValueError(f"{name} must be greater than 0 and at most 100")
        if self.warn_5h_pct > self.pause_5h_pct:
            raise ValueError("warn_5h_pct must not exceed pause_5h_pct")
        if self.warn_7d_pct > self.pause_7d_pct:
            raise ValueError("warn_7d_pct must not exceed pause_7d_pct")
        if self.max_running is not None and self.max_running < 1:
            raise ValueError("max_running must be at least 1 when set")
        return self


class GuidancePolicy(BaseModel):
    """Map working-directory prefixes to canonical decision documents."""

    canonical_decisions: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_document_paths(self) -> Self:
        """Reject blank configured document paths before they reach the CLI."""
        if any(not document.strip() for document in self.canonical_decisions.values()):
            raise ValueError("canonical_decisions values must be non-empty strings")
        return self


class PausePolicy(BaseModel):
    """Bounded deadlines/attempts for ``chitra.rate_limit_guard``'s pause/resume
    transaction machine (see that module's docstring for the phase sequence).

    Every deadline is a ceiling on how long a transaction may sit in one
    in-progress phase before the sweep escalates instead of retrying forever
    -- "no strand-forever" (see docs/SOL-ADVERSARIAL-REVIEW finding #2).
    Escalating never clears a hold; the freeze only ever lifts once a resume
    is actually confirmed delivered.
    """

    checkpoint_deadline_seconds: int = 180
    stop_deadline_seconds: int = 180
    quiescence_quiet_seconds: int = 30
    quiescence_timeout_seconds: int = 900
    resume_deadline_seconds: int = 180
    max_retry_attempts: int = 3

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        """Reject non-positive deadlines/attempts at configuration load time."""
        for name in (
            "checkpoint_deadline_seconds",
            "stop_deadline_seconds",
            "quiescence_quiet_seconds",
            "quiescence_timeout_seconds",
            "resume_deadline_seconds",
            "max_retry_attempts",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        if self.quiescence_quiet_seconds > self.quiescence_timeout_seconds:
            raise ValueError("quiescence_quiet_seconds must not exceed quiescence_timeout_seconds")
        return self


class LoadPolicy(BaseModel):
    """Host-pressure ladder and concurrency caps for load shedding.

    Pressure is sampled on the host running ``chitra-rate-limit-guard``.  The
    active level is persisted per host, so this policy remains deterministic
    even though the guard itself is a one-shot CLI.
    """

    baseline_max_running: int = 8
    l1_max_running: int = 6
    l2_max_running: int = 4
    l3_max_running: int = 2
    l1_mem_available_pct: float = 25.0
    l2_mem_available_pct: float = 15.0
    l3_mem_available_pct: float = 8.0
    l1_memory_some_avg60: float = 10.0
    l2_memory_some_avg60: float = 25.0
    l3_memory_full_avg60: float = 10.0
    l1_cpu_some_avg60: float = 60.0
    clear_mem_available_pct: float = 30.0
    clear_memory_some_avg60: float = 5.0
    consecutive_sweeps: int = 2
    l3_pause: PausePolicy = Field(
        default_factory=lambda: PausePolicy(
            checkpoint_deadline_seconds=60,
            stop_deadline_seconds=60,
            quiescence_quiet_seconds=15,
            quiescence_timeout_seconds=300,
            resume_deadline_seconds=60,
            max_retry_attempts=3,
        )
    )

    @model_validator(mode="after")
    def validate_ladder(self) -> Self:
        """Reject inverted pressure thresholds or concurrency ladders."""
        if not 0 < self.l3_mem_available_pct < self.l2_mem_available_pct < self.l1_mem_available_pct:
            raise ValueError("load MemAvailable thresholds must increase from L3 through L1")
        if not self.l1_mem_available_pct < self.clear_mem_available_pct <= 100:
            raise ValueError("clear_mem_available_pct must exceed the L1 threshold and be at most 100")
        if not 0 <= self.clear_memory_some_avg60 < self.l1_memory_some_avg60 < self.l2_memory_some_avg60:
            raise ValueError("load memory PSI some thresholds must increase from clear through L2")
        if self.l3_memory_full_avg60 <= 0 or self.l1_cpu_some_avg60 <= 0:
            raise ValueError("load PSI thresholds must be positive")
        if not 1 <= self.l3_max_running <= self.l2_max_running <= self.l1_max_running <= self.baseline_max_running:
            raise ValueError("load running caps must increase from L3 through baseline")
        if self.consecutive_sweeps < 1:
            raise ValueError("load consecutive_sweeps must be at least 1")
        return self


class PolicyConfig(BaseModel):
    """The complete optional policy.yaml schema."""

    completion_gate: GatePolicy = Field(default_factory=GatePolicy)
    dispatch: DispatchPolicy = Field(default_factory=DispatchPolicy)
    usage: UsagePolicy = Field(default_factory=UsagePolicy)
    guidance: GuidancePolicy = Field(default_factory=GuidancePolicy)
    pause: PausePolicy = Field(default_factory=PausePolicy)
    load: LoadPolicy = Field(default_factory=LoadPolicy)


def resolve_guidance(config: PolicyConfig, cwd: Path) -> Path | None:
    """Return the canonical decision document for ``cwd`` by longest prefix."""
    resolved_cwd = cwd.expanduser().resolve().as_posix()
    matches: list[tuple[int, str]] = []
    for prefix, document in config.guidance.canonical_decisions.items():
        if prefix == "default":
            continue
        resolved_prefix = Path(prefix).expanduser().resolve().as_posix()
        if resolved_cwd == resolved_prefix or (
            resolved_prefix != "/" and resolved_cwd.startswith(f"{resolved_prefix}/")
        ) or (resolved_prefix == "/" and resolved_cwd.startswith("/")):
            matches.append((len(resolved_prefix), document))
    if matches:
        return Path(max(matches, key=lambda match: match[0])[1]).expanduser()
    default = config.guidance.canonical_decisions.get("default")
    return Path(default).expanduser() if default is not None else None


def load_policy_config(path: Path | None = None) -> PolicyConfig:
    """Load policy from ``path`` or ``CHITRA_POLICY_CONFIG``.

    With neither set, return shipped defaults. A configured unreadable,
    malformed, or invalid file is logged and re-raised as a configuration
    error rather than being silently ignored.
    """
    resolved = path
    if resolved is None:
        env_value = os.environ.get(POLICY_CONFIG_ENV_VAR)
        resolved = Path(env_value) if env_value else None
    if resolved is None:
        return PolicyConfig()
    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("chitra_policy_config_unreadable", path=str(resolved), error=str(exc))
        raise
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        logger.error("chitra_policy_config_malformed", path=str(resolved), error=str(exc))
        raise
    try:
        return PolicyConfig.model_validate(data or {})
    except Exception as exc:
        logger.error("chitra_policy_config_invalid_schema", path=str(resolved), error=str(exc))
        raise
