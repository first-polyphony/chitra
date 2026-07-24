from __future__ import annotations

import json
import subprocess
from pathlib import Path

from chitra.pr_review import PRFinding, PRReviewError, PRReviewerVerdict, PullRequestDiff, load_latest_pr_review, pr_review_log_path
from chitra.pr_reviewd import GhCliError, build_arg_parser, fetch_pull_request, post_comment, render_comment, run


def _completed(*, returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeGh:
    """Records every ``gh`` invocation and answers with pre-baked responses in order."""

    def __init__(self, responses: list[subprocess.CompletedProcess[str]]) -> None:
        self._responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        return self._responses[len(self.calls) - 1]


_META = json.dumps(
    {
        "title": "Add PR review gate",
        "body": "Adds a workflow.",
        "headRefOid": "b" * 40,
        "files": [{"path": "src/chitra/pr_review.py", "additions": 40, "deletions": 0}],
    }
)


def test_fetch_pull_request_parses_metadata_and_diff() -> None:
    gh = _FakeGh([_completed(stdout=_META), _completed(stdout="diff --git a/x b/x\n+1\n")])

    diff = fetch_pull_request("ReticleWorks/chitra", 7, runner=gh)

    assert diff.title == "Add PR review gate"
    assert diff.head_sha == "b" * 40
    assert diff.changed_files[0].path == "src/chitra/pr_review.py"
    assert gh.calls[0][:3] == ["gh", "pr", "view"]
    assert gh.calls[1][:3] == ["gh", "pr", "diff"]


def test_fetch_pull_request_raises_on_gh_view_failure() -> None:
    gh = _FakeGh([_completed(returncode=1, stderr="no such pr")])
    try:
        fetch_pull_request("ReticleWorks/chitra", 7, runner=gh)
        raise AssertionError("expected GhCliError")
    except GhCliError as exc:
        assert "no such pr" in str(exc)


def test_fetch_pull_request_raises_on_gh_diff_failure() -> None:
    gh = _FakeGh([_completed(stdout=_META), _completed(returncode=1, stderr="boom")])
    try:
        fetch_pull_request("ReticleWorks/chitra", 7, runner=gh)
        raise AssertionError("expected GhCliError")
    except GhCliError as exc:
        assert "boom" in str(exc)


def test_post_comment_raises_on_failure() -> None:
    gh = _FakeGh([_completed(returncode=1, stderr="rate limited")])
    try:
        post_comment("ReticleWorks/chitra", 7, "hello", runner=gh)
        raise AssertionError("expected GhCliError")
    except GhCliError as exc:
        assert "rate limited" in str(exc)


def test_render_comment_reports_no_findings_and_states_non_blocking(tmp_path: Path) -> None:
    diff = PullRequestDiff.create(
        repo="ReticleWorks/chitra",
        number=7,
        title="t",
        body="b",
        head_sha="c" * 40,
        changed_files=(),
        diff_text="diff",
    )
    from chitra.pr_review import PRReviewReport

    report = PRReviewReport.create(pr_ref=diff.pr_ref, diff_sha256=diff.diff_sha256, reviewer_ids=["pr-reviewer-1"])
    body = render_comment(report, root=tmp_path)
    assert "No security findings" in body
    assert "never blocks merge" in body


def test_render_comment_reports_findings_and_blast_radius(tmp_path: Path) -> None:
    diff = PullRequestDiff.create(
        repo="ReticleWorks/chitra",
        number=7,
        title="t",
        body="b",
        head_sha="c" * 40,
        changed_files=(),
        diff_text="diff",
    )
    from chitra.pr_review import PRReviewReport

    finding = PRFinding(code="hardcoded_secret", severity="critical", detail="leaked", citation="+API_KEY='x'", file_path="a.py")
    report = PRReviewReport.create(
        pr_ref=diff.pr_ref,
        diff_sha256=diff.diff_sha256,
        reviewer_ids=["pr-reviewer-1"],
        findings=(finding,),
        blast_radius_hits=("src/chitra/auth.py",),
        oversized=True,
    )
    body = render_comment(report, root=tmp_path)
    assert "1 finding(s)" in body
    assert "critical" in body
    assert "src/chitra/auth.py" in body
    assert "exceeds the configured line/file ceiling" in body


class _CleanReviewer:
    def review(self, diff: PullRequestDiff, reviewer_id: str) -> PRReviewerVerdict:
        return PRReviewerVerdict(reviewer_id=reviewer_id, diff_sha256=diff.diff_sha256, verdict="clean")


class _UnavailableReviewer:
    def review(self, diff: PullRequestDiff, reviewer_id: str) -> PRReviewerVerdict:
        raise PRReviewError("isolated reviewer process could not run")


def test_run_end_to_end_logs_and_comments_clean_report(tmp_path: Path) -> None:
    gh = _FakeGh([_completed(stdout=_META), _completed(stdout="diff"), _completed()])
    args = build_arg_parser().parse_args(
        ["--repo", "ReticleWorks/chitra", "--pr", "7", "--root", str(tmp_path), "--reviewer-count", "1"]
    )

    exit_code = run(args, gh_runner=gh, reviewer=_CleanReviewer())

    assert exit_code == 0
    assert gh.calls[-1][:3] == ["gh", "pr", "comment"]
    loaded = load_latest_pr_review(pr_review_log_path(tmp_path), "ReticleWorks/chitra#7")
    assert loaded is not None
    assert loaded.findings == ()


def test_run_skips_comment_when_disabled(tmp_path: Path) -> None:
    gh = _FakeGh([_completed(stdout=_META), _completed(stdout="diff")])
    args = build_arg_parser().parse_args(
        ["--repo", "ReticleWorks/chitra", "--pr", "7", "--root", str(tmp_path), "--reviewer-count", "1", "--no-comment"]
    )

    exit_code = run(args, gh_runner=gh, reviewer=_CleanReviewer())

    assert exit_code == 0
    assert len(gh.calls) == 2  # view + diff only, no comment call


def test_run_never_fails_when_reviewer_is_unavailable(tmp_path: Path) -> None:
    gh = _FakeGh([_completed(stdout=_META), _completed(stdout="diff"), _completed()])
    args = build_arg_parser().parse_args(
        ["--repo", "ReticleWorks/chitra", "--pr", "7", "--root", str(tmp_path), "--reviewer-count", "1"]
    )

    exit_code = run(args, gh_runner=gh, reviewer=_UnavailableReviewer())

    assert exit_code == 0
    assert gh.calls[-1][:3] == ["gh", "pr", "comment"]
    assert load_latest_pr_review(pr_review_log_path(tmp_path), "ReticleWorks/chitra#7") is None


def test_run_returns_error_when_gh_view_fails(tmp_path: Path) -> None:
    gh = _FakeGh([_completed(returncode=1, stderr="no access")])
    args = build_arg_parser().parse_args(["--repo", "ReticleWorks/chitra", "--pr", "7", "--root", str(tmp_path)])

    exit_code = run(args, gh_runner=gh, reviewer=_CleanReviewer())

    assert exit_code == 1
