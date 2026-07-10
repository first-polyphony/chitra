from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.routing_feedback.routing_feedback_usage import analyze


def _append_ledger(path: Path, *, order_id: str, sent_at: str, routing_hint: str | None = "sonnet") -> None:
    item = {
        "order_id": order_id,
        "session_ref": "localhost:s:0.0",
        "tag": "[C]",
        "routing_hint": routing_hint,
        "message_hash": "abc",
        "sent_at": sent_at,
        "signature": "sig",
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(item) + "\n")


def _write_routing(path: Path) -> Path:
    path.write_text(yaml.safe_dump({"defaults": {"code-review": "sonnet", "search": "haiku"}}), encoding="utf-8")
    return path


def test_usage_report_counts_fresh_routing_hints_and_never_drafts_diff(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    _append_ledger(ledger, order_id="o1", sent_at="2026-07-09T00:00:00+00:00", routing_hint="sonnet")
    _append_ledger(ledger, order_id="o2", sent_at="2026-07-09T01:00:00+00:00", routing_hint="haiku")
    _append_ledger(ledger, order_id="o3", sent_at="2026-07-09T02:00:00+00:00", routing_hint=None)
    out = tmp_path / "out"

    report = analyze(
        ledger_path=ledger,
        routing_yaml=_write_routing(tmp_path / "routing.yaml"),
        output_dir=out,
        generated_at=datetime(2026, 7, 9, 3, tzinfo=UTC),
        max_age_hours=24,
        min_samples=3,
        max_changed_lines=20,
        max_hint_share=0.80,
    )

    assert report["status"] == "report_only"
    assert report["would_change_routing_yaml"] is False
    assert report["diff_changed_lines"] == 0
    assert (out / "routing-feedback.diff").read_text(encoding="utf-8") == ""
    assert {row["value"] for row in report["routing_hint_usage"]} == {"sonnet", "haiku", None}
    assert "success, failure" in report["blockers"][0]


def test_usage_report_blocks_when_fresh_samples_are_thin(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    _append_ledger(ledger, order_id="old", sent_at="2026-07-01T00:00:00+00:00")
    _append_ledger(ledger, order_id="fresh", sent_at="2026-07-09T00:00:00+00:00")

    report = analyze(
        ledger_path=ledger,
        routing_yaml=None,
        output_dir=tmp_path / "out",
        generated_at=datetime(2026, 7, 9, 3, tzinfo=UTC),
        max_age_hours=24,
        min_samples=2,
        max_changed_lines=20,
        max_hint_share=0.80,
    )

    assert report["status"] == "blocked"
    assert report["sources"]["ledger_jsonl"]["fresh_records"] == 1
    assert report["sources"]["ledger_jsonl"]["stale_records"] == 1
    assert any("below minimum 2" in blocker for blocker in report["blockers"])


def test_usage_report_blocks_skewed_hint_distribution(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    for index in range(9):
        _append_ledger(ledger, order_id=f"sonnet-{index}", sent_at="2026-07-09T00:00:00+00:00", routing_hint="sonnet")
    _append_ledger(ledger, order_id="haiku-1", sent_at="2026-07-09T00:00:00+00:00", routing_hint="haiku")

    report = analyze(
        ledger_path=ledger,
        routing_yaml=None,
        output_dir=tmp_path / "out",
        generated_at=datetime(2026, 7, 9, 3, tzinfo=UTC),
        max_age_hours=24,
        min_samples=8,
        max_changed_lines=20,
        max_hint_share=0.80,
    )

    assert report["status"] == "blocked"
    assert any("distribution is skewed" in blocker for blocker in report["blockers"])


def test_malformed_ledger_lines_are_counted_not_promoted_to_evidence(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("{not json}\n", encoding="utf-8")
    _append_ledger(ledger, order_id="o1", sent_at="2026-07-09T00:00:00+00:00")

    report = analyze(
        ledger_path=ledger,
        routing_yaml=None,
        output_dir=tmp_path / "out",
        generated_at=datetime(2026, 7, 9, 3, tzinfo=UTC),
        max_age_hours=24,
        min_samples=1,
        max_changed_lines=20,
        max_hint_share=1.0,
    )

    assert report["sources"]["ledger_jsonl"]["parse_stats"]["malformed"] == 1
    assert report["sources"]["ledger_jsonl"]["fresh_records"] == 1
