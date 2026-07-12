"""Goal -> principles -> oracle triangulation contract tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import ValidationError

import chitra.dispatchd as dispatchd_mod
import chitra.reasoning as reasoning_mod
from chitra.dispatch import DispatchOrder, DispatchResult, DispatchStatus
from chitra.dispatchd import run_once
from chitra.goals import GoalRecord
from chitra.principles import load_index
from chitra.reasoning import (
    DecisionRoute,
    FrozenGoal,
    GoalClauseRef,
    GoalJudgment,
    OracleVerdict,
    PrincipleJudgment,
    ReasoningError,
    RiskContract,
    TriangulationRequest,
    apply_oracle_verdict,
    reason,
)


def _goal() -> FrozenGoal:
    return FrozenGoal.from_record(
        GoalRecord(
            session_ref="trailhead:lane:0.0",
            intent="Deliver a reviewed implementation without changing the live deployed service.",
            goal="Build the deterministic answer and nudge reasoning module.",
            done_when="A pull request and focused tests demonstrate triangulation.",
            scope="Code, compiled principles, tests, and pull request only.",
            source="task-file:/tmp/triangulation.md",
            status="working",
        )
    )


def _risk(
    risk_class: Literal["A0", "A1", "A2", "A3"] = "A1",
    *,
    genuinely_ambiguous: bool = False,
    hard_to_reverse: bool = False,
) -> RiskContract:
    return RiskContract(
        risk_class=risk_class,
        genuinely_ambiguous=genuinely_ambiguous,
        hard_to_reverse=hard_to_reverse,
        consequence="test decision",
    )


def test_goal_only_answer_never_queries_principles(monkeypatch: pytest.MonkeyPatch) -> None:
    goal = _goal()
    request = TriangulationRequest(
        question="May this task deploy the change?",
        answer_category="action",
        goal=goal,
        goal_judgment=GoalJudgment(
            determination="determines",
            answer="Do not deploy; deliver a pull request only.",
            rationale="The frozen scope explicitly limits delivery.",
            clause_refs=(GoalClauseRef(field="scope", quote="pull request only"),),
        ),
        risk=_risk(),
    )

    def unexpected_lookup(*args: object, **kwargs: object) -> tuple[()]:
        raise AssertionError("principles must not be queried when the goal settles the question")

    monkeypatch.setattr(reasoning_mod, "lookup_principles", unexpected_lookup)
    outcome = reason(request, load_index())

    assert outcome.route == DecisionRoute.GOAL
    assert outcome.answer == "Do not deploy; deliver a pull request only."
    assert [item.source for item in outcome.provenance] == ["goal"]


def test_principle_answer_fills_only_the_goal_gap() -> None:
    goal = _goal()
    request = TriangulationRequest(
        question="Should this new service use structlog logging with keyword context?",
        answer_category="architecture",
        scopes=("engineering",),
        goal=goal,
        goal_judgment=GoalJudgment(
            determination="insufficient",
            rationale="The goal requires a module but does not choose a logging library.",
            clause_refs=(GoalClauseRef(field="goal", quote="reasoning module"),),
        ),
        principle_judgment=PrincipleJudgment(
            answer="Use structlog with keyword context.",
            principle_ids=("A08",),
            rationale="The selected binding architecture principle names structlog.",
        ),
        risk=_risk(),
    )

    outcome = reason(request, load_index())

    assert outcome.route == DecisionRoute.PRINCIPLES
    assert outcome.answer == "Use structlog with keyword context."
    assert [item.source for item in outcome.provenance] == ["goal", "principle"]
    assert outcome.provenance[-1].reference == "A08"


def test_oracle_escalates_only_when_goal_and_principles_are_insufficient() -> None:
    goal = _goal()
    request = TriangulationRequest(
        question="Choose the permanent tenant sharding key for an irreversible schema migration.",
        answer_category="architecture",
        scopes=("engineering",),
        goal=goal,
        goal_judgment=GoalJudgment(
            determination="insufficient",
            rationale="The frozen task never chooses a tenancy or schema boundary.",
            clause_refs=(GoalClauseRef(field="scope", quote="Code, compiled principles, tests"),),
        ),
        risk=_risk("A3", genuinely_ambiguous=True, hard_to_reverse=True),
    )

    pending = reason(request, load_index())

    assert pending.route == DecisionRoute.ORACLE_ESCALATION
    assert pending.answer is None
    assert pending.escalation is not None
    assert pending.escalation.agent == "oracle"
    assert pending.operator_confirmation_required is True
    assert all(item.source != "oracle-escalated" for item in pending.provenance)

    verdict = OracleVerdict(
        escalation_id=pending.escalation.escalation_id,
        verdict="Use a stable tenant UUID, subject to operator approval.",
        reasoning="The choice is durable and authority-sensitive.",
        citations=("schema.py:42",),
    )
    settled = apply_oracle_verdict(pending, verdict)
    assert settled.route == DecisionRoute.ORACLE_ANSWER
    assert settled.provenance[-1].source == "oracle-escalated"
    assert settled.operator_confirmation_required is True


def test_routine_unsettled_question_abstains_without_oracle() -> None:
    goal = _goal()
    request = TriangulationRequest(
        question="Which harmless label color should be used?",
        answer_category="cosmetic",
        goal=goal,
        goal_judgment=GoalJudgment(
            determination="insufficient",
            rationale="The frozen goal does not mention cosmetic labels.",
            clause_refs=(GoalClauseRef(field="goal", quote="reasoning module"),),
        ),
        risk=_risk("A0"),
    )

    outcome = reason(request, load_index())

    assert outcome.route == DecisionRoute.ABSTAIN
    assert outcome.escalation is None


def test_reasoned_dispatch_requires_exact_attested_text() -> None:
    goal = _goal()
    outcome = reason(
        TriangulationRequest(
            question="May this task deploy the change?",
            answer_category="action",
            goal=goal,
            goal_judgment=GoalJudgment(
                determination="determines",
                answer="Do not deploy; deliver a pull request only.",
                rationale="Scope settles it.",
                clause_refs=(GoalClauseRef(field="scope", quote="pull request only"),),
            ),
            risk=_risk(),
        ),
        load_index(),
    )
    attestation = outcome.to_attestation()

    order = DispatchOrder(
        order_id="reasoned-1",
        session_ref="trailhead:lane:0.0",
        nudge=attestation.approved_text,
        message_kind="reasoned_nudge",
        decision_attestation=attestation,
    )
    assert order.decision_attestation == attestation

    with pytest.raises(ValidationError, match="exactly match"):
        DispatchOrder(
            order_id="reasoned-2",
            session_ref="trailhead:lane:0.0",
            nudge="mutated after review",
            message_kind="reasoned_nudge",
            decision_attestation=attestation,
        )


def test_reasoned_dispatch_result_exposes_decision_lineage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    goal = _goal()
    outcome = reason(
        TriangulationRequest(
            question="May this task deploy the change?",
            answer_category="action",
            goal=goal,
            goal_judgment=GoalJudgment(
                determination="determines",
                answer="Do not deploy; deliver a pull request only.",
                rationale="Scope settles it.",
                clause_refs=(GoalClauseRef(field="scope", quote="pull request only"),),
            ),
            risk=_risk(),
        ),
        load_index(),
    )
    attestation = outcome.to_attestation()
    order = DispatchOrder(
        order_id="reasoned-result",
        session_ref="trailhead:lane:0.0",
        nudge=attestation.approved_text,
        message_kind="reasoned_nudge",
        decision_attestation=attestation,
    )
    orders_dir = tmp_path / "queue" / "orders"
    orders_dir.mkdir(parents=True)
    (orders_dir / "reasoned-result.json").write_text(order.model_dump_json(), encoding="utf-8")

    def fake_dispatch(pending: DispatchOrder, **kwargs: Any) -> DispatchResult:
        return DispatchResult(order_id=pending.order_id, session_ref=pending.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)
    results = run_once(
        tmp_path / "queue",
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
    )

    assert results[0].decision_id == outcome.decision_id
    assert results[0].goal_contract_id == goal.contract_id
    assert results[0].corpus_id == outcome.corpus_id
    assert results[0].decision_route == "goal"


def test_oracle_verdict_must_match_pending_escalation() -> None:
    goal = _goal()
    pending = reason(
        TriangulationRequest(
            question="Choose an irreversible schema boundary.",
            answer_category="architecture",
            goal=goal,
            goal_judgment=GoalJudgment(
                determination="insufficient",
                rationale="No schema choice exists in the goal.",
                clause_refs=(GoalClauseRef(field="scope", quote="tests"),),
            ),
            risk=_risk("A3", hard_to_reverse=True),
        ),
        load_index(),
    )

    with pytest.raises(ReasoningError, match="different escalation"):
        apply_oracle_verdict(
            pending,
            OracleVerdict(
                escalation_id="sha256:" + ("0" * 64),
                verdict="No.",
                reasoning="Mismatch.",
                citations=("schema.py:1",),
            ),
        )
