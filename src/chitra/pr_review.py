"""pr_review — deterministic pre-checks plus isolated LLM security review for one PR diff.

Modeled on PostHog's "Stop being the code review bottleneck" workflow (Jina Yoon,
newsletter.posthog.com/p/code-review-tips): several independent reviewer passes over the
same diff (agents are bad at checking their own work, so this module never lets the
authoring agent review itself), a cheap deterministic gate ahead of any LLM call
(StampHog's blast-radius deny-list and diff-size ceiling), and a findings report rather
than an autonomous fix — chitra's own "review-triage" role here stays a report, not a
merge decision.

This mirrors chitra's existing goal-enforcement gate shape (see
``chitra.goal_enforcement`` / ``chitra.completion_gate``): a ``Protocol``-typed isolated
reviewer, a frozen pydantic verdict that a reviewer cannot self-contradict, and a signed,
deduplicated JSONL ledger under the caller's state root. The one deliberate difference:
goal enforcement requires unanimous acceptance to clear a lane, but this gate never
"clears" anything — it unions findings from every reviewer and reports them. There is no
merge-blocking default; see ``PRReviewPolicy.block_on_findings`` and the module's own
``pr_reviewd`` CLI docstring for why that choice was left to the operator.

No LLM calls happen anywhere in this module except inside ``ClaudeSecurityReviewer.review``,
which is a single isolated subprocess call with no shared conversation state — exactly like
``chitra.goal_enforcement.ClaudeProcessReviewer``.
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

from chitra.policy_config import PRReviewPolicy

REVIEW_LOG_NAME = "pr_reviews.jsonl"


class PRReviewError(ValueError):
    """Raised when the PR review contract cannot be satisfied."""


class ReviewerProcessError(PRReviewError):
    """Raised when an isolated reviewer process fails or returns bad JSON."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class ChangedFile(_FrozenModel):
    """One file touched by a pull request, as reported by the PR host."""

    path: str = Field(min_length=1)
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)


