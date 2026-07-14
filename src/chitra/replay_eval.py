"""replay_eval — deterministic policy evaluator for labeled synthetic fixtures."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from .completion_gate import CompletionEvidence, TodoItem, evaluate_completion_claim
from .dispatch import directive_voice_violation
from .policy_config import PolicyConfig, load_policy_config
from .taxonomy import load_taxonomy


class CompletionFixtureCase(BaseModel):
    """One labeled completion-gate replay case."""

    kind: Literal["completion"]
    id: str
    todo_items: list[TodoItem]
    transcript_text: str
    evidence: list[CompletionEvidence]
    open_asks: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    expected_verdict: Literal["CLEAN", "COMPLETION_DISPUTE"]


class VoiceFixtureCase(BaseModel):
    """One labeled directive-voice replay case."""

    kind: Literal["voice"]
    id: str
    nudge: str
    expected_blocked: bool


FixtureCase = Annotated[CompletionFixtureCase | VoiceFixtureCase, Field(discriminator="kind")]
_FIXTURE_CASE_ADAPTER: TypeAdapter[FixtureCase] = TypeAdapter(FixtureCase)


def load_fixture_cases(fixtures_dir: Path) -> list[FixtureCase]:
    """Load every JSONL case under ``fixtures_dir``, failing with file+line context."""
    cases: list[FixtureCase] = []
    for path in sorted(fixtures_dir.glob("*.jsonl")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                cases.append(_FIXTURE_CASE_ADAPTER.validate_json(line))
            except Exception as exc:
                raise ValueError(f"invalid fixture at {path}:{line_number}: {exc}") from exc
    return cases


def evaluate_fixtures(cases: list[FixtureCase], policy: PolicyConfig) -> dict[str, float]:
    """Score fixtures under ``policy`` using only deterministic chitra checks."""
    taxonomy = load_taxonomy(policy.completion_gate.taxonomy_path)
    voice_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in policy.dispatch.banned_attribution_patterns]
    correct = 0
    completion_total = completion_correct = 0
    clean_total = clean_disputes = 0
    dispute_total = missed_disputes = 0
    voice_total = voice_correct = 0
    for case in cases:
        if isinstance(case, CompletionFixtureCase):
            completion_total += 1
            verdict = evaluate_completion_claim(
                case.todo_items,
                case.transcript_text,
                case.evidence,
                taxonomy,
                policy=policy.completion_gate,
                open_asks=case.open_asks,
                blockers=case.blockers,
            ).verdict
            if verdict == case.expected_verdict:
                correct += 1
                completion_correct += 1
            if case.expected_verdict == "CLEAN":
                clean_total += 1
                clean_disputes += verdict == "COMPLETION_DISPUTE"
            else:
                dispute_total += 1
                missed_disputes += verdict == "CLEAN"
        else:
            voice_total += 1
            blocked = directive_voice_violation(case.nudge, patterns=voice_patterns) is not None
            if blocked == case.expected_blocked:
                correct += 1
                voice_correct += 1
    total = len(cases)
    false_dispute_rate = clean_disputes / clean_total if clean_total else 0.0
    missed_dispute_rate = missed_disputes / dispute_total if dispute_total else 0.0
    return {
        "metric": correct / total if total else 0.0,
        "completion_accuracy": completion_correct / completion_total if completion_total else 0.0,
        "voice_accuracy": voice_correct / voice_total if voice_total else 0.0,
        "false_dispute_rate": false_dispute_rate,
        "missed_dispute_rate": missed_dispute_rate,
        "non_false_dispute": 1.0 - false_dispute_rate,
        "total_seconds": 0.0,
    }


def format_metrics(metrics: dict[str, float]) -> str:
    """Format metrics in the generic fenced wire format consumed by harnesses."""
    return "\n".join(
        [
            "---",
            f"metric: {metrics['metric']:.4f}",
            f"completion_accuracy: {metrics['completion_accuracy']:.4f}",
            f"voice_accuracy: {metrics['voice_accuracy']:.4f}",
            f"false_dispute_rate: {metrics['false_dispute_rate']:.4f}",
            f"missed_dispute_rate: {metrics['missed_dispute_rate']:.4f}",
            f"non_false_dispute: {metrics['non_false_dispute']:.4f}",
            f"total_seconds: {metrics['total_seconds']:.4f}",
            "status: ok",
            "---",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    """Run the immutable evaluator and print its generic metric block."""
    parser = argparse.ArgumentParser(prog="python -m chitra.replay_eval")
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--policy-config", type=Path, default=None)
    args = parser.parse_args(argv)
    print(format_metrics(evaluate_fixtures(load_fixture_cases(args.fixtures), load_policy_config(args.policy_config))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
