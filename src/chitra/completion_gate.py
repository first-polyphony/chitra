"""Forced, citation-bearing completion review for watched agent turns."""

from __future__ import annotations

import enum
import fcntl
import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from chitra.artifacts import ArtifactValidationError, validate_delivery_brief
from chitra.taxonomy import TaxonomyEntry

if TYPE_CHECKING:
    from chitra.policy_config import GatePolicy

_DEFERRAL_PHRASES: tuple[str, ...] = (
    "you'll need to",
    "you will need to",
    "todo",
    "not implemented",
    "notimplemented",
    "out of scope",
    "leaving for",
    "leave for",
    "deferred",
    "deferring",
    "close follow-up",
    "close follow-ups",
    "follow-up items",
    "left as an exercise",
    "in a future pr",
    "future work",
    "conditionally healthy",
    "correctly blocked",
    "parse-only",
    "not publication-ready",
    "repaired and covered by tests",
    "CI evidence",
)

_OPERATIONALIZED_CODES: frozenset[str] = frozenset({"DEFERRAL_STUB", "FAKE_DONE"})
_COMPLETION_CLAIM_RE = re.compile(
    r"\b(done|complete(?:d)?|finished|fixed|repaired|shipped|deployed|publication-ready|ready for (?:merge|release))\b",
    re.I,
)
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.I)
_PATH_RE = re.compile(r"(?:^|\s)(?:/|\./)[^\s,;]+|\b[^\s]+\.(?:json|jsonl|log|png|jpg|jpeg|webp|txt)\b", re.I)
_PR_RE = re.compile(r"\b(?:merged\s+)?pr\s*#\d+\b", re.I)
_LIVE_RESULT_RE = re.compile(r"\b(?:health|probe|curl|http|status|requests?|latency|exit)\b[^\n]*\b\d+(?:\.\d+)?\b", re.I)
_FAILURE_RE = re.compile(r"\b(?:error|failed|failure|http)\b[^\n]*\b(?:[45]\d\d|\d+)\b", re.I)


class CompletionClaimEvent(enum.StrEnum):
    COMPLETION_CLAIM = "completion_claim"
    TURN_END_WITHOUT_CLAIM = "turn_end_without_completion_claim"


class TodoItem(BaseModel):
    text: str
    status: str


EvidenceKind = Literal["artifact", "deploy", "live_verify", "merged_pr", "failure"]


