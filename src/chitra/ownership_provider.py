"""Fail-closed Chitra ownership authority over a bounded Unix socket.

The provider deliberately does not discover sessions.  It reads only the
canonical ``goals.json`` document and a separate, manager-written marker that
asserts that the exact document bytes are a complete snapshot.  Without a
fresh, digest-matching marker every answer is non-authoritative ``unknown``.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import pwd
import socket
import stat
import struct
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from chitra.goals import GOAL_STATUSES
from chitra.state_paths import state_dir as default_state_dir

QUERY_SCHEMA: Final = "chitra.ownership.query.v1"
RESULT_SCHEMA: Final = "chitra.ownership.result.v1"
ERROR_SCHEMA: Final = "chitra.protocol-error.v1"
GOALS_SCHEMA: Final = "chitra.goals.v1"
MANAGED_MARKER_SCHEMA: Final = "chitra.ownership-managed.v1"
PROVIDER_ID: Final = "chitra"
MAX_MESSAGE_BYTES: Final = 64 * 1024
DEFAULT_SOCKET_PATH = Path("/run/chitra-ownership/provider.sock")
DEFAULT_MARKER_NAME = "goals.managed.json"
DEFAULT_TIMEOUT_SECONDS = 2.0
DEFAULT_STATE_MAX_AGE_SECONDS = 30.0
DEFAULT_GENERATION_FENCE_PATH = Path("/var/lib/chitra-ownership/ownership-generation.json")
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_GENERATION_FENCE_BYTES = 8 * 1024
MAX_GOALS = 512
MAX_GOAL_HISTORY = 128
MAX_OPEN_ASKS = 128
MAX_GOAL_STRING_BYTES = 4096
MAX_IDENTIFIER_BYTES = 256
MAX_SESSION_REF_BYTES = 512
MAX_GENERATION = 2**63 - 1

_QUERY_FIELDS = frozenset(("schema", "request_id", "host_id", "boot_id", "session_ref"))
_MARKER_FIELDS = frozenset(
    (
        "schema",
        "goals_schema",
        "goals_sha256",
        "host_id",
        "boot_id",
        "generation",
        "complete",
        "manager_heartbeat_at",
    )
)
_GOAL_FIELDS = frozenset(
    (
        "session_ref",
        "goal",
        "done_when",
        "source",
        "status",
        "lane_id",
        "enrolled_done_when",
        "enrolled_at",
        "intent",
        "scope",
        "goal_version",
        "goal_history",
        "now",
        "last_verified",
        "created_at",
        "updated_at",
        "open_asks",
        "needs",
        "hold_reason",
        "resume_at",
    )
)
_GOAL_STRING_FIELDS = _GOAL_FIELDS - {"goal_version", "goal_history", "open_asks"}


class ProtocolError(ValueError):
    """A peer sent a malformed or unsupported protocol message."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class OwnershipLane:
    session_ref: str
    lane_id: str
    lane_generation: int


@dataclass(frozen=True, slots=True)
class ManagedState:
    """Validated state plus source metadata safe to expose to callers."""

    authoritative: bool
    reason: str
    generation: int
    complete: bool
    manager_heartbeat_at: str
    lanes: Mapping[str, OwnershipLane]

    @classmethod
    def unknown(
        cls,
        reason: str,
        *,
        generation: int = 0,
        manager_heartbeat_at: str = "",
    ) -> ManagedState:
        return cls(False, reason, generation, False, manager_heartbeat_at, {})


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty timestamp string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _nonempty_string(payload: Mapping[str, object], field: str, *, maximum: int = MAX_IDENTIFIER_BYTES) -> str:
    value = payload.get(field)
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum
        or value != value.strip()
        or any(ord(char) < 0x20 for char in value)
    ):
        raise ValueError(f"{field} must be a non-empty canonical string")
    return value


def canonical_session_parts(session_ref: str) -> tuple[str, str, str]:
    """Validate, but never synthesize, an exact ``host:lane:instance`` ref."""
    if (
        not isinstance(session_ref, str)
        or not session_ref
        or len(session_ref.encode("utf-8")) > MAX_SESSION_REF_BYTES
        or session_ref != session_ref.strip()
        or any(char.isspace() or ord(char) < 0x20 for char in session_ref)
    ):
        raise ValueError("session_ref is not canonical")
    parts = session_ref.split(":")
    if len(parts) != 3 or any(not part for part in parts):
        raise ValueError("session_ref must be exact host:lane:instance")
    return parts[0], parts[1], parts[2]


