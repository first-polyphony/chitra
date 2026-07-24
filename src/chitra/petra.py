"""Petra dark-launch authority: validate and durably observe, never act.

The only accepted input is advisory pressure evidence.  Petra verifies the
event's Chitra ownership fence, atomically records a decision plus outbox
entry, and acknowledges it.  There is intentionally no execution adapter in
this module: no holds, signals, tmux, subprocesses, rate-limit guard calls,
code-change dispatch, pull-request enrollment, merge, or deployment calls.
The advisory observation schema cannot authorize any of those operations.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import pwd
import re
import sqlite3
import stat
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from chitra.ownership_provider import (
    DEFAULT_TIMEOUT_SECONDS,
    PROVIDER_ID,
    QUERY_SCHEMA,
    RESULT_SCHEMA,
    ProtocolError,
    canonical_session_parts,
    format_timestamp,
    parse_timestamp,
    request_json_line,
    serve_unix_json_lines,
    utc_now,
    write_json_atomic,
)

OBSERVATION_SCHEMA: Final = "petra.pressure-observation.v1"
ACK_SCHEMA: Final = "petra.observation-ack.v1"
HEALTH_SCHEMA: Final = "petra.authority-health.v1"
LEDGER_SCHEMA: Final = "petra.decision-outbox.v1"
OUTBOX_EVENT_SCHEMA: Final = "petra.observation-recorded.v1"
MODE: Final = "observe"
DEFAULT_SOCKET_PATH = Path("/run/chitra-petra/petra.sock")
DEFAULT_HEALTH_PATH = Path("/run/chitra-petra/health.json")
DEFAULT_LEDGER_PATH = Path("/var/lib/chitra-petra/decision-outbox.sqlite3")
DEFAULT_OWNERSHIP_SOCKET_PATH = Path("/run/chitra-ownership/provider.sock")
MAX_EVIDENCE_BYTES = 32 * 1024
MAX_OBSERVATION_LIFETIME_SECONDS = 300.0
MAX_OWNERSHIP_AGE_SECONDS = 30.0
MAX_LEDGER_EVENTS = 10_000
MAX_IDENTIFIER_LENGTH = 256
MAX_SESSION_REF_LENGTH = 512

_OBSERVATION_FIELDS = frozenset(
    ("schema", "event_id", "host_id", "boot_id", "session_ref", "observed_at", "expires_at", "ownership", "evidence")
)
_EVIDENCE_FIELDS = frozenset(("ring0_policy_generation", "resource", "evidence_digest"))
_OWNERSHIP_FIELDS = frozenset(
    ("query_id", "provider_id", "provider_instance_id", "lane_id", "lane_generation", "ownership_generation")
)
_OWNERSHIP_RESULT_FIELDS = frozenset(
    (
        "schema",
        "request_id",
        "host_id",
        "boot_id",
        "provider_id",
        "provider_instance_id",
        "generated_at",
        "valid_until",
        "authoritative",
        "source",
        "result",
    )
)
_OWNERSHIP_SOURCE_FIELDS = frozenset(("schema", "generation", "complete", "manager_heartbeat_at"))
_OWNED_RESULT_FIELDS = frozenset(("session_ref", "status", "lane_id", "lane_generation"))
_RESOURCE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _required_string(payload: Mapping[str, object], field: str, *, maximum: int = MAX_IDENTIFIER_LENGTH) -> str:
    value = payload.get(field)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or any(ord(char) < 0x20 for char in value)
    ):
        raise ProtocolError("invalid_observation", f"{field} must be a non-empty canonical string")
    return value


def validate_observation(payload: object, *, host_uuid: str, now: datetime | None = None) -> dict[str, object]:
    """Validate the strict advisory envelope without recording anything."""
    if not isinstance(payload, dict) or set(payload) != _OBSERVATION_FIELDS:
        raise ProtocolError("invalid_observation_fields", "pressure observation fields must match the v1 contract exactly")
    if payload.get("schema") != OBSERVATION_SCHEMA:
        raise ProtocolError("unsupported_schema", "unsupported Petra input schema")
    event_id = _required_string(payload, "event_id")
    if len(event_id) > 200:
        raise ProtocolError("invalid_observation", "event_id is too long")
    host_id = _required_string(payload, "host_id")
    boot_id = _required_string(payload, "boot_id")
    session_ref = _required_string(payload, "session_ref", maximum=MAX_SESSION_REF_LENGTH)
    if host_id != host_uuid:
        raise ProtocolError("host_mismatch", "observation host_id does not match this Petra authority")
    try:
        ref_host, ref_lane, _ = canonical_session_parts(session_ref)
        observed_at = parse_timestamp(payload["observed_at"], field="observed_at")
        expires_at = parse_timestamp(payload["expires_at"], field="expires_at")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("invalid_observation", str(exc)) from exc
    current = utc_now() if now is None else now.astimezone(UTC)
    if ref_host != host_id:
        raise ProtocolError("host_mismatch", "session_ref host does not match host_id")
    if expires_at <= observed_at:
        raise ProtocolError("invalid_expiry", "expires_at must be after observed_at")
    if expires_at <= current:
        raise ProtocolError("expired_observation", "pressure observation has expired")
    if (observed_at - current).total_seconds() > 5.0:
        raise ProtocolError("future_observation", "observed_at is too far in the future")
    if (current - observed_at).total_seconds() > MAX_OBSERVATION_LIFETIME_SECONDS:
        raise ProtocolError("stale_observation", "observed_at is too old for advisory use")
    if (expires_at - observed_at).total_seconds() > MAX_OBSERVATION_LIFETIME_SECONDS:
        raise ProtocolError("invalid_expiry", "observation lifetime exceeds the advisory limit")

    ownership = payload["ownership"]
    if not isinstance(ownership, dict) or set(ownership) != _OWNERSHIP_FIELDS:
        raise ProtocolError("invalid_ownership", "ownership fields must match the v1 contract exactly")
    query_id = _required_string(ownership, "query_id")
    try:
        if str(uuid.UUID(query_id)) != query_id:
            raise ValueError("query_id is not canonical")
    except (AttributeError, ValueError) as exc:
        raise ProtocolError("invalid_ownership", "query_id must be a canonical UUID") from exc
    provider_id = _required_string(ownership, "provider_id")
    provider_instance_id = _required_string(ownership, "provider_instance_id")
    lane_id = _required_string(ownership, "lane_id")
    lane_generation = ownership.get("lane_generation")
    ownership_generation = ownership.get("ownership_generation")
    if provider_id != PROVIDER_ID:
        raise ProtocolError("invalid_ownership", "ownership provider_id must be chitra")
    if lane_id != ref_lane:
        raise ProtocolError("invalid_ownership", "ownership lane_id must match the exact session_ref")
    if isinstance(lane_generation, bool) or not isinstance(lane_generation, int) or lane_generation < 1:
        raise ProtocolError("invalid_ownership", "lane_generation must be a positive integer")
    if isinstance(ownership_generation, bool) or not isinstance(ownership_generation, int) or ownership_generation < 1:
        raise ProtocolError("invalid_ownership", "ownership_generation must be a positive integer")

    evidence = payload["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != _EVIDENCE_FIELDS:
        raise ProtocolError("invalid_evidence", "evidence fields must match the advisory v1 contract exactly")
    policy_generation = evidence.get("ring0_policy_generation")
    resource = evidence.get("resource")
    evidence_digest = evidence.get("evidence_digest")
    if isinstance(policy_generation, bool) or not isinstance(policy_generation, int) or policy_generation < 1:
        raise ProtocolError("invalid_evidence", "ring0_policy_generation must be a positive integer")
    if not isinstance(resource, str) or _RESOURCE_RE.fullmatch(resource) is None:
        raise ProtocolError("invalid_evidence", "resource must be a bounded canonical identifier")
    if not isinstance(evidence_digest, str) or _SHA256_DIGEST_RE.fullmatch(evidence_digest) is None:
        raise ProtocolError("invalid_evidence", "evidence_digest must be a sha256 digest")

    return {
        "schema": OBSERVATION_SCHEMA,
        "event_id": event_id,
        "host_id": host_id,
        "boot_id": boot_id,
        "session_ref": session_ref,
        "observed_at": format_timestamp(observed_at),
        "expires_at": format_timestamp(expires_at),
        "ownership": {
            "query_id": query_id,
            "provider_id": provider_id,
            "provider_instance_id": provider_instance_id,
            "lane_id": lane_id,
            "lane_generation": lane_generation,
            "ownership_generation": ownership_generation,
        },
        "evidence": {
            "ring0_policy_generation": policy_generation,
            "resource": resource,
            "evidence_digest": evidence_digest,
        },
    }


def ownership_query_for(observation: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema": QUERY_SCHEMA,
        "request_id": str(uuid.uuid4()),
        "host_id": observation["host_id"],
        "boot_id": observation["boot_id"],
        "session_ref": observation["session_ref"],
    }


@dataclass(frozen=True, slots=True)
class ValidatedOwnershipFence:
    """Immutable Chitra proof persisted beside the observed Petra decision."""

    provider_instance_id: str
    ownership_generation: int
    response: dict[str, object]


def validate_ownership_fence(
    response: object,
    *,
    query: Mapping[str, object],
    observation: Mapping[str, object],
    now: datetime | None = None,
) -> ValidatedOwnershipFence:
    """Require a live, exact Chitra owned result for the submitted fence."""
    if not isinstance(response, dict) or set(response) != _OWNERSHIP_RESULT_FIELDS or response.get("schema") != RESULT_SCHEMA:
        raise ProtocolError("ownership_unavailable", "Chitra returned no ownership result")
    for field in ("request_id", "host_id", "boot_id"):
        if response.get(field) != query[field]:
            raise ProtocolError("ownership_mismatch", f"Chitra {field} echo does not match")
    if response.get("provider_id") != PROVIDER_ID or response.get("authoritative") is not True:
        raise ProtocolError("ownership_not_authoritative", "Chitra ownership is not authoritative")
    source = response.get("source")
    result = response.get("result")
    expected = observation["ownership"]
    if (
        not isinstance(source, dict)
        or set(source) != _OWNERSHIP_SOURCE_FIELDS
        or source.get("schema") != "chitra.goals.v1"
        or source.get("complete") is not True
    ):
        raise ProtocolError("ownership_not_ready", "Chitra managed state is incomplete")
    if (
        not isinstance(result, dict)
        or set(result) != _OWNED_RESULT_FIELDS
        or result.get("status") != "owned"
        or result.get("session_ref") != observation["session_ref"]
    ):
        raise ProtocolError("ownership_not_owned", "Chitra does not own the exact session_ref")
    if not isinstance(expected, dict):
        raise ProtocolError("invalid_ownership", "ownership fence is malformed")
    if response.get("provider_instance_id") != expected["provider_instance_id"]:
        raise ProtocolError("ownership_instance_mismatch", "Chitra provider instance does not match the observation fence")
    if result.get("lane_id") != expected["lane_id"]:
        raise ProtocolError("ownership_lane_mismatch", "Chitra lane_id does not match the observation fence")
    if result.get("lane_generation") != expected["lane_generation"]:
        raise ProtocolError("ownership_generation_mismatch", "Chitra lane generation does not match the observation fence")
    if source.get("generation") != expected["ownership_generation"]:
        raise ProtocolError("ownership_generation_mismatch", "Chitra ownership generation does not match the observation fence")
    try:
        generated_at = parse_timestamp(response.get("generated_at"), field="ownership.generated_at")
        valid_until = parse_timestamp(response.get("valid_until"), field="ownership.valid_until")
        heartbeat_at = parse_timestamp(source.get("manager_heartbeat_at"), field="ownership.manager_heartbeat_at")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("ownership_unavailable", str(exc)) from exc
    current = utc_now() if now is None else now.astimezone(UTC)
    if generated_at > current + timedelta(seconds=5) or (current - generated_at).total_seconds() > MAX_OWNERSHIP_AGE_SECONDS:
        raise ProtocolError("ownership_unavailable", "Chitra ownership response is stale or clock-skewed")
    if heartbeat_at > current + timedelta(seconds=5) or (current - heartbeat_at).total_seconds() > MAX_OWNERSHIP_AGE_SECONDS:
        raise ProtocolError("ownership_not_ready", "Chitra manager heartbeat is stale or clock-skewed")
    if (
        valid_until <= current
        or valid_until <= generated_at
        or (valid_until - generated_at).total_seconds() > MAX_OWNERSHIP_AGE_SECONDS
    ):
        raise ProtocolError("ownership_expired", "Chitra ownership result has expired")
    return ValidatedOwnershipFence(
        provider_instance_id=str(response["provider_instance_id"]),
        ownership_generation=int(expected["ownership_generation"]),
        response=dict(response),
    )


def _canonical_json(value: Mapping[str, object]) -> str:
    """Serialize an already-validated record without ambiguity or NaN."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _require_private_regular_file(path: Path) -> None:
    """Reject a ledger file that is not owned and private to this authority."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ProtocolError("ledger_unavailable", "Petra decision ledger is not a private regular file")


def _ledger_connection(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    _require_private_regular_file(path)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path, timeout=2.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=2000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS petra_observations (
                event_id TEXT PRIMARY KEY,
                observation_sha256 TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode = 'observe'),
                disposition TEXT NOT NULL CHECK(disposition = 'observed'),
                observation_json TEXT NOT NULL,
                ownership_fence_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS petra_outbox (
                event_id TEXT PRIMARY KEY REFERENCES petra_observations(event_id) ON DELETE RESTRICT,
                schema TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode = 'observe'),
                disposition TEXT NOT NULL CHECK(disposition = 'observed')
            )
            """
        )
        os.chmod(path, 0o600)
        _require_private_regular_file(path)
        return connection
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            connection.close()
        raise ProtocolError("ledger_unavailable", "Petra decision ledger is unavailable") from exc


