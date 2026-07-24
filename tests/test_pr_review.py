from __future__ import annotations

from pathlib import Path

import pytest

from chitra.policy_config import PRReviewPolicy
from chitra.pr_review import (
    ChangedFile,
    PRFinding,
    PRReviewError,
    PRReviewerVerdict,
    PRReviewReport,
    PullRequestDiff,
    append_pr_review,
    blast_radius_hits,
    diff_is_oversized,
    load_latest_pr_review,
    pr_review_log_path,
    review_pull_request,
)


def _diff(*, changed_files: tuple[ChangedFile, ...] | None = None, diff_text: str = "diff --git a/x b/x\n+print(1)\n") -> PullRequestDiff:
    return PullRequestDiff.create(
        repo="ReticleWorks/chitra",
        number=42,
        title="Add a thing",
        body="Does a thing.",
        head_sha="a" * 40,
        changed_files=changed_files or (ChangedFile(path="src/chitra/dispatch.py", additions=10, deletions=2),),
        diff_text=diff_text,
    )


class _FakeReviewer:
    def __init__(self, verdicts: list[PRReviewerVerdict]) -> None:
        self._verdicts = verdicts
        self.calls: list[str] = []

    def review(self, diff: PullRequestDiff, reviewer_id: str) -> PRReviewerVerdict:
        self.calls.append(reviewer_id)
        return self._verdicts[len(self.calls) - 1]


def test_blast_radius_hits_matches_case_insensitively_and_dedupes() -> None:
    files = (
        ChangedFile(path="src/chitra/auth/token.py", additions=1, deletions=0),
        ChangedFile(path="src/chitra/AUTH/other.py", additions=1, deletions=0),
        ChangedFile(path="src/chitra/board.py", additions=1, deletions=0),
    )
    hits = blast_radius_hits(files, ["auth", "secret"])
    assert hits == ("src/chitra/AUTH/other.py", "src/chitra/auth/token.py")


def test_blast_radius_hits_ignores_blank_keywords() -> None:
    files = (ChangedFile(path="src/chitra/board.py", additions=1, deletions=0),)
    assert blast_radius_hits(files, ["", "   "]) == ()


def test_diff_is_oversized_on_lines_or_files() -> None:
    small = _diff()
    assert not diff_is_oversized(small, max_lines=500, max_files=20)
    assert diff_is_oversized(small, max_lines=5, max_files=20)

    many_files = tuple(ChangedFile(path=f"f{i}.py", additions=1, deletions=0) for i in range(25))
    big = _diff(changed_files=many_files)
    assert diff_is_oversized(big, max_lines=500, max_files=20)


def test_pr_reviewer_verdict_rejects_findings_without_verdict_match() -> None:
    with pytest.raises(ValueError):
        PRReviewerVerdict(reviewer_id="r1", diff_sha256="a" * 64, verdict="clean", findings=(
            PRFinding(code="other", severity="low", detail="x", citation="y"),
        ))
    with pytest.raises(ValueError):
        PRReviewerVerdict(reviewer_id="r1", diff_sha256="a" * 64, verdict="findings", findings=())


def test_review_pull_request_unions_and_dedupes_findings_across_reviewers() -> None:
    diff = _diff()
    shared = PRFinding(code="hardcoded_secret", severity="high", detail="leaked key", citation="+API_KEY = 'x'")
    unique = PRFinding(code="sql_injection", severity="critical", detail="raw query", citation="+cur.execute(q)")
    reviewer = _FakeReviewer(
        [
            PRReviewerVerdict(reviewer_id="pr-reviewer-1", diff_sha256=diff.diff_sha256, verdict="findings", findings=(shared,)),
            PRReviewerVerdict(
                reviewer_id="pr-reviewer-2", diff_sha256=diff.diff_sha256, verdict="findings", findings=(shared, unique)
            ),
        ]
    )

    report = review_pull_request(diff, reviewer=reviewer, policy=PRReviewPolicy(reviewer_count=2))

    assert reviewer.calls == ["pr-reviewer-1", "pr-reviewer-2"]
    assert len(report.findings) == 2
    assert report.highest_severity == "critical"
    assert report.pr_ref == "ReticleWorks/chitra#42"


