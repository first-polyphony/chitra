"""Fail-closed review of a watched lane's behavior against its frozen goal.

The object under review is always the monitored session's completed turn:
its direction, questions, and completion posture. Chitra's prospective reply
is never placed in these prompts. Each reviewer invocation is a separate
``claude -p`` process with no shared conversation state.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chitra.goals import (
    GoalNotFoundError,
    GoalRecord,
    check_specification,
    get_goal,
    record_review_restart,
    validate_goal,
)

MIN_REVIEWERS = 2
REVIEW_LOG_NAME = "goal_reviews.jsonl"


class GoalReviewError(ValueError):
    """Raised when the isolated review contract cannot be satisfied."""


class ReviewerProcessError(GoalReviewError):
    """Raised when an isolated reviewer process fails or returns bad JSON."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class FrozenGoal(_FrozenModel):
    """Content-addressed strategic goal snapshot for one review round."""

    session_ref: str
    intent: str
    goal: str
    done_when: str
    scope: str
    source: str
    goal_version: int = Field(ge=1)
    contract_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


def freeze_goal(record: GoalRecord) -> FrozenGoal:
    """Strict-validate and content-address one current goal record."""
    issues = [*validate_goal(record), *check_specification(record)]
    if issues:
        raise GoalReviewError("goal is not strict-valid: " + "; ".join(dict.fromkeys(issues)))
    payload = {
        "session_ref": record.session_ref,
        "intent": record.intent,
        "goal": record.goal,
        "done_when": record.done_when,
        "scope": record.scope,
        "source": record.source,
        "goal_version": record.goal_version,
    }
    return FrozenGoal.model_validate({**payload, "contract_id": f"sha256:{_sha256(payload)}"})


class WatchedSessionBehavior(_FrozenModel):
    """The completed lane turn scrutinized by isolated reviewers."""

    session_ref: str = Field(min_length=1)
    turn_text: str = Field(min_length=1)
    behavior_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_turn(cls, session_ref: str, turn_text: str) -> WatchedSessionBehavior:
        text = turn_text.strip()
        if not text:
            raise GoalReviewError("watched-session turn text must be non-empty")
        return cls(session_ref=session_ref, turn_text=text, behavior_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())


FindingCode = Literal["goal_drift", "smuggled_redirect", "hedged_completion", "unsupported_completion", "other"]


class ReviewFinding(_FrozenModel):
    code: FindingCode
    detail: str = Field(min_length=1)
    citation: str = Field(min_length=1)


class ReviewerVerdict(_FrozenModel):
    """Structured result from one isolated process."""

    reviewer_id: str = Field(min_length=1)
    goal_contract_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    behavior_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: Literal["accept", "reject"]
    findings: tuple[ReviewFinding, ...] = ()

    @model_validator(mode="after")
    def validate_findings(self) -> Self:
        if self.verdict == "accept" and self.findings:
            raise ValueError("an accepting reviewer cannot carry adverse findings")
        if self.verdict == "reject" and not self.findings:
            raise ValueError("a rejecting reviewer must cite at least one finding")
        return self


