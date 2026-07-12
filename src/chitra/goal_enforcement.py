"""Deterministic contracts for goal-bound adversarial review.

This module does not draft, reason, or call a model.  It freezes a strategic
goal version, binds a proposed answer or action to that version, and applies a
fail-closed quorum rule to caller-supplied adversarial reviews.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from chitra.goals import GoalRecord, check_specification

MIN_ADVERSARIAL_REVIEWERS = 2
GOAL_CONTRACT_SCHEMA = "chitra.goal-contract.v1"
CANDIDATE_SCHEMA = "chitra.goal-candidate.v1"
REVIEW_SCHEMA = "chitra.goal-review.v1"
VERDICT_SCHEMA = "chitra.goal-verdict.v1"

CandidateKind = Literal["answer", "nudge", "action"]
FindingCode = Literal[
    "goal_drift",
    "scope_violation",
    "done_when_conflict",
    "principle_violation",
    "unsupported_claim",
    "request_mismatch",
]
ReviewDisposition = Literal["accept", "reject"]
VerdictReason = Literal[
    "accepted",
    "candidate_invalid",
    "goal_changed",
    "insufficient_reviews",
    "reviewer_conflict",
    "review_binding_mismatch",
    "rejected",
]

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class GoalContractError(ValueError):
    """Raised when a goal is too weak to freeze as an enforcement contract."""


class _StrictModel(BaseModel):
    """Shared immutable boundary model."""

    model_config = ConfigDict(extra="forbid", frozen=True, serialize_by_alias=True)


def _require_text(value: str) -> str:
    if not value.strip():
        raise ValueError("must be non-empty")
    return value


def _require_sha256(value: str) -> str:
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError("must be a lowercase SHA-256 digest")
    return value


def _digest(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class GoalContract(_StrictModel):
    """One redirect-gated strategic goal version frozen for a review round.

    ``goal`` is "original" relative to the candidate under review: neither a
    lane question, tactical ``now`` update, nor draft can replace it.  A valid
    operator redirect creates a new version and therefore a new contract id.
    """

    schema_version: Literal["chitra.goal-contract.v1"] = Field(default="chitra.goal-contract.v1", alias="schema")
    contract_id: StrictStr
    session_ref: StrictStr
    goal_version: StrictInt = Field(ge=1)
    goal: StrictStr
    done_when: StrictStr
    intent: StrictStr
    scope: StrictStr
    source: StrictStr

    _validate_contract_id = field_validator("contract_id")(_require_sha256)
    _validate_text = field_validator("session_ref", "goal", "done_when", "intent", "scope", "source")(_require_text)


class EvidenceItem(_StrictModel):
    """Caller-supplied grounding visible to both writer and reviewers."""

    source: StrictStr
    text: StrictStr

    _validate_text = field_validator("source", "text")(_require_text)


class GoalCandidate(_StrictModel):
    """Exact answer or action text bound to a goal contract and request."""

    schema_version: Literal["chitra.goal-candidate.v1"] = Field(default="chitra.goal-candidate.v1", alias="schema")
    candidate_id: StrictStr
    contract_id: StrictStr
    kind: CandidateKind
    request: StrictStr
    content: StrictStr
    author_id: StrictStr
    evidence: tuple[EvidenceItem, ...] = ()

    _validate_ids = field_validator("candidate_id", "contract_id")(_require_sha256)
    _validate_text = field_validator("request", "content", "author_id")(_require_text)


class ReviewFinding(_StrictModel):
    """One concrete reason a candidate must not be released."""

    code: FindingCode
    detail: StrictStr
    basis: StrictStr

    _validate_text = field_validator("detail", "basis")(_require_text)


class AdversarialReview(_StrictModel):
    """Structured output from one isolated adversarial reviewer."""

    schema_version: Literal["chitra.goal-review.v1"] = Field(default="chitra.goal-review.v1", alias="schema")
    reviewer_id: StrictStr
    contract_id: StrictStr
    candidate_id: StrictStr
    disposition: ReviewDisposition
    findings: tuple[ReviewFinding, ...] = ()

    _validate_ids = field_validator("contract_id", "candidate_id")(_require_sha256)
    _validate_reviewer = field_validator("reviewer_id")(_require_text)

    @model_validator(mode="after")
    def validate_findings_match_disposition(self) -> AdversarialReview:
        """An acceptance has no findings; a rejection must explain itself."""
        if self.disposition == "accept" and self.findings:
            raise ValueError("an accepted review must not contain findings")
        if self.disposition == "reject" and not self.findings:
            raise ValueError("a rejected review must contain at least one finding")
        return self


class FeedbackItem(_StrictModel):
    """A reviewer finding normalized into the implementer's work queue."""

    reviewer_id: StrictStr
    code: FindingCode
    detail: StrictStr
    basis: StrictStr

    _validate_text = field_validator("reviewer_id", "detail", "basis")(_require_text)


class EnforcementVerdict(_StrictModel):
    """The deterministic release decision for one exact candidate."""

    schema_version: Literal["chitra.goal-verdict.v1"] = Field(default="chitra.goal-verdict.v1", alias="schema")
    release: StrictBool
    reason: VerdictReason
    contract_id: StrictStr
    candidate_id: StrictStr
    required_reviewers: StrictInt = Field(ge=MIN_ADVERSARIAL_REVIEWERS)
    reviewer_ids: tuple[StrictStr, ...]
    feedback: tuple[FeedbackItem, ...] = ()

    _validate_ids = field_validator("contract_id", "candidate_id")(_require_sha256)

    @model_validator(mode="after")
    def validate_release_reason(self) -> EnforcementVerdict:
        """Only the unanimous accepted state can authorize release."""
        if self.release != (self.reason == "accepted"):
            raise ValueError("release is permitted only for an accepted verdict")
        if self.reason == "rejected" and not self.feedback:
            raise ValueError("a rejected verdict must carry reviewer feedback")
        return self