class PullRequestDiff(_FrozenModel):
    """One content-addressed snapshot of a pull request's diff, scrutinized in isolation.

    Chitra's prospective comment is never part of this object; only the PR author's own
    title, body, and diff are ever shown to a reviewer.
    """

    repo: str = Field(min_length=1)
    number: int = Field(ge=1)
    title: str
    body: str
    head_sha: str = Field(pattern=r"^[0-9a-f]{7,40}$")
    changed_files: tuple[ChangedFile, ...]
    diff_text: str
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def pr_ref(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def total_additions(self) -> int:
        return sum(changed.additions for changed in self.changed_files)

    @property
    def total_deletions(self) -> int:
        return sum(changed.deletions for changed in self.changed_files)

    @classmethod
    def create(
        cls,
        *,
        repo: str,
        number: int,
        title: str,
        body: str,
        head_sha: str,
        changed_files: Sequence[ChangedFile],
        diff_text: str,
    ) -> PullRequestDiff:
        """Build one diff snapshot, computing its content address from the diff text."""
        return cls(
            repo=repo,
            number=number,
            title=title,
            body=body,
            head_sha=head_sha,
            changed_files=tuple(changed_files),
            diff_text=diff_text,
            diff_sha256=hashlib.sha256(diff_text.encode("utf-8", errors="replace")).hexdigest(),
        )


def blast_radius_hits(changed_files: Sequence[ChangedFile], keywords: Sequence[str]) -> tuple[str, ...]:
    """Return changed paths whose path text matches a deny-list keyword, deduped and sorted.

    Mirrors StampHog's blast-radius deny-list (PostHog's code-review workflow): a cheap,
    fully deterministic signal computed over path text only -- never diff content -- so
    it runs before, and independently of, any LLM call.
    """
    lowered_keywords = tuple(keyword.lower() for keyword in keywords if keyword.strip())
    hits = {changed.path for changed in changed_files if any(keyword in changed.path.lower() for keyword in lowered_keywords)}
    return tuple(sorted(hits))


def diff_is_oversized(diff: PullRequestDiff, *, max_lines: int, max_files: int) -> bool:
    """Flag a diff that exceeds either the configured line-count or file-count ceiling."""
    total_lines = diff.total_additions + diff.total_deletions
    return total_lines > max_lines or len(diff.changed_files) > max_files


FindingCode = Literal[
    "hardcoded_secret",
    "sql_injection",
    "command_injection",
    "path_traversal",
    "ssrf",
    "auth_bypass",
    "insecure_deserialization",
    "dependency_risk",
    "prompt_injection",
    "other",
]
Severity = Literal["low", "medium", "high", "critical"]
_SEVERITY_ORDER: dict[Severity, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class PRFinding(_FrozenModel):
    """One adverse, independently citable security finding against a diff."""

    code: FindingCode
    severity: Severity
    detail: str = Field(min_length=1)
    citation: str = Field(min_length=1)
    file_path: str | None = None


class PRReviewerVerdict(_FrozenModel):
    """Structured result from one isolated reviewer process."""

    reviewer_id: str = Field(min_length=1)
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: Literal["clean", "findings"]
    findings: tuple[PRFinding, ...] = ()

    @model_validator(mode="after")
    def validate_findings(self) -> Self:
        if self.verdict == "clean" and self.findings:
            raise ValueError("a clean reviewer verdict cannot carry findings")
        if self.verdict == "findings" and not self.findings:
            raise ValueError("a findings verdict must cite at least one finding")
        return self


class PRReviewReport(_FrozenModel):
    """Aggregate, content-addressed result of one full review round over one diff."""

    report_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    pr_ref: str = Field(min_length=1)
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_ids: tuple[str, ...] = Field(min_length=1)
    findings: tuple[PRFinding, ...] = ()
    blast_radius_hits: tuple[str, ...] = ()
    oversized: bool = False
    blocked: bool = False
    recorded_at: str

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if len(set(self.reviewer_ids)) != len(self.reviewer_ids):
            raise ValueError("reviewer ids must be unique")
        payload = self.model_dump(mode="json", exclude={"report_id"})
        if self.report_id != f"sha256:{_sha256(payload)}":
            raise ValueError("report_id does not match the review report")
        return self

    @classmethod
    def create(
        cls,
        *,
        pr_ref: str,
        diff_sha256: str,
        reviewer_ids: Sequence[str],
        findings: Sequence[PRFinding] = (),
        blast_radius_hits: Sequence[str] = (),
        oversized: bool = False,
        blocked: bool = False,
        recorded_at: str | None = None,
    ) -> PRReviewReport:
        payload = {
            "pr_ref": pr_ref,
            "diff_sha256": diff_sha256,
            "reviewer_ids": list(reviewer_ids),
            "findings": [finding.model_dump(mode="json") for finding in findings],
            "blast_radius_hits": list(blast_radius_hits),
            "oversized": oversized,
            "blocked": blocked,
            "recorded_at": recorded_at or datetime.now(UTC).isoformat(),
        }
        return cls.model_validate({**payload, "report_id": f"sha256:{_sha256(payload)}"})

    @property
    def highest_severity(self) -> Severity | None:
        if not self.findings:
            return None
        return max(self.findings, key=lambda finding: _SEVERITY_ORDER[finding.severity]).severity


class PRReviewer(Protocol):
    """One isolated review invocation over a single pull request diff."""

    def review(self, diff: PullRequestDiff, reviewer_id: str) -> PRReviewerVerdict: ...


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


class ClaudeSecurityReviewer:
    """Launch a fresh ``claude -p`` process per reviewer context, focused on security.

    Every invocation is a separate process with no shared conversation state, matching
    ``chitra.goal_enforcement.ClaudeProcessReviewer``.
    """

    def __init__(
        self,
        *,
        command: str = "claude",
        model: str | None = None,
        timeout_seconds: int = 180,
        runner: ProcessRunner = subprocess.run,
    ) -> None:
        self.command = command
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.runner = runner

    @staticmethod
    def _prompt(diff: PullRequestDiff, reviewer_id: str) -> str:
        request = {
            "reviewer_id": reviewer_id,
            "pr_ref": diff.pr_ref,
            "title": diff.title,
            "body": diff.body,
            "diff_sha256": diff.diff_sha256,
            "changed_files": [changed.model_dump(mode="json") for changed in diff.changed_files],
            "diff_text": diff.diff_text,
        }
        return (
            "You are an isolated security reviewer for one pull request diff. You did not write "
            "this code and have no stake in its author's claims. Probe only for concrete, citable "
            "security defects: hardcoded secrets or credentials, SQL or command injection, path "
            "traversal, SSRF, authentication/authorization bypass, insecure deserialization, risky "
            "new dependencies, and prompt injection in any LLM-facing code path. Do not flag style, "
            "performance, or correctness issues that carry no security implication. Return exactly "
            "one JSON object with reviewer_id, diff_sha256, verdict (clean or findings), and findings. "
            "Each finding needs code, severity, detail, citation (an exact line or hunk from the "
            "diff), and optionally file_path. The code MUST be exactly one of these literal values: "
            '"hardcoded_secret", "sql_injection", "command_injection", "path_traversal", "ssrf", '
            '"auth_bypass", "insecure_deserialization", "dependency_risk", "prompt_injection", or '
            '"other". The severity MUST be exactly one of: "low", "medium", "high", "critical". Do '
            "not invent any other code or severity string. Preserve all supplied identifiers exactly.\n"
            "INPUT=" + _canonical_json(request)
        )

    def review(self, diff: PullRequestDiff, reviewer_id: str) -> PRReviewerVerdict:
        command = [self.command, "-p", self._prompt(diff, reviewer_id), "--output-format", "text"]
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
            return PRReviewerVerdict.model_validate_json(completed.stdout.strip())
        except ValueError as exc:
            raise ReviewerProcessError(f"isolated reviewer {reviewer_id} returned invalid JSON: {exc}") from exc


def _validate_bound_review(review: PRReviewerVerdict, *, reviewer_id: str, diff: PullRequestDiff) -> None:
    if review.reviewer_id != reviewer_id:
        raise PRReviewError(f"isolated reviewer {reviewer_id} changed its assigned identity")
    if review.diff_sha256 != diff.diff_sha256:
        raise PRReviewError(f"isolated reviewer {reviewer_id} returned a stale or tampered diff binding")


def review_pull_request(
    diff: PullRequestDiff,
    *,
    reviewer: PRReviewer,
    policy: PRReviewPolicy | None = None,
) -> PRReviewReport:
    """Run the deterministic pre-checks, then an isolated multi-reviewer security pass.

    Findings from every reviewer are unioned (deduped by identical code + citation), not
    intersected: this is a report, not a vote. One reviewer citing a real issue matters
    even if another reviewer misses it, and there is deliberately no unanimity requirement
    to "clear" a PR, because nothing here blocks a merge unless the operator has opted
    into ``policy.block_on_findings`` (default off; see the module docstring).
    """
    policy = policy or PRReviewPolicy()
    hits = blast_radius_hits(diff.changed_files, policy.blast_radius_keywords)
    oversized = diff_is_oversized(diff, max_lines=policy.max_diff_lines, max_files=policy.max_diff_files)

    reviews: list[PRReviewerVerdict] = []
    for index in range(policy.reviewer_count):
        reviewer_id = f"pr-reviewer-{index + 1}"
        verdict = reviewer.review(diff, reviewer_id)
        _validate_bound_review(verdict, reviewer_id=reviewer_id, diff=diff)
        reviews.append(verdict)

    seen: set[tuple[str, str]] = set()
    findings: list[PRFinding] = []
    for verdict in reviews:
        for finding in verdict.findings:
            key = (finding.code, finding.citation)
            if key not in seen:
                seen.add(key)
                findings.append(finding)

    blocked = policy.block_on_findings and bool(findings)
    return PRReviewReport.create(
        pr_ref=diff.pr_ref,
        diff_sha256=diff.diff_sha256,
        reviewer_ids=[verdict.reviewer_id for verdict in reviews],
        findings=findings,
        blast_radius_hits=hits,
        oversized=oversized,
        blocked=blocked,
    )


def pr_review_log_path(root: Path) -> Path:
    return root / REVIEW_LOG_NAME


def append_pr_review(path: Path, report: PRReviewReport) -> None:
    """Append one deduplicated report under a lock, keyed by its content id."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        if json.loads(line).get("report_id") == report.report_id:
                            return
                    except (ValueError, AttributeError):
                        continue
            with path.open("a", encoding="utf-8") as output:
                output.write(report.model_dump_json() + "\n")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load_latest_pr_review(path: Path, pr_ref: str) -> PRReviewReport | None:
    """Return the newest valid report for one PR ref, or ``None`` if none is on file."""
    if not path.exists():
        return None
    latest: PRReviewReport | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            candidate = PRReviewReport.model_validate_json(line)
        except ValueError:
            continue
        if candidate.pr_ref == pr_ref:
            latest = candidate
    return latest