def managed_marker_for_state(
    goals_bytes: bytes,
    *,
    host_id: str,
    boot_id: str,
    generation: int,
    manager_heartbeat_at: datetime,
    complete: bool = True,
) -> dict[str, object]:
    """Build the explicit manager marker required for authoritative reads.

    This helper is intentionally pure: the manager remains responsible for
    atomically publishing the returned marker after it publishes goals.json.
    """
    if not host_id or not boot_id:
        raise ValueError("host_id and boot_id must be non-empty")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("generation must be a positive integer")
    return {
        "schema": MANAGED_MARKER_SCHEMA,
        "goals_schema": GOALS_SCHEMA,
        "goals_sha256": hashlib.sha256(goals_bytes).hexdigest(),
        "host_id": host_id,
        "boot_id": boot_id,
        "generation": generation,
        "complete": complete,
        "manager_heartbeat_at": format_timestamp(manager_heartbeat_at),
    }


def _load_json_bytes(raw: bytes, *, description: str) -> object:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{description} contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(_value: str) -> object:
        raise ValueError(f"{description} contains a non-finite JSON value")

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=reject_constant,
    )


def _validate_goals_document(raw: bytes, *, host_id: str) -> dict[str, OwnershipLane]:
    payload = _load_json_bytes(raw, description="goals state")
    if not isinstance(payload, dict) or set(payload) != {"schema", "updated_at", "goals"}:
        raise ValueError("goals state must contain exactly schema, updated_at, and goals")
    if payload["schema"] != GOALS_SCHEMA:
        raise ValueError("goals state schema is not chitra.goals.v1")
    parse_timestamp(payload["updated_at"], field="goals.updated_at")
    raw_goals = payload["goals"]
    if not isinstance(raw_goals, list) or len(raw_goals) > MAX_GOALS:
        raise ValueError("goals must be a list")

    lanes: dict[str, OwnershipLane] = {}
    lane_ids: set[str] = set()
    for raw_goal in raw_goals:
        if not isinstance(raw_goal, dict) or set(raw_goal) != _GOAL_FIELDS:
            raise ValueError("each managed goal must be a current canonical goal record")
        if any(
            not isinstance(raw_goal[field], str) or len(raw_goal[field].encode("utf-8")) > MAX_GOAL_STRING_BYTES
            for field in _GOAL_STRING_FIELDS
        ):
            raise ValueError("managed goal string fields must be strings")
        if raw_goal["status"] not in GOAL_STATUSES:
            raise ValueError("managed goal status is invalid")
        if any(not str(raw_goal[field]).strip() for field in ("goal", "done_when", "source")):
            raise ValueError("managed goal strategic fields must be non-empty")
        goal_version = raw_goal["goal_version"]
        if isinstance(goal_version, bool) or not isinstance(goal_version, int) or not 1 <= goal_version <= MAX_GENERATION:
            raise ValueError("managed goal goal_version must be positive")
        if (
            not isinstance(raw_goal["open_asks"], list)
            or len(raw_goal["open_asks"]) > MAX_OPEN_ASKS
            or not all(isinstance(item, str) and len(item.encode("utf-8")) <= MAX_GOAL_STRING_BYTES for item in raw_goal["open_asks"])
        ):
            raise ValueError("managed goal open_asks must be strings")
        history = raw_goal["goal_history"]
        if not isinstance(history, list) or len(history) > MAX_GOAL_HISTORY or not all(
            isinstance(item, dict)
            and len(item) <= 32
            and all(
                isinstance(key, str)
                and isinstance(value, str)
                and len(key.encode("utf-8")) <= MAX_IDENTIFIER_BYTES
                and len(value.encode("utf-8")) <= MAX_GOAL_STRING_BYTES
                for key, value in item.items()
            )
            for item in history
        ):
            raise ValueError("managed goal goal_history must contain string objects")
        session_ref = _nonempty_string(raw_goal, "session_ref", maximum=MAX_SESSION_REF_BYTES)
        record_host, record_lane, _ = canonical_session_parts(session_ref)
        lane_id = _nonempty_string(raw_goal, "lane_id", maximum=MAX_IDENTIFIER_BYTES)
        if record_host != host_id or lane_id != record_lane:
            raise ValueError("managed goal host or explicit lane_id does not match its session_ref")
        if session_ref in lanes or lane_id in lane_ids:
            raise ValueError("managed goals contain a duplicate session_ref or lane_id")
        lanes[session_ref] = OwnershipLane(
            session_ref=session_ref,
            lane_id=lane_id,
            lane_generation=goal_version,
        )
        lane_ids.add(lane_id)
    return lanes


