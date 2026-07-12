from __future__ import annotations

import json
from pathlib import Path

import pytest

from chitra.dispatch import DispatchOrder
from chitra.goals import GoalRecord
from chitra.reasoning import (
    DecisionQuestion,
    DecisionReasoner,
    GoalJudgment,
    OracleRequest,
    OracleVerdict,
    PrinciplesIndex,
    ReasoningContractError,
)


def _goal() -> GoalRecord:
    return GoalRecord(
        session_ref="localhost:lane:0.0",
        intent="Deliver a safe reviewed change without expanding operator authority",
        goal="Build the requested reasoning module on a feature branch",
        done_when="Tests pass and a pull request is opened",
        scope="Code tests and pull request only",
        source="task-file:/tmp/goal.md",
        status="working",
        goal_version=3,
    )


def _undetermined() -> GoalJudgment:
    return GoalJudgment(
        determines_answer=False,
        goal_fields=["scope"],
        inference="The goal leaves this implementation detail open.",
    )


def test_goal_judgment_wins_without_principle_or_oracle() -> None:
    calls: list[OracleRequest] = []

    def oracle(request: OracleRequest) -> OracleVerdict:
        calls.append(request)
        raise AssertionError("oracle must not be called")

    decision = DecisionReasoner(PrinciplesIndex()).decide(
        _goal(),
        GoalJudgment(
            determines_answer=True,
            answer="Open a pull request; do not deploy.",
            goal_fields=["done_when", "scope"],
            inference="The done-when and scope explicitly settle delivery.",
        ),
        DecisionQuestion(text="Should this be deployed?", expensive_to_reverse=True),
        oracle=oracle,
    )

    assert decision.answer == "Open a pull request; do not deploy."
    assert decision.provenance.source == "goal"
    assert decision.provenance.goal_version == 3
    assert decision.provenance.goal_fields == ["done_when", "scope"]
    assert decision.provenance.principle_ids == []
    assert calls == []


def test_principle_fills_goal_gap_without_oracle() -> None:
    calls: list[OracleRequest] = []

    def oracle(request: OracleRequest) -> OracleVerdict:
        calls.append(request)
        raise AssertionError("oracle must not be called")

    decision = DecisionReasoner(PrinciplesIndex()).decide(
        _goal(),
        _undetermined(),
        DecisionQuestion(text="Should we add a typed Pydantic schema at this model boundary?"),
        oracle=oracle,
    )

    assert decision.provenance.source == "principle"
    assert decision.provenance.principle_ids == ["A04"]
    assert "docs/agent_docs/rules/must-rules.md" in decision.provenance.principle_citations
    assert calls == []


def test_oracle_is_called_only_after_goal_and_principles_are_insufficient() -> None:
    calls: list[OracleRequest] = []

    def oracle(request: OracleRequest) -> OracleVerdict:
        calls.append(request)
        return OracleVerdict(
            verdict="Keep the existing wire format until the operator chooses a migration window.",
            evidence_refs=["infra/ccr/agents/oracle.md:1"],
            confidence_basis="The interface is expensive to reverse and neither governing source selects a format.",
        )

    decision = DecisionReasoner(PrinciplesIndex()).decide(
        _goal(),
        _undetermined(),
        DecisionQuestion(
            text="Choose between frobnicated wire formats quux and zorb",
            risk_class="a2",
            genuinely_ambiguous=True,
            expensive_to_reverse=True,
        ),
        oracle=oracle,
    )

    assert decision.provenance.source == "oracle-escalated"
    assert decision.provenance.oracle_escalated is True
    assert len(calls) == 1
    assert all(match.confidence < 0.75 for match in calls[0].principle_matches)
    assert len(decision.insufficiency_reasons) == 2


def test_routine_insufficiency_abstains_without_oracle() -> None:
    calls: list[OracleRequest] = []

    def oracle(request: OracleRequest) -> OracleVerdict:
        calls.append(request)
        raise AssertionError("oracle must not be called for routine residuals")

    decision = DecisionReasoner(PrinciplesIndex()).decide(
        _goal(),
        _undetermined(),
        DecisionQuestion(text="Which frobnicator nickname should we use?", risk_class="a1"),
        oracle=oracle,
    )

    assert decision.outcome == "abstain"
    assert decision.provenance.source == "abstained"
    assert calls == []


def test_corrupt_principles_lock_fails_closed(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "src/chitra/principles.lock.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["principles"][0]["guidance"] = "silently changed"
    corrupt = tmp_path / "principles.lock.json"
    corrupt.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReasoningContractError, match="corpus_id"):
        PrinciplesIndex(corrupt)


def test_incomplete_goal_fails_before_any_reasoning() -> None:
    incomplete = GoalRecord(
        session_ref="localhost:lane:0.0",
        goal="A goal with enough words to validate",
        done_when="A result is eventually made available",
        source="task-file:/tmp/goal.md",
        status="working",
    )

    with pytest.raises(ReasoningContractError, match="strict specification"):
        DecisionReasoner(PrinciplesIndex()).decide(
            incomplete,
            _undetermined(),
            DecisionQuestion(text="Should we proceed?"),
        )


def test_reasoned_dispatch_requires_exact_answer_and_provenance() -> None:
    decision = DecisionReasoner(PrinciplesIndex()).decide(
        _goal(),
        GoalJudgment(
            determines_answer=True,
            answer="Open a pull request; do not deploy.",
            goal_fields=["done_when", "scope"],
            inference="The goal explicitly settles delivery.",
        ),
        DecisionQuestion(text="Should this be deployed?"),
    )

    order = DispatchOrder(
        order_id="reasoned-1",
        session_ref="localhost:lane:0.0",
        nudge=decision.answer,
        decision_kind="answer",
        reasoning=decision,
    )
    assert order.reasoning is not None
    assert order.reasoning.provenance.source == "goal"

    with pytest.raises(ValueError, match="exactly match"):
        DispatchOrder(
            order_id="reasoned-2",
            session_ref="localhost:lane:0.0",
            nudge="mutated after review",
            decision_kind="answer",
            reasoning=decision,
        )


def test_reasoned_dispatch_cannot_omit_attestation() -> None:
    with pytest.raises(ValueError, match="requires reasoning provenance"):
        DispatchOrder(
            order_id="reasoned-3",
            session_ref="localhost:lane:0.0",
            nudge="Unattested autonomous answer",
            decision_kind="answer",
        )
