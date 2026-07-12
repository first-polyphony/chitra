"""Tests for the deterministic adversarial goal-enforcement contracts."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError

from chitra.goal_enforcement import (
    AdversarialReview,
    EvidenceItem,
    GoalContractError,
    ReviewFinding,
    build_candidate,
    freeze_goal_contract,
    gate_candidate,
)
from chitra.goals import GoalRecord


def _goal() -> GoalRecord:
    return GoalRecord(
        session_ref="host:lane:0.0",
        goal="Ship the deterministic parser repair without widening scope.",
        done_when="The parser tests and static checks pass cleanly.",
        intent="Restore correct parser behavior while preserving every existing public contract.",
        scope="Parser implementation and focused tests only.",
        source="task-file:/tmp/parser-repair.md",
        status="working",
        now="editing parser code",
    )


def _candidate(goal: GoalRecord | None = None):
    contract = freeze_goal_contract(_goal() if goal is None else goal)
    return build_candidate(
        contract,
        kind="answer",
        request="Should I replace the parser framework?",
        content="No. Repair the existing parser and keep the public contract unchanged.",
        author_id="implementer-0",
        evidence=(EvidenceItem(source="failing test", text="test_parser_contract fails on nested input"),),
    )


def _accept(candidate, reviewer_id: str) -> AdversarialReview:
    return AdversarialReview(
        reviewer_id=reviewer_id,
        contract_id=candidate.contract_id,
        candidate_id=candidate.candidate_id,
        disposition="accept",
    )


def _reject(candidate, reviewer_id: str, detail: str = "The proposal widens the recorded scope.") -> AdversarialReview:
    return AdversarialReview(
        reviewer_id=reviewer_id,
        contract_id=candidate.contract_id,
        candidate_id=candidate.candidate_id,
        disposition="reject",
        findings=(ReviewFinding(code="scope_violation", detail=detail, basis="scope permits parser changes only"),),
    )


def test_goal_contract_is_stable_across_tactical_updates_but_changes_on_redirect() -> None:
    goal = _goal()
    original = freeze_goal_contract(goal)

    assert original.model_dump()["schema"] == "chitra.goal-contract.v1"
    assert freeze_goal_contract(replace(goal, now="running tests", status="blocked")).contract_id == original.contract_id

    redirected = replace(
        goal,
        goal_version=2,
        scope="Parser implementation, focused tests, and compatibility documentation.",
    )
    assert freeze_goal_contract(redirected).contract_id != original.contract_id


def test_goal_contract_requires_a_complete_strategic_specification() -> None:
    with pytest.raises(GoalContractError, match="scope must be"):
        freeze_goal_contract(replace(_goal(), scope=""))


def test_review_contract_requires_findings_exactly_for_rejection() -> None:
    candidate = _candidate()
    with pytest.raises(ValidationError, match="accepted review must not contain findings"):
        AdversarialReview(
            reviewer_id="reviewer-1",
            contract_id=candidate.contract_id,
            candidate_id=candidate.candidate_id,
            disposition="accept",
            findings=(ReviewFinding(code="goal_drift", detail="drift", basis="goal says repair"),),
        )
    with pytest.raises(ValidationError, match="rejected review must contain"):
        AdversarialReview(
            reviewer_id="reviewer-1",
            contract_id=candidate.contract_id,
            candidate_id=candidate.candidate_id,
            disposition="reject",
        )


def test_gate_releases_only_two_or_more_unanimous_bound_reviewers() -> None:
    goal = _goal()
    candidate = _candidate(goal)

    one = gate_candidate(candidate, (_accept(candidate, "reviewer-1"),), current_goal=goal)
    unanimous = gate_candidate(
        candidate,
        (_accept(candidate, "reviewer-1"), _accept(candidate, "reviewer-2")),
        current_goal=goal,
    )

    assert (one.release, one.reason) == (False, "insufficient_reviews")
    assert (unanimous.release, unanimous.reason) == (True, "accepted")
    with pytest.raises(ValueError, match="at least 2"):
        gate_candidate(candidate, (), current_goal=goal, required_reviewers=1)


def test_any_adversarial_finding_blocks_and_becomes_a_stable_work_queue() -> None:
    goal = _goal()
    candidate = _candidate(goal)
    rejected = gate_candidate(
        candidate,
        (_reject(candidate, "reviewer-2"), _accept(candidate, "reviewer-1")),
        current_goal=goal,
    )

    assert (rejected.release, rejected.reason) == (False, "rejected")
    assert [item.reviewer_id for item in rejected.feedback] == ["reviewer-2"]
    assert rejected.feedback[0].code == "scope_violation"


@pytest.mark.parametrize("reviewer_ids", [("reviewer-1", "reviewer-1"), ("implementer-0", "reviewer-2")])
def test_duplicate_or_self_reviewers_fail_closed(reviewer_ids: tuple[str, str]) -> None:
    goal = _goal()
    candidate = _candidate(goal)
    verdict = gate_candidate(
        candidate,
        tuple(_accept(candidate, reviewer_id) for reviewer_id in reviewer_ids),
        current_goal=goal,
    )
    assert (verdict.release, verdict.reason) == (False, "reviewer_conflict")


def test_stale_reviews_tampered_candidates_and_redirects_fail_closed() -> None:
    goal = _goal()
    candidate = _candidate(goal)
    reviews = (_accept(candidate, "reviewer-1"), _accept(candidate, "reviewer-2"))

    stale_review = reviews[0].model_copy(update={"candidate_id": "0" * 64})
    stale = gate_candidate(candidate, (stale_review, reviews[1]), current_goal=goal)
    tampered = gate_candidate(candidate.model_copy(update={"content": "Replace everything."}), reviews, current_goal=goal)
    redirected = gate_candidate(candidate, reviews, current_goal=replace(goal, goal_version=2))

    assert (stale.release, stale.reason) == (False, "review_binding_mismatch")
    assert (tampered.release, tampered.reason) == (False, "candidate_invalid")
    assert (redirected.release, redirected.reason) == (False, "goal_changed")