def test_review_pull_request_never_blocks_by_default() -> None:
    diff = _diff()
    finding = PRFinding(code="other", severity="critical", detail="bad", citation="+x")
    reviewer = _FakeReviewer(
        [PRReviewerVerdict(reviewer_id="pr-reviewer-1", diff_sha256=diff.diff_sha256, verdict="findings", findings=(finding,))]
    )

    report = review_pull_request(diff, reviewer=reviewer, policy=PRReviewPolicy(reviewer_count=1))

    assert report.blocked is False


def test_review_pull_request_blocks_only_when_policy_opts_in() -> None:
    diff = _diff()
    finding = PRFinding(code="other", severity="critical", detail="bad", citation="+x")
    reviewer = _FakeReviewer(
        [PRReviewerVerdict(reviewer_id="pr-reviewer-1", diff_sha256=diff.diff_sha256, verdict="findings", findings=(finding,))]
    )

    report = review_pull_request(diff, reviewer=reviewer, policy=PRReviewPolicy(reviewer_count=1, block_on_findings=True))

    assert report.blocked is True


def test_review_pull_request_reports_clean_verdict_with_no_findings() -> None:
    diff = _diff()
    reviewer = _FakeReviewer([PRReviewerVerdict(reviewer_id="pr-reviewer-1", diff_sha256=diff.diff_sha256, verdict="clean")])

    report = review_pull_request(diff, reviewer=reviewer, policy=PRReviewPolicy(reviewer_count=1))

    assert report.findings == ()
    assert report.blocked is False


def test_review_pull_request_rejects_a_reviewer_returning_a_stale_diff_binding() -> None:
    diff = _diff()

    class StaleReviewer:
        def review(self, diff: PullRequestDiff, reviewer_id: str) -> PRReviewerVerdict:
            return PRReviewerVerdict(reviewer_id=reviewer_id, diff_sha256="f" * 64, verdict="clean")

    with pytest.raises(PRReviewError):
        review_pull_request(diff, reviewer=StaleReviewer(), policy=PRReviewPolicy(reviewer_count=1))


def test_review_pull_request_rejects_a_reviewer_that_changes_its_identity() -> None:
    diff = _diff()

    class ImpersonatingReviewer:
        def review(self, diff: PullRequestDiff, reviewer_id: str) -> PRReviewerVerdict:
            return PRReviewerVerdict(reviewer_id="someone-else", diff_sha256=diff.diff_sha256, verdict="clean")

    with pytest.raises(PRReviewError):
        review_pull_request(diff, reviewer=ImpersonatingReviewer(), policy=PRReviewPolicy(reviewer_count=1))


def test_append_and_load_pr_review_round_trips_and_dedupes(tmp_path: Path) -> None:
    diff = _diff()
    report = PRReviewReport.create(pr_ref=diff.pr_ref, diff_sha256=diff.diff_sha256, reviewer_ids=["pr-reviewer-1"])
    log_path = pr_review_log_path(tmp_path)

    append_pr_review(log_path, report)
    append_pr_review(log_path, report)  # idempotent: identical report_id is not duplicated

    assert log_path.read_text(encoding="utf-8").count(report.report_id) == 1
    loaded = load_latest_pr_review(log_path, diff.pr_ref)
    assert loaded is not None
    assert loaded.report_id == report.report_id


def test_load_latest_pr_review_returns_none_when_absent(tmp_path: Path) -> None:
    assert load_latest_pr_review(pr_review_log_path(tmp_path), "ReticleWorks/chitra#1") is None


def test_pr_review_policy_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError):
        PRReviewPolicy(max_diff_lines=0)
    with pytest.raises(ValueError):
        PRReviewPolicy(max_diff_files=0)
    with pytest.raises(ValueError):
        PRReviewPolicy(reviewer_count=0)
    with pytest.raises(ValueError):
        PRReviewPolicy(blast_radius_keywords=["auth", "  "])