class SessionReviewSignal(_FrozenModel):
    """Unanimity result fed into Chitra's later decision attestation."""

    signal_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    session_ref: str
    goal_contract_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    behavior_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: Literal["accept", "reject"]
    reviewer_ids: tuple[str, ...] = Field(min_length=1)
    findings: tuple[ReviewFinding, ...] = ()
    restarted_after_redirect: bool = False
    recorded_at: str

    @model_validator(mode="after")
    def validate_signal(self) -> Self:
        if len(set(self.reviewer_ids)) != len(self.reviewer_ids):
            raise ValueError("reviewer ids must be unique")
        if self.verdict == "accept" and self.findings:
            raise ValueError("an accepted signal cannot carry findings")
        payload = self.model_dump(mode="json", exclude={"signal_id"})
        if self.signal_id != f"sha256:{_sha256(payload)}":
            raise ValueError("signal_id does not match the review signal")
        return self

    @classmethod
    def create(
        cls,
        *,
        session_ref: str,
        goal_contract_id: str,
        behavior_sha256: str,
        verdict: Literal["accept", "reject"],
        reviewer_ids: Sequence[str],
        findings: Sequence[ReviewFinding] = (),
        restarted_after_redirect: bool = False,
        recorded_at: str | None = None,
    ) -> SessionReviewSignal:
        payload = {
            "session_ref": session_ref,
            "goal_contract_id": goal_contract_id,
            "behavior_sha256": behavior_sha256,
            "verdict": verdict,
            "reviewer_ids": list(reviewer_ids),
            "findings": [finding.model_dump(mode="json") for finding in findings],
            "restarted_after_redirect": restarted_after_redirect,
            "recorded_at": recorded_at or datetime.now(UTC).isoformat(),
        }
        return cls.model_validate({**payload, "signal_id": f"sha256:{_sha256(payload)}"})


class BehaviorReviewer(Protocol):
    """One isolated review invocation."""

    def review(self, goal: FrozenGoal, behavior: WatchedSessionBehavior, reviewer_id: str) -> ReviewerVerdict: ...


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