class CompletionEvidence(BaseModel):
    """One claimed proof item with the exact citation retained."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EvidenceKind
    citation: str = Field(min_length=1)
    todo_item: str | None = None

    @field_validator("citation")
    @classmethod
    def citation_is_not_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence citation must be non-empty")
        return value.strip()


class CompletionAudit(BaseModel):
    verdict: Literal["CLEAN", "COMPLETION_DISPUTE"]
    todo_residue: list[str]
    deferral_matches: list[dict[str, str]]
    evidence_gap: bool
    invalid_evidence: list[str]
    per_item_evidence_gap: list[str]
    brief_issues: list[str]
    posture_mismatch: bool
    summary: str


class TurnEndAudit(BaseModel):
    condition: Literal["completion_claim", "turn_end_without_completion_claim"]
    completion: CompletionAudit | None = None
    summary: str


class CompletionReviewRecord(BaseModel):
    """Our-side record; never included in text pasted to a lane."""

    session_ref: str
    pane_id: str
    behavior_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    condition: Literal["completion_claim", "turn_end_without_completion_claim"]
    completion_verdict: Literal["CLEAN", "COMPLETION_DISPUTE"] | None = None
    review_signal_id: str | None = None
    review_verdict: Literal["accept", "reject", "unavailable"]
    status: str
    summary: str
    recorded_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


def is_completion_claim(text: str) -> bool:
    """Return whether a completed turn adopts a literal completion posture."""
    return _COMPLETION_CLAIM_RE.search(text) is not None


def check_todo_residue(todo_items: list[TodoItem], *, complete_statuses: Sequence[str] = ("done",)) -> list[str]:
    return [item.text for item in todo_items if item.status not in complete_statuses]


def scan_deferral_language(
    text: str,
    taxonomy: Sequence[TaxonomyEntry],
    *,
    phrases: Sequence[str] | None = None,
) -> list[dict[str, str]]:
    available_codes = {entry.code for entry in taxonomy} & _OPERATIONALIZED_CODES
    if not available_codes:
        return []
    lowered = text.lower()
    matches: list[dict[str, str]] = []
    for phrase in phrases if phrases is not None else _DEFERRAL_PHRASES:
        if phrase.lower() in lowered:
            code = "DEFERRAL_STUB" if "DEFERRAL_STUB" in available_codes else next(iter(available_codes))
            matches.append({"phrase": phrase, "code": code})
    return matches


def evidence_is_concrete(evidence: CompletionEvidence) -> bool:
    """Reject labels/assertions that contain no independently locatable fact."""
    citation = evidence.citation
    if evidence.kind == "deploy":
        return _SHA_RE.search(citation) is not None
    if evidence.kind == "live_verify":
        return _PATH_RE.search(citation) is not None or _LIVE_RESULT_RE.search(citation) is not None
    if evidence.kind == "artifact":
        return _PATH_RE.search(citation) is not None
    if evidence.kind == "merged_pr":
        return _PR_RE.search(citation) is not None
    return _PATH_RE.search(citation) is not None or _FAILURE_RE.search(citation) is not None


def extract_completion_evidence(text: str) -> list[CompletionEvidence]:
    """Extract exact cited lines from a pane turn without converting claims to booleans."""
    evidence: list[CompletionEvidence] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        kinds: list[EvidenceKind] = []
        if _PR_RE.search(line):
            kinds.append("merged_pr")
        if _SHA_RE.search(line) and re.search(r"\b(deploy|release|sha|commit)\b", line, re.I):
            kinds.append("deploy")
        if _LIVE_RESULT_RE.search(line) or (re.search(r"\blive[- ]?verif", line, re.I) and _PATH_RE.search(line)):
            kinds.append("live_verify")
        if _PATH_RE.search(line) and re.search(r"\b(proof|artifact|screenshot|record|log|report)\b", line, re.I):
            kinds.append("artifact")
        if _FAILURE_RE.search(line):
            kinds.append("failure")
        for kind in kinds:
            key = (kind, line)
            if key not in seen:
                evidence.append(CompletionEvidence(kind=kind, citation=line))
                seen.add(key)
    return evidence


def evaluate_completion_claim(
    todo_items: list[TodoItem],
    transcript_text: str,
    evidence: Sequence[CompletionEvidence],
    taxonomy: Sequence[TaxonomyEntry],
    *,
    policy: GatePolicy | None = None,
    delivery_brief: str | None = None,
    open_asks: Sequence[str] = (),
    blockers: Sequence[str] = (),
) -> CompletionAudit:
    """Require cited deploy/live proof, honest posture, and an outcome brief."""
    if policy is None:
        from chitra.policy_config import GatePolicy

        policy = GatePolicy()
    todo_residue = check_todo_residue(todo_items, complete_statuses=policy.complete_todo_statuses)
    deferral_matches = scan_deferral_language(transcript_text, taxonomy, phrases=policy.deferral_phrases)
    invalid_evidence = [item.citation for item in evidence if not evidence_is_concrete(item)]
    valid = [item for item in evidence if evidence_is_concrete(item)]
    valid_kinds = {item.kind for item in valid}
    missing_evidence: list[str] = []
    if "deploy" in policy.required_evidence and "deploy" not in valid_kinds:
        missing_evidence.append("deploy evidence citation")
    if "live_verify" in policy.required_evidence and "live_verify" not in valid_kinds:
        missing_evidence.append("live-verify evidence citation")
    evidence_gap = bool(missing_evidence or invalid_evidence)
    per_item_evidence_gap = [
        item.text
        for item in todo_items
        if item.status in policy.complete_todo_statuses and not any(proof.todo_item == item.text for proof in valid)
    ]
    brief_issues: list[str] = []
    try:
        validate_delivery_brief(delivery_brief if delivery_brief is not None else transcript_text)
    except ArtifactValidationError as exc:
        brief_issues.append(str(exc))
    blocked_todos = [item.text for item in todo_items if item.status.lower() in {"blocked", "stalled"}]
    posture_mismatch = bool(blocked_todos and not open_asks and not blockers)

    clean = not (
        todo_residue
        or deferral_matches
        or evidence_gap
        or per_item_evidence_gap
        or brief_issues
        or posture_mismatch
    )
    if clean:
        return CompletionAudit(
            verdict="CLEAN",
            todo_residue=[],
            deferral_matches=[],
            evidence_gap=False,
            invalid_evidence=[],
            per_item_evidence_gap=[],
            brief_issues=[],
            posture_mismatch=False,
            summary="clean: cited deploy and live verification evidence, per-item proof, and delivery brief all validate",
        )

    gaps: list[str] = []
    if todo_residue:
        gaps.append(f"{len(todo_residue)} non-done todo item(s): {todo_residue!r}")
    if deferral_matches:
        gaps.append(f"deferral language detected: {[match['phrase'] for match in deferral_matches]!r}")
    if missing_evidence:
        gaps.append("missing " + ", ".join(missing_evidence))
    if invalid_evidence:
        gaps.append(f"bare or non-concrete evidence assertions: {invalid_evidence!r}")
    if per_item_evidence_gap:
        gaps.append(f"missing per-item verification: {per_item_evidence_gap!r}")
    if brief_issues:
        gaps.append(f"delivery brief invalid: {brief_issues[0]}")
    if posture_mismatch:
        gaps.append(f"blocked todo posture has no open ask or blocker: {blocked_todos!r}")
    return CompletionAudit(
        verdict="COMPLETION_DISPUTE",
        todo_residue=todo_residue,
        deferral_matches=deferral_matches,
        evidence_gap=evidence_gap,
        invalid_evidence=invalid_evidence,
        per_item_evidence_gap=per_item_evidence_gap,
        brief_issues=brief_issues,
        posture_mismatch=posture_mismatch,
        summary="completion dispute: " + "; ".join(gaps),
    )


def evaluate_turn_end(
    transcript_text: str,
    *,
    todo_items: list[TodoItem],
    evidence: Sequence[CompletionEvidence],
    taxonomy: Sequence[TaxonomyEntry],
    policy: GatePolicy | None = None,
    open_asks: Sequence[str] = (),
    blockers: Sequence[str] = (),
) -> TurnEndAudit:
    """Force a review at turn-end while distinguishing a non-completion turn."""
    if not is_completion_claim(transcript_text):
        return TurnEndAudit(
            condition="turn_end_without_completion_claim",
            summary="turn ended without a completion claim; lane is not complete",
        )
    completion = evaluate_completion_claim(
        todo_items,
        transcript_text,
        evidence,
        taxonomy,
        policy=policy,
        delivery_brief=transcript_text,
        open_asks=open_asks,
        blockers=blockers,
    )
    return TurnEndAudit(condition="completion_claim", completion=completion, summary=completion.summary)


def append_completion_review(path: Path, record: CompletionReviewRecord) -> None:
    """Append one deduplicated internal turn-end review record under a lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    key = (record.session_ref, record.behavior_sha256)
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        payload = json.loads(line)
                    except ValueError:
                        continue
                    if (payload.get("session_ref"), payload.get("behavior_sha256")) == key:
                        return
            with path.open("a", encoding="utf-8") as output:
                output.write(record.model_dump_json() + "\n")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def behavior_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