def freeze_goal_contract(record: GoalRecord) -> GoalContract:
    """Freeze a fully specified strategic goal version into a stable contract."""
    issues = check_specification(record)
    if record.goal_version < 1:
        issues.append("goal_version must be at least one")
    if issues:
        raise GoalContractError("; ".join(issues))
    payload: dict[str, object] = {
        "schema": GOAL_CONTRACT_SCHEMA,
        "session_ref": record.session_ref,
        "goal_version": record.goal_version,
        "goal": record.goal,
        "done_when": record.done_when,
        "intent": record.intent,
        "scope": record.scope,
        "source": record.source,
    }
    return GoalContract(
        contract_id=_digest(payload),
        session_ref=record.session_ref,
        goal_version=record.goal_version,
        goal=record.goal,
        done_when=record.done_when,
        intent=record.intent,
        scope=record.scope,
        source=record.source,
    )


def build_candidate(
    contract: GoalContract,
    *,
    kind: CandidateKind,
    request: str,
    content: str,
    author_id: str,
    evidence: tuple[EvidenceItem, ...] = (),
) -> GoalCandidate:
    """Bind exact candidate text and grounding to one frozen goal version."""
    payload: dict[str, object] = {
        "schema": CANDIDATE_SCHEMA,
        "contract_id": contract.contract_id,
        "kind": kind,
        "request": request,
        "content": content,
        "author_id": author_id,
        "evidence": [item.model_dump(mode="json") for item in evidence],
    }
    return GoalCandidate(
        candidate_id=_digest(payload),
        contract_id=contract.contract_id,
        kind=kind,
        request=request,
        content=content,
        author_id=author_id,
        evidence=evidence,
    )


def _candidate_digest(candidate: GoalCandidate) -> str:
    payload: dict[str, object] = {
        "schema": candidate.schema_version,
        "contract_id": candidate.contract_id,
        "kind": candidate.kind,
        "request": candidate.request,
        "content": candidate.content,
        "author_id": candidate.author_id,
        "evidence": [item.model_dump(mode="json") for item in candidate.evidence],
    }
    return _digest(payload)


def _feedback(reviews: tuple[AdversarialReview, ...]) -> tuple[FeedbackItem, ...]:
    unique = {(review.reviewer_id, finding.code, finding.detail, finding.basis) for review in reviews for finding in review.findings}
    return tuple(
        FeedbackItem(reviewer_id=reviewer_id, code=code, detail=detail, basis=basis) for reviewer_id, code, detail, basis in sorted(unique)
    )


def _verdict(
    candidate: GoalCandidate,
    *,
    release: bool,
    reason: VerdictReason,
    required_reviewers: int,
    reviews: tuple[AdversarialReview, ...],
) -> EnforcementVerdict:
    return EnforcementVerdict(
        release=release,
        reason=reason,
        contract_id=candidate.contract_id,
        candidate_id=candidate.candidate_id,
        required_reviewers=required_reviewers,
        reviewer_ids=tuple(review.reviewer_id for review in reviews),
        feedback=_feedback(reviews) if reason == "rejected" else (),
    )


def gate_candidate(
    candidate: GoalCandidate,
    reviews: tuple[AdversarialReview, ...],
    *,
    current_goal: GoalRecord,
    required_reviewers: int = MIN_ADVERSARIAL_REVIEWERS,
) -> EnforcementVerdict:
    """Release only an unchanged candidate unanimously accepted by N reviewers.

    The caller must supply a freshly loaded ``current_goal`` at the last
    responsible moment.  Any candidate mutation, strategic redirect, missing
    or duplicate reviewer, stale review, self-review, or rejection blocks.
    """
    if required_reviewers < MIN_ADVERSARIAL_REVIEWERS:
        raise ValueError(f"required_reviewers must be at least {MIN_ADVERSARIAL_REVIEWERS}")
    if candidate.candidate_id != _candidate_digest(candidate):
        return _verdict(
            candidate,
            release=False,
            reason="candidate_invalid",
            required_reviewers=required_reviewers,
            reviews=reviews,
        )
    if freeze_goal_contract(current_goal).contract_id != candidate.contract_id:
        return _verdict(
            candidate,
            release=False,
            reason="goal_changed",
            required_reviewers=required_reviewers,
            reviews=reviews,
        )
    if len(reviews) < required_reviewers:
        return _verdict(
            candidate,
            release=False,
            reason="insufficient_reviews",
            required_reviewers=required_reviewers,
            reviews=reviews,
        )
    reviewer_ids = [review.reviewer_id for review in reviews]
    if len(set(reviewer_ids)) != len(reviewer_ids) or candidate.author_id in reviewer_ids:
        return _verdict(
            candidate,
            release=False,
            reason="reviewer_conflict",
            required_reviewers=required_reviewers,
            reviews=reviews,
        )
    if any(review.contract_id != candidate.contract_id or review.candidate_id != candidate.candidate_id for review in reviews):
        return _verdict(
            candidate,
            release=False,
            reason="review_binding_mismatch",
            required_reviewers=required_reviewers,
            reviews=reviews,
        )
    if any(review.disposition == "reject" for review in reviews):
        return _verdict(
            candidate,
            release=False,
            reason="rejected",
            required_reviewers=required_reviewers,
            reviews=reviews,
        )
    return _verdict(
        candidate,
        release=True,
        reason="accepted",
        required_reviewers=required_reviewers,
        reviews=reviews,
    )