class ClaudeProcessReviewer:
    """Launch a fresh ``claude -p`` process for every reviewer context."""

    def __init__(
        self,
        *,
        command: str = "claude",
        model: str | None = None,
        timeout_seconds: int = 120,
        runner: ProcessRunner = subprocess.run,
    ) -> None:
        self.command = command
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.runner = runner

    @staticmethod
    def _prompt(goal: FrozenGoal, behavior: WatchedSessionBehavior, reviewer_id: str) -> str:
        request = {
            "reviewer_id": reviewer_id,
            "frozen_goal": goal.model_dump(mode="json"),
            "watched_session_behavior": behavior.model_dump(mode="json"),
        }
        return (
            "You are an isolated adversarial reviewer. Scrutinize only the WATCHED SESSION's completed turn against "
            "its frozen goal. Detect goal drift, a clarifying question that smuggles a strategy redirect, a hedge "
            "presented as completion, or completion without cited proof. Do not review, rewrite, or infer any Chitra "
            "draft response; none is supplied. Return exactly one JSON object with reviewer_id, goal_contract_id, "
            "behavior_sha256, verdict (accept or reject), and findings. Each finding needs code, detail, and an exact "
            "citation from the watched turn. Preserve all supplied identifiers exactly.\nINPUT="
            + _canonical_json(request)
        )

    def review(self, goal: FrozenGoal, behavior: WatchedSessionBehavior, reviewer_id: str) -> ReviewerVerdict:
        command = [self.command, "-p", self._prompt(goal, behavior, reviewer_id), "--output-format", "text"]
        if self.model is not None:
            command.extend(["--model", self.model])
        try:
            completed = self.runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReviewerProcessError(f"isolated reviewer {reviewer_id} could not run: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
            raise ReviewerProcessError(f"isolated reviewer {reviewer_id} failed: {detail}")
        try:
            return ReviewerVerdict.model_validate_json(completed.stdout.strip())
        except ValueError as exc:
            raise ReviewerProcessError(f"isolated reviewer {reviewer_id} returned invalid JSON: {exc}") from exc


def _validate_bound_review(
    review: ReviewerVerdict,
    *,
    reviewer_id: str,
    goal: FrozenGoal,
    behavior: WatchedSessionBehavior,
) -> None:
    if review.reviewer_id != reviewer_id:
        raise GoalReviewError(f"isolated reviewer {reviewer_id} changed its assigned identity")
    if review.goal_contract_id != goal.contract_id:
        raise GoalReviewError(f"isolated reviewer {reviewer_id} returned a stale or tampered goal binding")
    if review.behavior_sha256 != behavior.behavior_sha256:
        raise GoalReviewError(f"isolated reviewer {reviewer_id} returned a stale or tampered behavior binding")


def _signal(
    *,
    goal: FrozenGoal,
    behavior: WatchedSessionBehavior,
    reviews: Sequence[ReviewerVerdict],
    restarted_after_redirect: bool,
) -> SessionReviewSignal:
    findings = tuple(finding for review in reviews for finding in review.findings)
    verdict: Literal["accept", "reject"] = "accept" if all(review.verdict == "accept" for review in reviews) else "reject"
    return SessionReviewSignal.create(
        session_ref=behavior.session_ref,
        goal_contract_id=goal.contract_id,
        behavior_sha256=behavior.behavior_sha256,
        verdict=verdict,
        reviewer_ids=[review.reviewer_id for review in reviews],
        findings=findings,
        restarted_after_redirect=restarted_after_redirect,
    )


def review_log_path(root: Path) -> Path:
    return root / REVIEW_LOG_NAME


def append_review_signal(path: Path, signal: SessionReviewSignal) -> None:
    """Append one internal review signal, deduplicated by content id."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        if json.loads(line).get("signal_id") == signal.signal_id:
                            return
                    except (ValueError, AttributeError):
                        continue
            with path.open("a", encoding="utf-8") as output:
                output.write(signal.model_dump_json() + "\n")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load_latest_review_signal(path: Path, session_ref: str) -> SessionReviewSignal | None:
    """Return the newest valid internal signal for a session."""
    if not path.exists():
        return None
    latest: SessionReviewSignal | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            candidate = SessionReviewSignal.model_validate_json(line)
        except ValueError:
            continue
        if candidate.session_ref == session_ref:
            latest = candidate
    return latest


def review_watched_session(
    root: Path,
    session_ref: str,
    behavior: WatchedSessionBehavior,
    *,
    reviewer: BehaviorReviewer,
    reviewer_count: int = MIN_REVIEWERS,
    max_redirect_restarts: int = 3,
    log_path: Path | None = None,
) -> SessionReviewSignal:
    """Run a unanimous isolated round, restarting on a frozen-goal redirect.

    The initial round requires at least two processes. After any detected
    redirect, the discarded round is logged and the fresh round uses exactly
    one process, per the 4B-mod policy.
    """
    if reviewer_count < MIN_REVIEWERS:
        raise ValueError(f"reviewer_count must be at least {MIN_REVIEWERS}")
    if behavior.session_ref != session_ref:
        raise GoalReviewError("behavior session_ref does not match the reviewed session")
    restarted = False
    restarts = 0
    round_size = reviewer_count
    while True:
        record = get_goal(root, session_ref)
        if record is None:
            raise GoalNotFoundError(session_ref)
        goal = freeze_goal(record)
        reviews: list[ReviewerVerdict] = []
        redirected = False
        for index in range(round_size):
            reviewer_id = f"reviewer-{restarts + 1}-{index + 1}"
            review = reviewer.review(goal, behavior, reviewer_id)
            _validate_bound_review(review, reviewer_id=reviewer_id, goal=goal, behavior=behavior)
            reviews.append(review)
            current_record = get_goal(root, session_ref)
            if current_record is None:
                raise GoalNotFoundError(session_ref)
            current_goal = freeze_goal(current_record)
            if current_goal.contract_id != goal.contract_id:
                record_review_restart(
                    root,
                    session_ref,
                    previous_contract_id=goal.contract_id,
                    restarted_contract_id=current_goal.contract_id,
                    behavior_sha256=behavior.behavior_sha256,
                )
                redirected = True
                restarted = True
                restarts += 1
                round_size = 1
                break
        if redirected:
            if restarts > max_redirect_restarts:
                raise GoalReviewError("goal kept redirecting during review; restart limit exceeded")
            continue
        signal = _signal(goal=goal, behavior=behavior, reviews=reviews, restarted_after_redirect=restarted)
        append_review_signal(log_path or review_log_path(root), signal)
        return signal
