"""pr_reviewd — one-shot CLI: fetch a pull request via ``gh``, run chitra's PR security
review gate (``chitra.pr_review``), log the report, and post a findings comment.

Run under an external trigger -- either a GitHub Actions step on a ``pull_request``
event, or an operator/cron invocation -- exactly like ``chitra.rate_limit_guard``'s
one-shot sweep model; this is not a new always-on daemon. A stock workflow ships at
``.github/workflows/pr-security-review.yml``.

Conservative default, an explicit design choice (see this tool's own PR description for
the alternative): this command's exit code is always 0 once the review itself runs
(whether it finds nothing, finds something, or the isolated reviewer process itself is
unavailable), and it posts a plain issue comment -- never a "request changes" review and
never a failing required check. Findings are surfaced but never gate CI or branch
protection by default. ``chitra.policy_config.PRReviewPolicy.block_on_findings`` exists
for an operator who later wants a blocking posture; nothing here flips it automatically.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import structlog

from chitra.policy_config import PRReviewPolicy, load_policy_config
from chitra.pr_review import (
    ChangedFile,
    ClaudeSecurityReviewer,
    PRReviewer,
    PRReviewError,
    PRReviewReport,
    PullRequestDiff,
    append_pr_review,
    pr_review_log_path,
    review_pull_request,
)
from chitra.state_paths import state_dir as default_state_dir

logger = structlog.get_logger(__name__)

GhRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class GhCliError(PRReviewError):
    """Raised when the ``gh`` CLI cannot answer a required pull-request query."""


def _run_gh(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def fetch_pull_request(repo: str, number: int, *, runner: GhRunner = _run_gh) -> PullRequestDiff:
    """Fetch one PR's metadata and diff via the ``gh`` CLI.

    ``gh`` is a trusted local tool invoked exactly like ``chitra.watchd``'s ``tmux``
    calls: its stdout is the source of truth, and a nonzero exit is a hard failure, not
    something this function guesses around.
    """
    meta_result = runner(["gh", "pr", "view", str(number), "--repo", repo, "--json", "title,body,headRefOid,files"])
    if meta_result.returncode != 0:
        raise GhCliError(f"gh pr view failed for {repo}#{number}: {meta_result.stderr.strip()}")
    try:
        meta = json.loads(meta_result.stdout)
    except ValueError as exc:
        raise GhCliError(f"gh pr view returned invalid JSON for {repo}#{number}: {exc}") from exc

    diff_result = runner(["gh", "pr", "diff", str(number), "--repo", repo])
    if diff_result.returncode != 0:
        raise GhCliError(f"gh pr diff failed for {repo}#{number}: {diff_result.stderr.strip()}")

    changed_files = tuple(
        ChangedFile(path=entry["path"], additions=entry.get("additions", 0), deletions=entry.get("deletions", 0))
        for entry in meta.get("files", [])
    )
    return PullRequestDiff.create(
        repo=repo,
        number=number,
        title=meta.get("title") or "",
        body=meta.get("body") or "",
        head_sha=meta["headRefOid"],
        changed_files=changed_files,
        diff_text=diff_result.stdout,
    )


def post_comment(repo: str, number: int, body: str, *, runner: GhRunner = _run_gh) -> None:
    """Post one plain issue comment. Never a review, never a "request changes"."""
    result = runner(["gh", "pr", "comment", str(number), "--repo", repo, "--body", body])
    if result.returncode != 0:
        raise GhCliError(f"gh pr comment failed for {repo}#{number}: {result.stderr.strip()}")


def render_comment(report: PRReviewReport, *, root: Path) -> str:
    """Render one deterministic markdown comment body for a review report.

    Always states plainly that the comment is non-blocking; never implies a merge
    decision was made.
    """
    lines = ["### chitra PR security review (non-blocking report)", ""]
    if report.oversized:
        lines.append("- **Diff size**: exceeds the configured line/file ceiling; consider splitting this PR.")
    if report.blast_radius_hits:
        joined = ", ".join(f"`{path}`" for path in report.blast_radius_hits)
        lines.append(f"- **Blast radius**: touches sensitive paths: {joined}")
    if not report.findings:
        lines.append("- No security findings from this pass.")
    else:
        lines.append(f"- **{len(report.findings)} finding(s)**, highest severity: `{report.highest_severity}`")
        lines.append("")
        for finding in report.findings:
            location = f" ({finding.file_path})" if finding.file_path else ""
            lines.append(f"  - `{finding.severity}` **{finding.code}**{location}: {finding.detail}")
            lines.append(f"    > {finding.citation}")
    lines.append("")
    lines.append(
        "This review never blocks merge or requests changes; it only reports. Full "
        f"report: `{pr_review_log_path(root)}` (report_id `{report.report_id}`)."
    )
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chitra-pr-review",
        description="Fetch one pull request, run chitra's PR security review gate, and report findings.",
    )
    parser.add_argument("--repo", required=True, help="owner/repo, e.g. ReticleWorks/chitra")
    parser.add_argument("--pr", type=int, required=True, dest="number", help="Pull request number.")
    parser.add_argument("--root", type=Path, default=None, help="State root for the review ledger (default: chitra state dir).")
    parser.add_argument("--reviewer-command", default="claude", help="Isolated reviewer command (default: claude).")
    parser.add_argument("--reviewer-model", default=None, help="Pinned isolated reviewer model (default: ambient model).")
    parser.add_argument("--reviewer-count", type=int, default=None, help="Override the configured PRReviewPolicy.reviewer_count.")
    parser.add_argument(
        "--comment",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Post a findings comment via gh pr comment (default: on).",
    )
    parser.add_argument("--json", action="store_true", help="Print the report as JSON to stdout.")
    return parser


def _resolve_policy(args: argparse.Namespace) -> PRReviewPolicy:
    policy = load_policy_config().pr_review
    if args.reviewer_count is not None:
        policy = policy.model_copy(update={"reviewer_count": args.reviewer_count})
    return policy


def run(args: argparse.Namespace, *, gh_runner: GhRunner = _run_gh, reviewer: PRReviewer | None = None) -> int:
    """Execute one review pass; split from ``main`` so tests can inject fakes for both
    the ``gh`` CLI and the isolated reviewer without touching real subprocesses."""
    root = args.root or default_state_dir()
    policy = _resolve_policy(args)

    try:
        diff = fetch_pull_request(args.repo, args.number, runner=gh_runner)
    except GhCliError as exc:
        print(f"chitra-pr-review: {exc}", file=sys.stderr)
        return 1

    active_reviewer = reviewer or ClaudeSecurityReviewer(command=args.reviewer_command, model=args.reviewer_model)
    try:
        report = review_pull_request(diff, reviewer=active_reviewer, policy=policy)
    except PRReviewError as exc:
        logger.warning("pr_review_unavailable", pr_ref=diff.pr_ref, error=str(exc))
        print(f"chitra-pr-review: isolated review unavailable for {diff.pr_ref}: {exc}", file=sys.stderr)
        if args.comment:
            try:
                post_comment(
                    args.repo,
                    args.number,
                    "### chitra PR security review (non-blocking report)\n\n"
                    f"Isolated security review could not complete: {exc}. No findings were assessed; "
                    "this comment does not block merge.",
                    runner=gh_runner,
                )
            except GhCliError as comment_exc:
                print(f"chitra-pr-review: {comment_exc}", file=sys.stderr)
        # A reviewer-availability gap never fails the calling CI job -- that would make
        # this gate a de facto blocking check by accident, which contradicts the
        # conservative default described in the module docstring.
        return 0

    append_pr_review(pr_review_log_path(root), report)
    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print(f"{report.pr_ref}: {len(report.findings)} finding(s), report_id={report.report_id}")

    if args.comment:
        try:
            post_comment(args.repo, args.number, render_comment(report, root=root), runner=gh_runner)
        except GhCliError as exc:
            print(f"chitra-pr-review: {exc}", file=sys.stderr)

    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
