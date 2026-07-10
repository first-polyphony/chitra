"""Tests for chitra.replay_eval's deterministic fixture evaluator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chitra.policy_config import PolicyConfig
from chitra.replay_eval import evaluate_fixtures, format_metrics, load_fixture_cases, main

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "replay"


def test_shipped_fixtures_have_the_exact_shipped_policy_metrics() -> None:
    metrics = evaluate_fixtures(load_fixture_cases(FIXTURES_DIR), PolicyConfig())
    assert (
        format_metrics(metrics)
        == """---
metric: 1.0000
completion_accuracy: 1.0000
voice_accuracy: 1.0000
false_dispute_rate: 0.0000
missed_dispute_rate: 0.0000
non_false_dispute: 1.0000
total_seconds: 0.0000
status: ok
---"""
    )


def test_main_prints_the_generic_metric_wire_format(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--fixtures", str(FIXTURES_DIR)]) == 0
    assert capsys.readouterr().out.startswith("---\nmetric: 1.0000\n")


def test_malformed_fixture_includes_its_file_and_line(tmp_path: Path) -> None:
    fixture = tmp_path / "bad.jsonl"
    fixture.write_text(json.dumps({"kind": "voice", "id": "ok", "nudge": "hi", "expected_blocked": False}) + "\n{bad", encoding="utf-8")
    with pytest.raises(ValueError, match=r"bad\.jsonl:2"):
        load_fixture_cases(tmp_path)
