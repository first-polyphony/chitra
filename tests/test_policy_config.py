"""Tests for optional policy.yaml and lazy persistent-state paths."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from chitra.completion_gate import _DEFERRAL_PHRASES
from chitra.policy_config import (
    POLICY_CONFIG_ENV_VAR,
    GuidancePolicy,
    LoadPolicy,
    PolicyConfig,
    PRReviewPolicy,
    UsagePolicy,
    load_policy_config,
    resolve_guidance,
)
from chitra.state_paths import default_ledger_key_path, default_ledger_path, default_queue_dir


def test_unconfigured_policy_is_the_current_shipped_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(POLICY_CONFIG_ENV_VAR, raising=False)
    policy = load_policy_config()
    assert policy.completion_gate.deferral_phrases == list(_DEFERRAL_PHRASES)
    assert policy.completion_gate.complete_todo_statuses == ["done"]
    assert policy.completion_gate.required_evidence == ["deploy", "live_verify"]
    assert "brief_gate_mode" not in policy.completion_gate.model_dump()
    assert policy.dispatch.banned_attribution_patterns == [
        r"\boperator\b",
        r"\bthe monitor\b",
        r"\bchitra (wants|says|needs|relays)\b",
    ]
    assert policy.dispatch.extra_idle_input_regexes == []
    assert policy.usage == UsagePolicy()
    assert policy.usage.model_dump() == {
        "pause_5h_pct": 92.0,
        "pause_7d_pct": 95.0,
        "warn_5h_pct": 80.0,
        "warn_7d_pct": 90.0,
        "max_running": None,
        "auto_resume": True,
    }
    assert policy.load == LoadPolicy()
    assert policy.pr_review == PRReviewPolicy()
    assert policy.pr_review.block_on_findings is False
    assert policy.pr_review.reviewer_count == 2


def test_usage_policy_loads_overrides_and_rejects_invalid_values(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "usage": {
                    "pause_5h_pct": 86.0,
                    "pause_7d_pct": 93.0,
                    "warn_5h_pct": 71.0,
                    "warn_7d_pct": 86.0,
                    "max_running": 3,
                    "auto_resume": False,
                }
            }
        ),
        encoding="utf-8",
    )
    assert load_policy_config(path).usage == UsagePolicy(
        pause_5h_pct=86.0,
        pause_7d_pct=93.0,
        warn_5h_pct=71.0,
        warn_7d_pct=86.0,
        max_running=3,
        auto_resume=False,
    )

    for usage in (
        {"pause_5h_pct": 0},
        {"pause_7d_pct": 101},
        {"warn_5h_pct": -1},
        {"warn_7d_pct": 101},
        {"warn_5h_pct": 86, "pause_5h_pct": 85},
        {"warn_7d_pct": 93, "pause_7d_pct": 92},
        {"max_running": 0},
    ):
        path.write_text(yaml.safe_dump({"usage": usage}), encoding="utf-8")
        with pytest.raises(ValueError):
            load_policy_config(path)


def test_load_policy_block_loads_overrides_and_rejects_inverted_ladders(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "load": {
                    "baseline_max_running": 10,
                    "l1_max_running": 7,
                    "l2_max_running": 5,
                    "l3_max_running": 2,
                    "consecutive_sweeps": 3,
                }
            }
        ),
        encoding="utf-8",
    )
    assert load_policy_config(path).load.baseline_max_running == 10
    assert load_policy_config(path).load.consecutive_sweeps == 3

    for invalid in (
        {"l3_mem_available_pct": 16},
        {"clear_memory_some_avg60": 11},
        {"l3_max_running": 7},
        {"consecutive_sweeps": 0},
    ):
        path.write_text(yaml.safe_dump({"load": invalid}), encoding="utf-8")
        with pytest.raises(ValueError):
            load_policy_config(path)


def test_pr_review_policy_loads_overrides_and_rejects_invalid_values(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(
        yaml.safe_dump({"pr_review": {"max_diff_lines": 200, "max_diff_files": 5, "reviewer_count": 3, "block_on_findings": True}}),
        encoding="utf-8",
    )
    loaded = load_policy_config(path).pr_review
    assert loaded.max_diff_lines == 200
    assert loaded.max_diff_files == 5
    assert loaded.reviewer_count == 3
    assert loaded.block_on_findings is True

    for pr_review in (
        {"max_diff_lines": 0},
        {"max_diff_files": 0},
        {"reviewer_count": 0},
        {"blast_radius_keywords": ["auth", "  "]},
    ):
        path.write_text(yaml.safe_dump({"pr_review": pr_review}), encoding="utf-8")
        with pytest.raises(ValueError):
            load_policy_config(path)


def test_policy_explicit_path_wins_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit.yaml"
    configured = tmp_path / "configured.yaml"
    explicit.write_text(yaml.safe_dump({"completion_gate": {"complete_todo_statuses": ["closed"]}}), encoding="utf-8")
    configured.write_text(yaml.safe_dump({"completion_gate": {"complete_todo_statuses": ["resolved"]}}), encoding="utf-8")
    monkeypatch.setenv(POLICY_CONFIG_ENV_VAR, str(configured))
    assert load_policy_config(explicit).completion_gate.complete_todo_statuses == ["closed"]


def test_policy_loads_from_environment_when_no_path_is_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configured = tmp_path / "policy.yaml"
    configured.write_text(yaml.safe_dump({"dispatch": {"extra_idle_input_regexes": ["READY"]}}), encoding="utf-8")
    monkeypatch.setenv(POLICY_CONFIG_ENV_VAR, str(configured))
    assert load_policy_config().dispatch.extra_idle_input_regexes == ["READY"]


def test_policy_configured_errors_are_not_silently_ignored(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        load_policy_config(tmp_path / "missing.yaml")
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("completion_gate: [", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        load_policy_config(malformed)


@pytest.mark.parametrize(
    "data",
    [
        {"completion_gate": {"required_evidence": ["unknown"]}},
        {"dispatch": {"banned_attribution_patterns": ["["]}},
        {"dispatch": {"extra_idle_input_regexes": ["["]}},
    ],
)
def test_policy_rejects_invalid_schema_values(tmp_path: Path, data: dict[str, object]) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_policy_config(path)


def test_state_paths_are_resolved_lazily_from_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CHITRA_STATE_DIR", str(tmp_path / "state"))
    assert default_queue_dir() == tmp_path / "state" / "queue"
    assert default_ledger_path() == tmp_path / "state" / "ledger.jsonl"
    assert default_ledger_key_path() == tmp_path / "state" / "ledger.key"


def test_policy_model_defaults_are_independent() -> None:
    first = PolicyConfig()
    second = PolicyConfig()
    first.completion_gate.deferral_phrases.append("custom")
    assert "custom" not in second.completion_gate.deferral_phrases


def test_resolve_guidance_uses_longest_component_boundary_prefix_and_default(tmp_path: Path) -> None:
    config = PolicyConfig(
        guidance=GuidancePolicy(
            canonical_decisions={
                "/opt": "/docs/opt.md",
                "/opt/acme": "/docs/acme.md",
                "default": "/docs/default.md",
            }
        )
    )

    assert resolve_guidance(config, Path("/opt/acme/chitra")) == Path("/docs/acme.md")
    assert resolve_guidance(config, Path("/opt/other")) == Path("/docs/opt.md")
    assert resolve_guidance(config, Path("/opt/acme-other/work")) == Path("/docs/opt.md")
    boundary_config = PolicyConfig(
        guidance=GuidancePolicy(canonical_decisions={"/opt/ac": "/docs/ac.md", "default": "/docs/default.md"})
    )
    assert resolve_guidance(boundary_config, Path("/opt/acme/x")) == Path("/docs/default.md")
    assert resolve_guidance(PolicyConfig(guidance=GuidancePolicy(canonical_decisions={"default": "/docs/default.md"})), tmp_path) == Path(
        "/docs/default.md"
    )
    assert resolve_guidance(PolicyConfig(), tmp_path) is None


def test_guidance_policy_rejects_empty_document_values() -> None:
    with pytest.raises(ValueError, match="canonical_decisions values"):
        GuidancePolicy(canonical_decisions={"default": ""})
