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


class PolicyConfig(BaseModel):
    """The complete optional policy.yaml schema."""

    completion_gate: GatePolicy = Field(default_factory=GatePolicy)
    dispatch: DispatchPolicy = Field(default_factory=DispatchPolicy)


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