class StateReadError(ValueError):
    """A bounded state read failed with a public fail-closed reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _read_bounded_regular_file(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
    expected_owner_uid: int | None,
) -> bytes:
    """Read one stable, bounded, non-symlink state file without following it."""

    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise StateReadError(f"{label}_missing") from exc
    except OSError as exc:
        raise StateReadError(f"{label}_unreadable") from exc
    if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) & 0o022:
        raise StateReadError(f"{label}_unsafe")
    if expected_owner_uid is not None and before.st_uid != expected_owner_uid:
        raise StateReadError(f"{label}_untrusted")
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise StateReadError(f"{label}_missing") from exc
    except OSError as exc:
        raise StateReadError(f"{label}_unreadable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_ino != before.st_ino
            or metadata.st_dev != before.st_dev
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise StateReadError(f"{label}_changed")
        if expected_owner_uid is not None and metadata.st_uid != expected_owner_uid:
            raise StateReadError(f"{label}_untrusted")
        if metadata.st_size <= 0:
            raise StateReadError(f"{label}_empty")
        if metadata.st_size > maximum_bytes:
            raise StateReadError(f"{label}_oversized")
        content = bytearray()
        while len(content) <= maximum_bytes:
            chunk = os.read(descriptor, min(64 * 1024, maximum_bytes + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) != metadata.st_size or len(content) > maximum_bytes:
            raise StateReadError(f"{label}_changed")
        return bytes(content)
    except OSError as exc:
        raise StateReadError(f"{label}_unreadable") from exc
    finally:
        os.close(descriptor)


def load_managed_state(
    *,
    goals_path: Path,
    marker_path: Path,
    host_id: str,
    boot_id: str,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_STATE_MAX_AGE_SECONDS,
    expected_owner_uid: int | None = None,
    generation_fence_path: Path | None = None,
) -> ManagedState:
    """Load one complete digest-bound snapshot, failing closed on every flaw."""

    try:
        goals_bytes = _read_bounded_regular_file(
            goals_path,
            label="state",
            maximum_bytes=MAX_STATE_BYTES,
            expected_owner_uid=expected_owner_uid,
        )
    except StateReadError as exc:
        return ManagedState.unknown(exc.reason)
    try:
        marker_raw = _read_bounded_regular_file(
            marker_path,
            label="managed_marker",
            maximum_bytes=MAX_STATE_BYTES,
            expected_owner_uid=expected_owner_uid,
        )
    except StateReadError as exc:
        return ManagedState.unknown(exc.reason)

    generation = 0
    heartbeat = ""
    try:
        marker = _load_json_bytes(marker_raw, description="managed marker")
        if not isinstance(marker, dict) or set(marker) != _MARKER_FIELDS:
            raise ValueError("managed marker fields are not canonical")
        generation_value = marker["generation"]
        if isinstance(generation_value, bool) or not isinstance(generation_value, int) or not 1 <= generation_value <= MAX_GENERATION:
            raise ValueError("managed marker generation must be positive")
        generation = generation_value
        heartbeat = _nonempty_string(marker, "manager_heartbeat_at", maximum=64)
        heartbeat_at = parse_timestamp(heartbeat, field="manager_heartbeat_at")
        if marker["schema"] != MANAGED_MARKER_SCHEMA or marker["goals_schema"] != GOALS_SCHEMA:
            raise ValueError("managed marker schema is not canonical")
        if marker["complete"] is not True:
            return ManagedState.unknown("state_partial", generation=generation, manager_heartbeat_at=heartbeat)
        if _nonempty_string(marker, "host_id") != host_id:
            return ManagedState.unknown("state_host_mismatch", generation=generation, manager_heartbeat_at=heartbeat)
        if _nonempty_string(marker, "boot_id") != boot_id:
            return ManagedState.unknown("state_boot_mismatch", generation=generation, manager_heartbeat_at=heartbeat)
        digest = marker["goals_sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or digest != hashlib.sha256(goals_bytes).hexdigest():
            return ManagedState.unknown("state_digest_mismatch", generation=generation, manager_heartbeat_at=heartbeat)
        current = utc_now() if now is None else now.astimezone(UTC)
        age = (current - heartbeat_at).total_seconds()
        if age < -5.0:
            return ManagedState.unknown("state_heartbeat_in_future", generation=generation, manager_heartbeat_at=heartbeat)
        if age > max_age_seconds:
            return ManagedState.unknown("state_stale", generation=generation, manager_heartbeat_at=heartbeat)
        lanes = _validate_goals_document(goals_bytes, host_id=host_id)
        if generation_fence_path is not None and not _enforce_generation_fence(
            generation_fence_path,
            host_id=host_id,
            boot_id=boot_id,
            generation=generation,
            goals_sha256=digest,
        ):
            return ManagedState.unknown("state_generation_rollback", generation=generation, manager_heartbeat_at=heartbeat)
    except (OverflowError, RecursionError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return ManagedState.unknown("state_malformed", generation=generation, manager_heartbeat_at=heartbeat)
    return ManagedState(True, "", generation, True, heartbeat, lanes)


_GENERATION_FENCE_FIELDS = frozenset(("schema", "host_id", "boot_id", "generation", "goals_sha256"))
GENERATION_FENCE_SCHEMA: Final = "chitra.ownership-generation-fence.v1"


def _enforce_generation_fence(
    path: Path,
    *,
    host_id: str,
    boot_id: str,
    generation: int,
    goals_sha256: str,
) -> bool:
    """Persist and enforce a per-boot monotonic managed-state generation.

    A current boot may never serve a lower generation, nor a different snapshot
    under the same generation.  A new boot starts a new fence because request
    boot identity is independently mandatory on every ownership query.
    """

    descriptor: int | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path.with_suffix(path.suffix + ".lock"), lock_flags, 0o600)
        os.chmod(path.with_suffix(path.suffix + ".lock"), 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        previous: dict[str, object] | None = None
        try:
            raw = _read_bounded_regular_file(
                path,
                label="generation_fence",
                maximum_bytes=MAX_GENERATION_FENCE_BYTES,
                expected_owner_uid=os.geteuid(),
            )
        except StateReadError as exc:
            if exc.reason != "generation_fence_missing":
                return False
        else:
            decoded = _load_json_bytes(raw, description="generation fence")
            if not isinstance(decoded, dict) or set(decoded) != _GENERATION_FENCE_FIELDS:
                return False
            previous = decoded
        if previous is not None:
            try:
                previous_host = _nonempty_string(previous, "host_id")
                previous_boot = _nonempty_string(previous, "boot_id")
                previous_generation = previous["generation"]
                previous_digest = previous["goals_sha256"]
            except (KeyError, ValueError):
                return False
            if (
                previous.get("schema") != GENERATION_FENCE_SCHEMA
                or isinstance(previous_generation, bool)
                or not isinstance(previous_generation, int)
                or not 1 <= previous_generation <= MAX_GENERATION
                or not isinstance(previous_digest, str)
                or len(previous_digest) != 64
                or previous_host != host_id
            ):
                return False
            if previous_boot == boot_id:
                if generation < previous_generation:
                    return False
                if generation == previous_generation and goals_sha256 != previous_digest:
                    return False
                if generation == previous_generation:
                    return True
        write_json_atomic(
            path,
            {
                "schema": GENERATION_FENCE_SCHEMA,
                "host_id": host_id,
                "boot_id": boot_id,
                "generation": generation,
                "goals_sha256": goals_sha256,
            },
            mode=0o600,
        )
        return True
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(descriptor)


def validate_query(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict) or set(payload) != _QUERY_FIELDS:
        raise ProtocolError("invalid_query_fields", "ownership query fields must match the v1 contract exactly")
    if payload.get("schema") != QUERY_SCHEMA:
        raise ProtocolError("unsupported_schema", "unsupported ownership query schema")
    try:
        values = {
            "request_id": _nonempty_string(payload, "request_id", maximum=MAX_IDENTIFIER_BYTES),
            "host_id": _nonempty_string(payload, "host_id", maximum=MAX_IDENTIFIER_BYTES),
            "boot_id": _nonempty_string(payload, "boot_id", maximum=MAX_IDENTIFIER_BYTES),
            "session_ref": _nonempty_string(payload, "session_ref", maximum=MAX_SESSION_REF_BYTES),
        }
        ref_host, _, _ = canonical_session_parts(values["session_ref"])
    except ValueError as exc:
        raise ProtocolError("invalid_query", str(exc)) from exc
    if ref_host != values["host_id"]:
        raise ProtocolError("invalid_query", "session_ref host must equal host_id")
    return values


def ownership_result(
    payload: object,
    *,
    provider_instance_id: str,
    goals_path: Path,
    marker_path: Path,
    expected_host_id: str,
    expected_boot_id: str,
    now: datetime | None = None,
    validity_seconds: float = 5.0,
    state_max_age_seconds: float = DEFAULT_STATE_MAX_AGE_SECONDS,
    expected_owner_uid: int | None = None,
    generation_fence_path: Path | None = None,
) -> dict[str, object]:
    query = validate_query(payload)
    if (
        isinstance(validity_seconds, bool)
        or not isinstance(validity_seconds, (int, float))
        or not 1.0 <= validity_seconds <= 30.0
    ):
        raise ValueError("validity_seconds must be between one and thirty seconds")
    state = load_managed_state(
        goals_path=goals_path,
        marker_path=marker_path,
        host_id=expected_host_id,
        boot_id=expected_boot_id,
        now=now,
        max_age_seconds=state_max_age_seconds,
        expected_owner_uid=expected_owner_uid,
        generation_fence_path=generation_fence_path,
    )
    current = utc_now() if now is None else now.astimezone(UTC)
    if query["host_id"] != expected_host_id or query["boot_id"] != expected_boot_id:
        state = ManagedState.unknown(
            "request_authority_mismatch",
            generation=state.generation,
            manager_heartbeat_at=state.manager_heartbeat_at,
        )

    result: dict[str, object] = {"session_ref": query["session_ref"], "status": "unknown"}
    if state.authoritative:
        lane = state.lanes.get(query["session_ref"])
        if lane is None:
            result["status"] = "unowned"
        else:
            result.update(
                status="owned",
                lane_id=lane.lane_id,
                lane_generation=lane.lane_generation,
            )
    else:
        result["reason"] = state.reason

    return {
        "schema": RESULT_SCHEMA,
        "request_id": query["request_id"],
        "host_id": query["host_id"],
        "boot_id": query["boot_id"],
        "provider_id": PROVIDER_ID,
        "provider_instance_id": provider_instance_id,
        "generated_at": format_timestamp(current),
        "valid_until": format_timestamp(current + timedelta(seconds=validity_seconds)),
        "authoritative": state.authoritative,
        "source": {
            "schema": GOALS_SCHEMA,
            "generation": state.generation,
            "complete": state.complete,
            "manager_heartbeat_at": state.manager_heartbeat_at,
        },
        "result": result,
    }


def read_json_line(connection: socket.socket, *, max_bytes: int = MAX_MESSAGE_BYTES) -> object:
    """Read exactly one newline-terminated JSON value, bounded through EOF."""
    chunks = bytearray()
    while True:
        chunk = connection.recv(min(8192, max_bytes + 1 - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > max_bytes:
            raise ProtocolError("message_too_large", "message exceeds 64 KiB")
    if not chunks.endswith(b"\n") or chunks.count(b"\n") != 1:
        raise ProtocolError("invalid_framing", "request must be exactly one JSON line")
    try:
        return _load_json_bytes(bytes(chunks[:-1]), description="request")
    except (OverflowError, RecursionError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProtocolError("invalid_json", "request is not strict UTF-8 JSON") from exc


def write_json_line(connection: socket.socket, payload: object, *, max_bytes: int = MAX_MESSAGE_BYTES) -> None:
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise ProtocolError("invalid_response", "response cannot be encoded as finite JSON") from exc
    if len(encoded) > max_bytes:
        raise ProtocolError("message_too_large", "response exceeds 64 KiB")
    connection.sendall(encoded)


def request_json_line(
    socket_path: Path,
    payload: object,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_MESSAGE_BYTES,
    expected_peer_uid: int | None = None,
) -> object:
    """Timeout-bounded client helper for both local authority protocols."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout_seconds)
        connection.connect(str(socket_path))
        if expected_peer_uid is not None:
            if not hasattr(socket, "SO_PEERCRED"):
                raise ProtocolError("peer_identity_unavailable", "local authority peer credentials are unavailable")
            credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, peer_uid, _gid = struct.unpack("3i", credentials)
            if peer_uid != expected_peer_uid:
                raise ProtocolError("peer_identity_mismatch", "local authority peer identity did not match")
        write_json_line(connection, payload, max_bytes=max_bytes)
        connection.shutdown(socket.SHUT_WR)
        return read_json_line(connection, max_bytes=max_bytes)


