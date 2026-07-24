from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from chitra.goal_enforcement import (
    ClaudeProcessReviewer,
    GoalReviewError,
    ReviewerVerdict,
    ReviewFinding,
    WatchedSessionBehavior,
    freeze_goal,
    review_watched_session,
)
from chitra.goals import GoalRecord, get_goal, redirect_goal, upsert_goal


def _goal(root: Path) -> GoalRecord:
    return upsert_goal(
        root,
        GoalRecord(
            session_ref="localhost:lane:0.0",
            intent="Deliver the requested implementation without redirecting the operator strategy.",
            goal="Build and verify the requested forced completion gate.",
            done_when="Every required local validation passes with cited output.",
            scope="WS1 source tests and documentation only.",
            source="task-file:/tmp/ws1.md",
            status="working",
        ),
    )


class AcceptingReviewer:
    def __init__(self, *, root: Path | None = None, redirect: bool = False) -> None:
        self.calls: list[str] = []
        self.root = root
        self.redirect = redirect

    def review(self, goal, behavior, reviewer_id: str) -> ReviewerVerdict:
        self.calls.append(reviewer_id)
        if self.redirect and len(self.calls) == 1:
            assert self.root is not None
            redirect_goal(
                self.root,
                behavior.session_ref,
                reason="operator corrected the bounded delivery target",
                goal="Build and verify the corrected forced completion gate.",
            )
        return ReviewerVerdict(
            reviewer_id=reviewer_id,
            goal_contract_id=goal.contract_id,
            behavior_sha256=behavior.behavior_sha256,
            verdict="accept",
        )


def test_initial_round_requires_unanimous_isolated_acceptance(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    behavior = WatchedSessionBehavior.from_turn(goal.session_ref, "The gate code is complete and blocks drift with cited proof.")
    reviewer = AcceptingReviewer()

    signal = review_watched_session(tmp_path, goal.session_ref, behavior, reviewer=reviewer)

    assert signal.verdict == "accept"
    assert reviewer.calls == ["reviewer-1-1", "reviewer-1-2"]
    assert signal.reviewer_ids == tuple(reviewer.calls)
    assert (tmp_path / "goal_reviews.jsonl").exists()


def test_frozen_goal_uses_immutable_enrollment_condition_after_redirect(tmp_path: Path) -> None:
    enrolled = _goal(tmp_path)
    redirected = redirect_goal(
        tmp_path,
        enrolled.session_ref,
        reason="operator proposed a smaller validation target",
        done_when="The focused local validation passes with cited output.",
    )

    frozen = freeze_goal(redirected)

    assert frozen.done_when == enrolled.done_when


def test_initial_round_can_be_configured_to_one_reviewer(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    behavior = WatchedSessionBehavior.from_turn(goal.session_ref, "Done with cited completion evidence.")
    reviewer = AcceptingReviewer()

    signal = review_watched_session(tmp_path, goal.session_ref, behavior, reviewer=reviewer, reviewer_count=1)

    assert signal.verdict == "accept"
    assert reviewer.calls == ["reviewer-1-1"]
    assert signal.reviewer_ids == ("reviewer-1-1",)


def test_any_rejection_blocks_unanimous_release(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    behavior = WatchedSessionBehavior.from_turn(goal.session_ref, "Done, but the requested live probe was not run.")

    class MixedReviewer(AcceptingReviewer):
        def review(self, frozen, watched, reviewer_id: str) -> ReviewerVerdict:
            if reviewer_id.endswith("2"):
                return ReviewerVerdict(
                    reviewer_id=reviewer_id,
                    goal_contract_id=frozen.contract_id,
                    behavior_sha256=watched.behavior_sha256,
                    verdict="reject",
                    findings=(
                        ReviewFinding(
                            code="hedged_completion",
                            detail="Completion lacks the required live proof.",
                            citation="the requested live probe was not run",
                        ),
                    ),
                )
            return super().review(frozen, watched, reviewer_id)

    signal = review_watched_session(tmp_path, goal.session_ref, behavior, reviewer=MixedReviewer())
    assert signal.verdict == "reject"
    assert signal.findings[0].code == "hedged_completion"


def test_redirect_restarts_automatically_with_exactly_one_reviewer_and_logs_history(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    behavior = WatchedSessionBehavior.from_turn(goal.session_ref, "The lane asks whether it may change the release strategy.")
    reviewer = AcceptingReviewer(root=tmp_path, redirect=True)

    signal = review_watched_session(tmp_path, goal.session_ref, behavior, reviewer=reviewer)

    assert reviewer.calls == ["reviewer-1-1", "reviewer-2-1"]
    assert signal.restarted_after_redirect is True
    assert signal.reviewer_ids == ("reviewer-2-1",)
    stored = get_goal(tmp_path, goal.session_ref)
    assert stored is not None
    assert stored.goal_history[-1]["event"] == "adversarial-review-redirect-restart"
    assert "one reviewer" in stored.goal_history[-1]["reason"]


def test_tampered_reviewer_binding_fails_closed(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    behavior = WatchedSessionBehavior.from_turn(goal.session_ref, "A normal bounded technical question.")

    class TamperedReviewer:
        def review(self, frozen, watched, reviewer_id: str) -> ReviewerVerdict:
            return ReviewerVerdict(
                reviewer_id=reviewer_id,
                goal_contract_id="sha256:" + "0" * 64,
                behavior_sha256=watched.behavior_sha256,
                verdict="accept",
            )

    with pytest.raises(GoalReviewError, match="tampered goal binding"):
        review_watched_session(tmp_path, goal.session_ref, behavior, reviewer=TamperedReviewer())


def test_claude_reviewer_uses_a_fresh_process_and_only_watched_behavior_context(tmp_path: Path) -> None:
    goal = freeze_goal(_goal(tmp_path))
    behavior = WatchedSessionBehavior.from_turn(goal.session_ref, "Can I redirect this work to an unrelated deploy?")
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        prompt = command[2]
        request = json.loads(prompt.split("<input>\n", 1)[1].rsplit("\n</input>", 1)[0])
        output = ReviewerVerdict(
            reviewer_id=request["reviewer_id"],
            goal_contract_id=request["frozen_goal"]["contract_id"],
            behavior_sha256=request["watched_session_behavior"]["behavior_sha256"],
            verdict="accept",
        ).model_dump_json()
        return subprocess.CompletedProcess(command, 0, output, "")

    reviewer = ClaudeProcessReviewer(runner=runner)
    reviewer.review(goal, behavior, "reviewer-a")
    reviewer.review(goal, behavior, "reviewer-b")

    assert len(commands) == 2
    assert all(command[:2] == ["claude", "-p"] for command in commands)
    assert commands[0] is not commands[1]
    assert all("watched_session_behavior" in command[2] for command in commands)
    assert all("Chitra draft response" in command[2] and "approved_text" not in command[2] for command in commands)
    # The prompt must enumerate the exact FindingCode literals so the reviewer
    # model does not invent an out-of-enum code (e.g. "COMPLETION_WITHOUT_PROOF")
    # that fails ReviewerVerdict validation and forces a fail-closed verdict.
    for code in ("goal_drift", "smuggled_redirect", "hedged_completion", "unsupported_completion", "other"):
        assert all(code in command[2] for command in commands)
