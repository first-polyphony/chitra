#!/usr/bin/env python3
"""Draft-only routing feedback usage report for chitra ledgers.

This tool is intentionally outside ``src/chitra``. It treats ``ledger.jsonl``
and ``routing.yaml`` as external data sources and never mutates chitra state or
the routing config. Chitra's current ledger proves delivery only; it has no
success/failure, judge score, human override, or task_type field. Because of
that, this script can report routing_hint usage frequency but refuses to draft a
real routing.yaml change.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

DEFAULT_MAX_AGE_HOURS = 168
DEFAULT_MIN_SAMPLES = 8
DEFAULT_MAX_CHANGED_LINES = 20
DEFAULT_MAX_HINT_SHARE = 0.80
NO_HINT = "__none__"


class FeedbackUsageError(Exception):
    """Raised when input artifacts are malformed enough to stop reporting."""


@dataclass(frozen=True)
class LedgerRecord:
    order_id: str
    session_ref: str
    tag: str
    routing_hint: str | None
    sent_at: datetime


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def now_utc(value: str | None = None) -> datetime:
    return parse_time(value) or datetime.now(UTC)


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def iter_ledger_records(path: Path) -> tuple[list[LedgerRecord], dict[str, int]]:
    """Read the delivery ledger without importing chitra package internals."""
    stats = {"lines": 0, "records": 0, "malformed": 0, "missing_required": 0, "invalid_sent_at": 0}
    records: list[LedgerRecord] = []
    if not path.exists():
        return records, stats
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            stats["lines"] += 1
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                stats["malformed"] += 1
                continue
            if not isinstance(item, dict):
                stats["malformed"] += 1
                continue
            order_id = _string_or_none(item.get("order_id"))
            session_ref = _string_or_none(item.get("session_ref"))
            tag = _string_or_none(item.get("tag"))
            sent_at = parse_time(_string_or_none(item.get("sent_at")))
            if order_id is None or session_ref is None or tag is None:
                stats["missing_required"] += 1
                continue
            if sent_at is None:
                stats["invalid_sent_at"] += 1
                continue
            records.append(
                LedgerRecord(
                    order_id=order_id,
                    session_ref=session_ref,
                    tag=tag,
                    routing_hint=_string_or_none(item.get("routing_hint")),
                    sent_at=sent_at,
                )
            )
            stats["records"] += 1
    return records, stats


def load_routing_defaults(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise FeedbackUsageError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise FeedbackUsageError(f"{path} must contain a YAML mapping")
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise FeedbackUsageError(f"{path} defaults must be a mapping")
    return {str(task_type): str(routing_hint) for task_type, routing_hint in defaults.items()}


def counter_rows(counter: Counter[str], *, total: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for value, count in counter.most_common():
        rows.append({"value": None if value == NO_HINT else value, "count": count, "share": count / total if total else 0.0})
    return rows


def analyze(
    *,
    ledger_path: Path,
    routing_yaml: Path | None,
    output_dir: Path,
    generated_at: datetime,
    max_age_hours: int,
    min_samples: int,
    max_changed_lines: int,
    max_hint_share: float,
) -> dict[str, Any]:
    records, ledger_stats = iter_ledger_records(ledger_path)
    routing_defaults = load_routing_defaults(routing_yaml) if routing_yaml is not None else {}
    cutoff = generated_at - timedelta(hours=max_age_hours)
    fresh = [record for record in records if record.sent_at >= cutoff]
    stale_count = len(records) - len(fresh)
    hint_counter: Counter[str] = Counter(record.routing_hint or NO_HINT for record in fresh)
    session_counter: Counter[str] = Counter(record.session_ref for record in fresh)
    tag_counter: Counter[str] = Counter(record.tag for record in fresh)

    limitation_blockers = [
        "chitra ledger entries prove delivery only; they do not contain success, failure, judge_score, human_override, or task_type",
        "routing.yaml maps task_type to routing_hint, but ledger.jsonl records routing_hint only; no task_type diff can be justified",
    ]
    gate_blockers: list[str] = []
    if len(fresh) < min_samples:
        gate_blockers.append(f"fresh ledger sample count {len(fresh)} is below minimum {min_samples}")
    if hint_counter and max_hint_share > 0:
        value, count = hint_counter.most_common(1)[0]
        share = count / len(fresh)
        if share > max_hint_share:
            label = "no routing_hint" if value == NO_HINT else value
            gate_blockers.append(f"routing_hint distribution is skewed: {label!r} has {share:.0%} of fresh samples")

    configured_hint_counter: Counter[str] = Counter(routing_defaults.values())
    report = {
        "schema": "chitra.routing_feedback_usage_report.v1",
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "status": "report_only" if not gate_blockers else "blocked",
        "would_change_routing_yaml": False,
        "diff_changed_lines": 0,
        "limits": {
            "max_age_hours": max_age_hours,
            "min_samples": min_samples,
            "max_changed_lines": max_changed_lines,
            "max_hint_share": max_hint_share,
        },
        "sources": {
            "ledger_jsonl": {
                "path": str(ledger_path),
                "available": ledger_path.exists(),
                "total_records": len(records),
                "fresh_records": len(fresh),
                "stale_records": stale_count,
                "parse_stats": ledger_stats,
            },
            "routing_yaml": {
                "path": str(routing_yaml) if routing_yaml is not None else None,
                "available": routing_yaml.exists() if routing_yaml is not None else False,
                "default_count": len(routing_defaults),
            },
        },
        "routing_hint_usage": counter_rows(hint_counter, total=len(fresh)),
        "session_ref_usage": counter_rows(session_counter, total=len(fresh)),
        "tag_usage": counter_rows(tag_counter, total=len(fresh)),
        "configured_routing_hints": counter_rows(configured_hint_counter, total=len(routing_defaults)),
        "blockers": limitation_blockers + gate_blockers,
        "draft_notes": [
            "No routing.yaml diff was produced. This is a usage-frequency scaffold until an external judge records outcome telemetry.",
            "The tool is PR/draft-only: it writes report artifacts and never rewrites routing.yaml.",
        ],
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "routing-feedback-usage-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "routing-feedback.diff").write_text("", encoding="utf-8")
    (output_dir / "routing-feedback-pr-body.md").write_text(build_pr_body(report), encoding="utf-8")
    return report


def build_pr_body(report: dict[str, Any]) -> str:
    source = report["sources"]["ledger_jsonl"]
    lines = [
        "## Chitra Routing Feedback Usage Report",
        "",
        f"Status: `{report['status']}`",
        f"Generated at: `{report['generated_at']}`",
        "",
        "### Scope",
        "- Separate consumer tool outside the installable `chitra` package.",
        "- Draft-only: writes report artifacts and never rewrites `routing.yaml`.",
        "- Outcome limitation: ledger entries prove delivery only, not task success.",
        "",
        "### Ledger Window",
        f"- Path: `{source['path']}`",
        f"- Fresh records: `{source['fresh_records']}`",
        f"- Stale records excluded: `{source['stale_records']}`",
        f"- Minimum samples: `{report['limits']['min_samples']}`",
        "",
        "### Routing Hint Usage",
    ]
    usage = report.get("routing_hint_usage") or []
    if usage:
        for row in usage:
            label = row["value"] if row["value"] is not None else "(none)"
            lines.append(f"- `{label}`: {row['count']} ({row['share']:.0%})")
    else:
        lines.append("- No fresh ledger records.")
    lines.extend(["", "### Blockers"])
    lines.extend(f"- {blocker}" for blocker in report["blockers"])
    lines.extend(["", "### Diff", "- No routing.yaml diff was produced."])
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> int:
    generated_at = now_utc(args.now)
    try:
        analyze(
            ledger_path=Path(args.ledger_jsonl),
            routing_yaml=Path(args.routing_yaml) if args.routing_yaml else None,
            output_dir=Path(args.output_dir),
            generated_at=generated_at,
            max_age_hours=args.max_age_hours,
            min_samples=args.min_samples,
            max_changed_lines=args.max_changed_lines,
            max_hint_share=args.max_hint_share,
        )
    except FeedbackUsageError as exc:
        print(f"routing-feedback-usage: {exc}")
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger-jsonl", required=True)
    parser.add_argument("--routing-yaml")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--now")
    parser.add_argument("--max-age-hours", type=int, default=DEFAULT_MAX_AGE_HOURS)
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--max-changed-lines", type=int, default=DEFAULT_MAX_CHANGED_LINES)
    parser.add_argument("--max-hint-share", type=float, default=DEFAULT_MAX_HINT_SHARE)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
