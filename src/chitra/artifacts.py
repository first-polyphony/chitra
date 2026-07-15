"""Deterministic storage for Claude artifact publish and review state.

No LLM calls in this module's own code path — it only records operator-supplied
artifact metadata and explicit review state.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self, cast

import structlog
from pydantic import ConfigDict, TypeAdapter, ValidationInfo, model_validator
from pydantic.dataclasses import dataclass as pydantic_dataclass

from chitra._fsio import parse_iso8601, write_json_atomic
from chitra.lexicon import ARTIFACT_PROCESS_ONLY_RE, ARTIFACT_WORK_EVIDENCE_RE
from chitra.state_paths import state_dir

logger = structlog.get_logger(__name__)

ArtifactKind = Literal["interview", "product-review", "page"]
ReviewStatus = Literal["unreviewed", "reviewed"]
ARTIFACT_KINDS: tuple[ArtifactKind, ...] = ("interview", "product-review", "page")
REVIEW_STATUSES: tuple[ReviewStatus, ...] = ("unreviewed", "reviewed")
ARTIFACT_URL_PREFIX = "https://claude.ai/code/artifact/"
SCHEMA = "chitra.artifacts.v1"

_BRIEF_LABEL_RE = re.compile(
    r"(?im)^\s*(what was built|what it does|does it actually work)\s*:\s*",
)
class ArtifactValidationError(ValueError):
    """Raised when an artifact record is not valid monitor doctrine."""


class ArtifactNotFoundError(KeyError):
    """Raised when an operation requires an artifact record that is absent."""


@dataclass(frozen=True, slots=True)
class DeliveryBrief:
    """The three outcome questions required before an artifact is recorded."""

    what_was_built: str
    what_it_does: str
    does_it_actually_work: str


def validate_delivery_brief(brief: str) -> DeliveryBrief:
    """Lint the sidecar-authored brief supplied to the guarded record CLI."""
    text = brief.strip()
    matches = list(_BRIEF_LABEL_RE.finditer(text))
    sections: dict[str, str] = {}
    names = {
        "what was built": "what_was_built",
        "what it does": "what_it_does",
        "does it actually work": "does_it_actually_work",
    }
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[names[match.group(1).lower()]] = text[match.end() : end].strip()
    missing = [label for label, field in names.items() if not sections.get(field)]
    if missing:
        raise ArtifactValidationError("brief must answer what was built, what it does, and does it actually work")
    if any(len(value.split()) < 3 for value in sections.values()):
        raise ArtifactValidationError("brief answers must contain concrete outcome detail")
    if all(ARTIFACT_PROCESS_ONLY_RE.search(value) for value in sections.values()):
        raise ArtifactValidationError("brief is process narration rather than a delivery result")
    if not ARTIFACT_WORK_EVIDENCE_RE.search(sections["does_it_actually_work"]):
        raise ArtifactValidationError("does-it-actually-work must cite a SHA, path, probe result, or numbered check output")
    return DeliveryBrief(**sections)


def brief_is_conforming(brief: str) -> bool:
    """Return whether a stored brief passes the delivery-brief content gate.

    The guarded CLI (``upsert_artifact``) enforces this on every write, but a
    record written straight to ``artifacts.json`` bypasses that gate. This
    predicate lets the load path FLAG such records instead of failing the whole
    roster load — so a bypass becomes visible rather than silent.
    """
    try:
        validate_delivery_brief(brief)
    except ArtifactValidationError:
        return False
    return True


@pydantic_dataclass(frozen=True, slots=True, config=ConfigDict(strict=True))
class ArtifactRecord:
    """The canonical persisted artifact metadata and explicit review state."""

    url: str
    title: str
    kind: ArtifactKind
    source: str
    brief: str = ""
    published_at: str = ""
    updated_at: str = ""
    review_status: ReviewStatus = "unreviewed"
    reviewed_at: str = ""
    response: str = ""
    brief_conforming: bool = True
    """Derived on load; NOT persisted. False marks a record that bypassed the
    guarded CLI with a non-conforming brief (the F8 direct-JSON-write bypass)."""

    def to_dict(self) -> dict[str, str]:
        return cast(
            dict[str, str],
            _ARTIFACT_RECORD_ADAPTER.dump_python(self, mode="json", exclude={"brief_conforming"}),
        )

    @classmethod
    def from_dict(cls, payload: object) -> ArtifactRecord:
        return _ARTIFACT_RECORD_ADAPTER.validate_python(payload, strict=False, context={"persisted": True})

    @model_validator(mode="before")
    @classmethod
    def validate_persisted(cls, payload: object, info: ValidationInfo) -> object:
        """Validate required v1 strings and derive the non-persisted brief flag."""
        if not info.context or not info.context.get("persisted"):
            return payload
        if not isinstance(payload, dict):
            raise ValueError("artifact record must be an object")
        normalized = dict(payload)
        for field in ("url", "title", "kind", "source", "published_at", "updated_at", "review_status", "reviewed_at", "response"):
            value = payload.get(field)
            if not isinstance(value, str):
                raise ValueError(f"artifact record {field} must be a string")
            normalized[field] = value
        raw_brief = payload.get("brief", "")
        brief = raw_brief if isinstance(raw_brief, str) else ""
        normalized["brief"] = brief
        normalized["brief_conforming"] = brief_is_conforming(brief)
        if not normalized["brief_conforming"]:
            logger.warning(
                "artifact_brief_nonconforming",
                url=normalized["url"],
                reason="brief bypassed the guarded record CLI (F8 direct-write) or fails the delivery-brief gate",
            )
        return normalized

    @model_validator(mode="after")
    def validate_persisted_record(self, info: ValidationInfo) -> Self:
        if not info.context or not info.context.get("persisted"):
            return self
        issues = validate_artifact(self)
        if issues:
            raise ValueError("; ".join(issues))
        parse_iso8601(
            self.published_at,
            invalid_message="artifact record published_at must be an ISO8601 UTC datetime",
            require_utc=True,
        )
        parse_iso8601(
            self.updated_at,
            invalid_message="artifact record updated_at must be an ISO8601 UTC datetime",
            require_utc=True,
        )
        if self.reviewed_at:
            parse_iso8601(
                self.reviewed_at,
                invalid_message="artifact record reviewed_at must be an ISO8601 UTC datetime",
                require_utc=True,
            )
        return self


_ARTIFACT_RECORD_ADAPTER = TypeAdapter(ArtifactRecord)


def artifacts_path(root: Path | None = None) -> Path:
    """Return the persistent artifact document path for ``root``."""
    return (state_dir() if root is None else root) / "artifacts.json"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_response(response: str) -> None:
    """Require non-empty saved operator responses to be valid JSON."""
    if not response:
        return
    try:
        json.loads(response)
    except json.JSONDecodeError as exc:
        raise ArtifactValidationError("response must be valid JSON") from exc


def validate_artifact(rec: ArtifactRecord) -> list[str]:
    """Return deterministic doctrine violations for a prospective artifact."""
    issues: list[str] = []
    if not rec.url.startswith(ARTIFACT_URL_PREFIX):
        issues.append(f"url must start with {ARTIFACT_URL_PREFIX}")
    if not rec.title.strip():
        issues.append("title must be non-empty")
    if rec.kind not in ARTIFACT_KINDS:
        issues.append(f"kind must be one of {', '.join(ARTIFACT_KINDS)}")
    if not rec.source.strip():
        issues.append("source must be non-empty")
    if rec.review_status not in REVIEW_STATUSES:
        issues.append(f"review_status must be one of {', '.join(REVIEW_STATUSES)}")
    if rec.review_status == "unreviewed" and rec.reviewed_at:
        issues.append("unreviewed artifacts must not have reviewed_at")
    if rec.review_status == "reviewed" and not rec.reviewed_at:
        issues.append("reviewed artifacts must have reviewed_at")
    try:
        _validate_response(rec.response)
    except ArtifactValidationError as exc:
        issues.append(str(exc))
    return issues


def load_artifacts(root: Path | None = None) -> list[ArtifactRecord]:
    """Load stored records; a missing store has no recorded artifacts."""
    path = artifacts_path(root)
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("artifacts.json is not a chitra.artifacts.v1 document")
    raw_artifacts = payload.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise ValueError("artifacts.json artifacts must be a list")
    return [ArtifactRecord.from_dict(item) for item in raw_artifacts]


def _write_artifacts(root: Path | None, records: list[ArtifactRecord]) -> None:
    path = artifacts_path(root)
    payload = {"schema": SCHEMA, "updated_at": _utc_now(), "artifacts": [record.to_dict() for record in records]}
    write_json_atomic(path, payload)


def get_artifact(root: Path | None, url: str) -> ArtifactRecord | None:
    """Return the record for ``url``, if the monitor has stored one."""
    return next((record for record in load_artifacts(root) if record.url == url), None)


def list_artifacts(root: Path | None = None) -> list[ArtifactRecord]:
    """Return all records in deterministic publish-time and URL order."""
    return sorted(load_artifacts(root), key=lambda record: (record.published_at, record.url))


def list_unreviewed_artifacts(root: Path | None = None) -> list[ArtifactRecord]:
    """Return unreviewed records in deterministic publish-time and URL order."""
    return [record for record in list_artifacts(root) if record.review_status == "unreviewed"]


def upsert_artifact(root: Path | None, rec: ArtifactRecord) -> ArtifactRecord:
    """Atomically record an artifact, resetting its review state on every upsert."""
    validate_delivery_brief(rec.brief)
    issues = validate_artifact(rec)
    if issues:
        raise ArtifactValidationError("; ".join(issues))
    existing = get_artifact(root, rec.url)
    now = _utc_now()
    stored = ArtifactRecord(
        url=rec.url,
        title=rec.title,
        kind=rec.kind,
        source=rec.source,
        brief=rec.brief,
        published_at=existing.published_at if existing is not None else now,
        updated_at=now,
    )
    records = [record for record in load_artifacts(root) if record.url != rec.url]
    records.append(stored)
    _write_artifacts(root, records)
    logger.info("artifact_mutated", url=stored.url, action="record")
    return stored


def mark_reviewed(root: Path | None, url: str, *, response: str = "") -> ArtifactRecord:
    """Mark one stored artifact reviewed, retaining an optional exact JSON response."""
    _validate_response(response)
    existing = get_artifact(root, url)
    if existing is None:
        raise ArtifactNotFoundError(f"artifact not found: {url}")
    reviewed = replace(existing, updated_at=_utc_now(), review_status="reviewed", reviewed_at=_utc_now(), response=response)
    records = [record for record in load_artifacts(root) if record.url != url]
    records.append(reviewed)
    _write_artifacts(root, records)
    logger.info("artifact_mutated", url=url, action="mark_reviewed")
    return reviewed


def _print_record(record: ArtifactRecord) -> None:
    print(json.dumps(record.to_dict(), indent=2, sort_keys=True))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chitra-artifacts", description="Store deterministic Claude artifact publish and review state.")
    parser.add_argument("--root", type=Path, default=state_dir())
    commands = parser.add_subparsers(dest="command", required=True)

    def add_root(command: argparse.ArgumentParser) -> None:
        command.add_argument("--root", type=Path, default=argparse.SUPPRESS)

    record_command = commands.add_parser("record", help="Create or update an artifact record.")
    add_root(record_command)
    record_command.add_argument("--url", required=True)
    record_command.add_argument("--title", required=True)
    record_command.add_argument("--kind", choices=ARTIFACT_KINDS, required=True)
    record_command.add_argument("--source", required=True)
    record_command.add_argument("--brief", required=True)

    reviewed_command = commands.add_parser("mark-reviewed", help="Mark an existing artifact as reviewed.")
    add_root(reviewed_command)
    reviewed_command.add_argument("--url", required=True)
    reviewed_command.add_argument("--response", default="")

    list_command = commands.add_parser("list", help="Print all records as compact JSON lines.")
    add_root(list_command)

    unreviewed_command = commands.add_parser("unreviewed", help="Render the unreviewed artifact roster block.")
    add_root(unreviewed_command)

    nonconforming_command = commands.add_parser("nonconforming", help="List records whose brief bypassed the guarded CLI (F8).")
    add_root(nonconforming_command)

    get_command = commands.add_parser("get", help="Print one artifact record as JSON.")
    add_root(get_command)
    get_command.add_argument("--url", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if args.command == "record":
            _print_record(
                upsert_artifact(
                    args.root,
                    ArtifactRecord(
                        url=args.url,
                        title=args.title,
                        kind=cast(ArtifactKind, args.kind),
                        source=args.source,
                        brief=args.brief,
                    ),
                )
            )
        elif args.command == "mark-reviewed":
            _print_record(mark_reviewed(args.root, args.url, response=args.response))
        elif args.command == "list":
            for record in list_artifacts(args.root):
                print(json.dumps(record.to_dict(), separators=(",", ":"), sort_keys=True))
        elif args.command == "unreviewed":
            records = list_unreviewed_artifacts(args.root)
            if records:
                print("UNREVIEWED ARTIFACTS")
                for record in records:
                    flag = "" if record.brief_conforming else "  ⚠ NON-CONFORMING BRIEF (bypassed the record CLI — re-record)"
                    print(f"- {record.kind}: {record.title} — {record.url} (published {record.published_at}, source {record.source}){flag}")
        elif args.command == "nonconforming":
            flagged = [record for record in list_artifacts(args.root) if not record.brief_conforming]
            if flagged:
                print("NON-CONFORMING ARTIFACT BRIEFS (bypassed the guarded record CLI — F8)")
                for record in flagged:
                    print(f"- {record.kind}: {record.title} — {record.url} (review_status {record.review_status}, source {record.source})")
        else:
            found_record = get_artifact(args.root, args.url)
            if found_record is None:
                raise ArtifactNotFoundError(f"artifact not found: {args.url}")
            _print_record(found_record)
    except (ArtifactValidationError, ArtifactNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"chitra-artifacts: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