def serve_unix_json_lines(
    socket_path: Path,
    handler: Callable[[object], object],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    socket_mode: int = 0o660,
) -> None:
    """Serve one request per local connection until interrupted."""
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = socket_path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(existing.st_mode):
            raise RuntimeError(f"refusing to replace non-socket path: {socket_path}")
        current = socket_path.lstat()
        if current.st_dev != existing.st_dev or current.st_ino != existing.st_ino or not stat.S_ISSOCK(current.st_mode):
            raise RuntimeError(f"refusing to replace changed socket path: {socket_path}")
        socket_path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound_identity: tuple[int, int] | None = None
    try:
        listener.bind(str(socket_path))
        os.chmod(socket_path, socket_mode)
        bound = socket_path.lstat()
        if not stat.S_ISSOCK(bound.st_mode):
            raise RuntimeError(f"ownership socket is not a socket after bind: {socket_path}")
        bound_identity = (bound.st_dev, bound.st_ino)
        listener.listen(16)
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                continue
            with connection:
                connection.settimeout(timeout_seconds)
                try:
                    response = handler(read_json_line(connection))
                except ProtocolError as exc:
                    response = {"schema": ERROR_SCHEMA, "reason": exc.reason, "message": str(exc)}
                except (OSError, TimeoutError):
                    continue
                except Exception:
                    response = {
                        "schema": ERROR_SCHEMA,
                        "reason": "authority_failure",
                        "message": "authority request could not be evaluated",
                    }
                try:
                    write_json_line(connection, response)
                except (OSError, TimeoutError, ProtocolError):
                    continue
    finally:
        listener.close()
        try:
            current = socket_path.lstat()
            if (
                bound_identity is not None
                and stat.S_ISSOCK(current.st_mode)
                and (current.st_dev, current.st_ino) == bound_identity
            ):
                socket_path.unlink()
        except FileNotFoundError:
            pass


