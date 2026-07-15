"""Produce reviewed corrective dispatches through the reasoning engine.

Decided defaults: accepted completion reviews emit nothing. Rejected
unsupported or hedged completion claims produce ``reasoned_action`` orders;
rejected goal drift produces a ``reasoned_nudge`` that restores the frozen
goal. Watchd supplies the operator-approved confirmation for that narrow
review-rejection correction. Abstentions and decisions carrying any additional
operator gate are never dispatched. The oracle is injected, and the default
oracle only escalates consequential residuals for operator confirmation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from chitra.dispatch import DispatchOrder
from chitra.goal_enforcement import FindingCode, SessionReviewSignal
from chitra.goals import GoalRecord, done_when_with_delta
from chitra.reasoning import (
    DecisionQuestion,
    DecisionReasoner,
    GoalJudgment,
    Oracle,
    OracleRequest,
    OracleVerdict,
    PrinciplesIndex,
)

_COMPLETION_FINDINGS: frozenset[FindingCode] = frozenset({"hedged_completion", "unsupported_completion"})
_DRIFT_FINDINGS: frozenset[FindingCode] = frozenset({"goal_drift", "smuggled_redirect"})
_REVIEW_REJECTION_GATE = "watched-session review rejected the lane behavior"


def abstaining_oracle(request: OracleRequest) -> OracleVerdict:
    """Escalate a consequential residual without inventing an autonomous answer."""
    evidence_refs = request.question.evidence_refs
    if not evidence_refs:
        review = request.question.session_review
        evidence_refs = [review.signal_id if review is not None else "chitra:abstaining-oracle"]
    return OracleVerdict(
        verdict="Explicit operator confirmation is required because the available evidence does not settle this decision.",
        evidence_refs=evidence_refs,
        confidence_basis="the core oracle abstains from autonomous answers when goal and principles are insufficient",
    )


def _question_and_judgment(
    goal: GoalRecord,
    review_signal: SessionReviewSignal,
) -> tuple[DecisionQuestion, GoalJudgment]:
    finding_codes = {finding.code for finding in review_signal.findings}
    evidence_refs = [finding.citation for finding in review_signal.findings]
    if finding_codes & _DRIFT_FINDINGS:
        answer = (
            "The completed turn did not pass review because it moved away from the frozen goal. "
            f"Continue against the frozen goal: {goal.goal}. Stay within scope: {goal.scope}."
        )
        return (
            DecisionQuestion(
                text="Should the rejected lane direction be corrected back to the frozen goal?",
                answer_category="nudge",
                evidence_refs=evidence_refs,
                session_review=review_signal,
            ),
            GoalJudgment(
                determines_answer=True,
                answer=answer,
                goal_fields=["goal", "scope"],
                inference="The frozen goal and scope directly determine the corrective direction.",
            ),
        )
    if finding_codes & _COMPLETION_FINDINGS:
        answer = (
            "The completion claim did not pass review. "
            f"Continue against the frozen goal: {goal.goal}. "
            f"Satisfy the completion condition before claiming completion: {done_when_with_delta(goal)}. "
            f"Stay within scope: {goal.scope}."
        )
        return (
            DecisionQuestion(
                text="Should the rejected completion claim continue against the frozen goal and completion condition?",
                answer_category="action",
                evidence_refs=evidence_refs,
                session_review=review_signal,
            ),
            GoalJudgment(
                determines_answer=True,
                answer=answer,
                goal_fields=["goal", "done_when", "scope"],
                inference="The frozen goal, completion condition, and scope directly determine the corrective action.",
            ),
        )
    return (
        DecisionQuestion(
            text="How should an unclassified adverse review finding be handled?",
            answer_category="action",
            evidence_refs=evidence_refs,
            session_review=review_signal,
        ),
        GoalJudgment(
            determines_answer=False,
            goal_fields=["goal", "done_when", "scope"],
            inference="The adverse finding is not a completion or goal-drift decision settled by the frozen goal.",
        ),
    )


def build_reasoned_dispatch(
    goal: GoalRecord,
    review_signal: SessionReviewSignal,
    *,
    principles: PrinciplesIndex,
    oracle: Oracle = abstaining_oracle,
    now: datetime | None = None,
    review_rejection_confirmed: bool = True,
) -> DispatchOrder | None:
    """Build one attested corrective order, or fail closed with no dispatch."""
    if review_signal.verdict == "accept":
        return None

    question, judgment = _question_and_judgment(goal, review_signal)
    oracle_warranted = question.genuinely_ambiguous or question.expensive_to_reverse or question.risk_class in ("a2", "a3")
    attestation = DecisionReasoner(principles).decide(
        goal,
        judgment,
        question,
        oracle=oracle if oracle_warranted else None,
    )
    if attestation.outcome == "abstain":
        return None
    if attestation.operator_confirmation_required:
        if not review_rejection_confirmed or attestation.operator_gate_reasons != (_REVIEW_REJECTION_GATE,):
            return None
        attestation = attestation.with_operator_confirmation()

    created_at = (now or datetime.now(UTC)).isoformat()
    signal_digest = review_signal.signal_id.removeprefix("sha256:")
    return DispatchOrder(
        order_id=f"reasoned-review-{signal_digest[:20]}",
        session_ref=goal.session_ref,
        nudge=attestation.approved_text,
        message_kind=attestation.message_kind,
        decision_attestation=attestation,
        created_at=created_at,
    )
