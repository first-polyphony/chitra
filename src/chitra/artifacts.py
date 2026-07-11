"""Deterministic storage for Claude artifact publish and review state.

No LLM calls in this module's own code path — it only records operator-supplied
artifact metadata and explicit review state.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import structlog

from chitra.state_paths import state_dir

logger = structlog.get_logger(__name__)

ArtifactKind = Literal["interview", "product-review", "page"]
ReviewStatus = Literal["unreviewed", "reviewed"]
ARTIFACT_KINDS: tuple[ArtifactKind, ...] = ("interview", "product-review", "page")
REVIEW_STATUSES: tuple[ReviewStatus, ...] = ("unreviewed", "reviewed")
ARTIFACT_URL_PREFIX = "https://claude.ai/code/artifact/"
SCHEMA = "chitra.artifacts.v1"


class ArtifactValidationError(ValueError):
    """Raised when an artifact record is not valid monitor doctrine."""


class ArtifactNotFoundError(KeyError):
    """Raised when an operation requires an artifact record that is absent."""


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """The canonical persisted artifact metadata and explicit review state."""

    url: str
    title: str
    kind: ArtifactKind
    source: str
    published_at: str = ""
    updated_at: str = ""
    review_status: ReviewStatus = "unreviewed"
    reviewed_at: str = ""
    response: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "url": self.url,
            "title": self.title,
            "kind": self.kind,
            "source": self.source,
            "published_at": self.published_at,
            "updated_at": self.updated_at,
            "review_status": self.review_status,
            "reviewed_at": self.reviewed_at,
            "response": self.response,
        }


def artifacts_path(root: Path | None = None) -> Path:
    """Return the persistent artifact document path for ``root``."""
    return (state_dir() if root is None else root) / "artifacts.json"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso8601_utc(value: str, field: str) -> datetime:
    """Parse one UTC ISO8601 timestamp from persisted artifact state."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"artifact record {field} must be an ISO8601 UTC datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"artifact record {field} must be an ISO8601 UTC datetime")
    return parsed


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


def _record_from_dict(payload: object) -> ArtifactRecord:
    if not isinstance(payload, dict):
        raise ValueError("artifact record must be an object")
    fields = ("url", "title", "kind", "source", "published_at", "updated_at", "review_status", "reviewed_at", "response")
    values: dict[str, str] = {}
    for field in fields:
        value = payload.get(field)
        if not isinstance(value, str):
            raise ValueError(f"artifact record {field} must be a string")
        values[field] = value
    record = ArtifactRecord(
        url=values["url"],
        title=values["title"],
        kind=cast(ArtifactKind, values["kind"]),
        source=values["source"],
        published_at=values["published_at"],
        updated_at=values["updated_at"],
        review_status=cast(ReviewStatus, values["review_status"]),
        reviewed_at=values["reviewed_at"],
        response=values["response"],
    )
    issues = validate_artifact(record)
    if issues:
        raise ValueError("; ".join(issues))
    _parse_iso8601_utc(record.published_at, "published_at")
    _parse_iso8601_utc(record.updated_at, "updated_at")
    if record.reviewed_at:
        _parse_iso8601_utc(record.reviewed_at, "reviewed_at")
    return record


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
    return [_record_from_dict(item) for item in raw_artifacts]


def _write_artifacts(root: Path | None, records: list[ArtifactRecord]) -> None:
    path = artifacts_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": SCHEMA, "updated_at": _utc_now(), "artifacts": [record.to_dict() for record in records]}
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as tmp:
            tmp_name = tmp.name
            json.dump(payload, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
        os.replace(tmp_name, path)
    finally:
        if tmp_name is not None and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def get_artifact(root: Path | None, url: str) -> ArtifactRecord | None:
    """Return the record for ``url``, if the monitor has stored one."""
    return next((record for record in load_artifacts(root) if record.url == url), None)


def list_artifacts(root: Path | None = None) -> list[ArtifactRecord]:
    """Return all records in deterministic publish-time and URL order."""
    return sorted(load_artifacts(root), key=lambda record: (record.published_at, record.url))


def upsert_artifact(root: Path | None, rec: ArtifactRecord) -> ArtifactRecord:
    """Atomically record an artifact, resetting its review state on every upsert."""
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

    reviewed_command = commands.add_parser("mark-reviewed", help="Mark an existing artifact as reviewed.")
    add_root(reviewed_command)
    reviewed_command.add_argument("--url", required=True)
    reviewed_command.add_argument("--response", default="")

    list_command = commands.add_parser("list", help="Print all records as compact JSON lines.")
    add_root(list_command)

    unreviewed_command = commands.add_parser("unreviewed", help="Render the unreviewed artifact roster block.")
    add_root(unreviewed_command)

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
                    ArtifactRecord(url=args.url, title=args.title, kind=cast(ArtifactKind, args.kind), source=args.source),
                )
            )
        elif args.command == "mark-reviewed":
            _print_record(mark_reviewed(args.root, args.url, response=args.response))
        elif args.command == "list":
            for record in list_artifacts(args.root):
                print(json.dumps(record.to_dict(), separators=(",", ":"), sort_keys=True))
        elif args.command == "unreviewed":
            records = [record for record in list_artifacts(args.root) if record.review_status == "unreviewed"]
            if records:
                print("UNREVIEWED ARTIFACTS")
                for record in records:
                    print(f"- {record.kind}: {record.title} — {record.url} (published {record.published_at}, source {record.source})")
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