def record_observation(
    path: Path,
    observation: Mapping[str, object],
    *,
    ownership_fence: Mapping[str, object],
    recorded_at: datetime | None = None,
) -> str:
    """Transactionally record one advisory decision and exactly one outbox row.

    Event IDs are idempotent only when the canonical observation bytes match.
    A conflicting duplicate is an explicit safety error rather than silent loss.
    The fixed capacity rejects new records instead of silently pruning evidence.
    """

    current = utc_now() if recorded_at is None else recorded_at.astimezone(UTC)
    timestamp = format_timestamp(current)
    try:
        observation_json = _canonical_json(observation)
        fence_json = _canonical_json(ownership_fence)
    except (TypeError, ValueError) as exc:
        raise ProtocolError("ledger_unavailable", "Petra record cannot be canonically serialized") from exc
    digest = hashlib.sha256(observation_json.encode("utf-8")).hexdigest()
    event_id = str(observation["event_id"])
    connection = _ledger_connection(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT observation_sha256 FROM petra_observations WHERE event_id = ?", (event_id,)
        ).fetchone()
        if existing is not None:
            if existing[0] != digest:
                raise ProtocolError("conflicting_duplicate", "event_id was already recorded with different evidence")
            connection.execute("COMMIT")
            return "duplicate"
        count = connection.execute("SELECT COUNT(*) FROM petra_observations").fetchone()
        if count is None or int(count[0]) >= MAX_LEDGER_EVENTS:
            raise ProtocolError("ledger_capacity", "Petra advisory ledger capacity is exhausted")
        connection.execute(
            """
            INSERT INTO petra_observations
                (event_id, observation_sha256, recorded_at, mode, disposition, observation_json, ownership_fence_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, digest, timestamp, MODE, "observed", observation_json, fence_json),
        )
        connection.execute(
            "INSERT INTO petra_outbox (event_id, schema, recorded_at, mode, disposition) VALUES (?, ?, ?, ?, ?)",
            (event_id, OUTBOX_EVENT_SCHEMA, timestamp, MODE, "observed"),
        )
        connection.execute("COMMIT")
        return "accepted"
    except ProtocolError:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise
    except sqlite3.Error as exc:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise ProtocolError("ledger_unavailable", "Petra decision ledger transaction failed") from exc
    finally:
        connection.close()


def health_document(
    *,
    host_uuid: str,
    chitra_instance_id: str = "",
    state_generation: int = 0,
    ownership_ready: bool = False,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    current = utc_now() if generated_at is None else generated_at.astimezone(UTC)
    return {
        "schema": HEALTH_SCHEMA,
        "authority": "petra",
        "host_uuid": host_uuid,
        "generated_at": format_timestamp(current),
        "mode": MODE,
        "chitra": {
            "instance_id": chitra_instance_id,
            "state_generation": state_generation,
            "ownership_ready": ownership_ready,
        },
    }


def publish_health(
    path: Path,
    *,
    host_uuid: str,
    chitra_instance_id: str = "",
    state_generation: int = 0,
    ownership_ready: bool = False,
    generated_at: datetime | None = None,
) -> None:
    write_json_atomic(
        path,
        health_document(
            host_uuid=host_uuid,
            chitra_instance_id=chitra_instance_id,
            state_generation=state_generation,
            ownership_ready=ownership_ready,
            generated_at=generated_at,
        ),
    )


OwnershipResolver = Callable[[Mapping[str, object]], object]


def observation_handler(
    *,
    host_uuid: str,
    ledger_path: Path,
    health_path: Path,
    ownership_resolver: OwnershipResolver,
    clock: Callable[[], datetime] = utc_now,
) -> Callable[[object], object]:
    """Build the stateful Petra handler while keeping effects injectable."""

    def handle(payload: object) -> object:
        received_at = clock().astimezone(UTC)
        observation = validate_observation(payload, host_uuid=host_uuid, now=received_at)
        query = ownership_query_for(observation)
        try:
            ownership_response = ownership_resolver(query)
            committed_at = clock().astimezone(UTC)
            # Revalidate after the ownership round trip: an expired advisory or
            # a stale Chitra result must never be made durable.
            observation = validate_observation(payload, host_uuid=host_uuid, now=committed_at)
            fence = validate_ownership_fence(
                ownership_response,
                query=query,
                observation=observation,
                now=committed_at,
            )
            status = record_observation(
                ledger_path,
                observation,
                ownership_fence=fence.response,
                recorded_at=committed_at,
            )
        except ProtocolError:
            publish_health(health_path, host_uuid=host_uuid, generated_at=clock().astimezone(UTC))
            raise
        except OSError as exc:
            publish_health(health_path, host_uuid=host_uuid, generated_at=clock().astimezone(UTC))
            raise ProtocolError("ownership_unavailable", "Chitra ownership provider is unavailable") from exc
        publish_health(
            health_path,
            host_uuid=host_uuid,
            chitra_instance_id=fence.provider_instance_id,
            state_generation=fence.ownership_generation,
            ownership_ready=True,
            generated_at=committed_at,
        )
        return {
            "schema": ACK_SCHEMA,
            "event_id": observation["event_id"],
            "status": status,
            "generated_at": format_timestamp(committed_at),
            "mode": MODE,
        }

    return handle


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Petra observe-only authority service")
    parser.add_argument("--socket-path", type=Path, default=DEFAULT_SOCKET_PATH)
    parser.add_argument("--ownership-socket-path", type=Path, default=DEFAULT_OWNERSHIP_SOCKET_PATH)
    parser.add_argument("--ownership-peer-user", default="chitra")
    parser.add_argument("--ledger-path", type=Path, default=DEFAULT_LEDGER_PATH)
    parser.add_argument("--health-path", type=Path, default=DEFAULT_HEALTH_PATH)
    parser.add_argument("--host-uuid", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        ownership_peer_uid = pwd.getpwnam(args.ownership_peer_user).pw_uid
    except KeyError as exc:
        raise ValueError("configured Chitra ownership provider identity is unavailable") from exc

    def resolve(query: Mapping[str, object]) -> object:
        return request_json_line(
            args.ownership_socket_path,
            dict(query),
            timeout_seconds=args.timeout_seconds,
            expected_peer_uid=ownership_peer_uid,
        )

    publish_health(args.health_path, host_uuid=args.host_uuid)
    handler = observation_handler(
        host_uuid=args.host_uuid,
        ledger_path=args.ledger_path,
        health_path=args.health_path,
        ownership_resolver=resolve,
    )
    try:
        serve_unix_json_lines(args.socket_path, handler, timeout_seconds=args.timeout_seconds)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