def write_json_atomic(path: Path, payload: object, *, mode: int = 0o640) -> None:
    """Durably replace a JSON document and fsync its containing directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as output:
            temporary_name = output.name
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_name is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name)


def _read_boot_id(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("boot ID file is empty")
    return value


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed Chitra ownership provider")
    parser.add_argument("--socket-path", type=Path, default=DEFAULT_SOCKET_PATH)
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--marker-path", type=Path)
    parser.add_argument("--generation-fence-path", type=Path, default=DEFAULT_GENERATION_FENCE_PATH)
    parser.add_argument("--state-owner-user", default="chitra")
    parser.add_argument("--host-id", required=True)
    parser.add_argument("--boot-id")
    parser.add_argument("--boot-id-file", type=Path, default=Path("/proc/sys/kernel/random/boot_id"))
    parser.add_argument("--instance-id", default="")
    parser.add_argument("--state-max-age-seconds", type=float, default=DEFAULT_STATE_MAX_AGE_SECONDS)
    parser.add_argument("--validity-seconds", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    boot_id = args.boot_id or _read_boot_id(args.boot_id_file)
    instance_id = args.instance_id or str(uuid.uuid4())
    goals_path = args.state_dir / "goals.json"
    marker_path = args.marker_path or args.state_dir / DEFAULT_MARKER_NAME
    try:
        state_owner_uid = pwd.getpwnam(args.state_owner_user).pw_uid
    except KeyError as exc:
        raise ValueError("configured Chitra state owner identity is unavailable") from exc

    def handle(payload: object) -> object:
        return ownership_result(
            payload,
            provider_instance_id=instance_id,
            goals_path=goals_path,
            marker_path=marker_path,
            expected_host_id=args.host_id,
            expected_boot_id=boot_id,
            validity_seconds=args.validity_seconds,
            state_max_age_seconds=args.state_max_age_seconds,
            expected_owner_uid=state_owner_uid,
            generation_fence_path=args.generation_fence_path,
        )

    try:
        serve_unix_json_lines(args.socket_path, handle, timeout_seconds=args.timeout_seconds)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
