"""Tests for the fail-closed Chitra ownership provider."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chitra import ownership_provider as provider
from chitra.goals import GoalRecord
from chitra.ownership_provider import (
    MAX_MESSAGE_BYTES,
    ProtocolError,
    load_managed_state,
    managed_marker_for_state,
    ownership_result,
    read_json_line,
)

NOW = datetime(2026, 7, 15, 16, 0, tzinfo=UTC)
HOST = "host-a"
BOOT = "boot-a"


def _goal(session_ref: str, lane_id: str) -> dict[str, object]:
    return GoalRecord(
        session_ref=session_ref,
        lane_id=lane_id,
        goal="Implement the complete bounded ownership authority contract safely",
        done_when="All focused ownership authority tests pass cleanly",
        source="task-file:test",
        status="working",
        enrolled_done_when="All focused ownership authority tests pass cleanly",
        enrolled_at="2026-07-15T15:00:00Z",
        created_at="2026-07-15T15:00:00Z",
        updated_at="2026-07-15T15:00:00Z",
    ).to_dict()


def _write_state(
    root: Path,
    *,
    complete: bool = True,
    marker_host: str = HOST,
    heartbeat: datetime = NOW,
    generation: int = 17,
) -> tuple[Path, Path]:
    goals_path = root / "goals.json"
    marker_path = root / "goals.managed.json"
    document = {
        "schema": "chitra.goals.v1",
        "updated_at": "2026-07-15T16:00:00Z",
        "goals": [_goal("host-a:lane-one:0.0", "lane-one")],
    }
    raw = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    goals_path.write_bytes(raw)
    marker = managed_marker_for_state(
        raw,
        host_id=marker_host,
        boot_id=BOOT,
        generation=generation,
        manager_heartbeat_at=heartbeat,
        complete=complete,
    )
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    return goals_path, marker_path


def _query(session_ref: str) -> dict[str, str]:
    return {
        "schema": "chitra.ownership.query.v1",
        "request_id": "request-1",
        "host_id": HOST,
        "boot_id": BOOT,
        "session_ref": session_ref,
    }


def test_missing_state_returns_non_authoritative_unknown(tmp_path: Path) -> None:
    response = ownership_result(
        _query("host-a:lane-one:0.0"),
        provider_instance_id="provider-instance",
        goals_path=tmp_path / "goals.json",
        marker_path=tmp_path / "goals.managed.json",
        expected_host_id=HOST,
        expected_boot_id=BOOT,
        now=NOW,
    )

    assert response["authoritative"] is False
    assert response["source"] == {
        "schema": "chitra.goals.v1",
        "generation": 0,
        "complete": False,
        "manager_heartbeat_at": "",
    }
    assert response["result"] == {
        "session_ref": "host-a:lane-one:0.0",
        "status": "unknown",
        "reason": "state_missing",
    }


def test_valid_complete_state_returns_exact_owned_and_unowned(tmp_path: Path) -> None:
    goals_path, marker_path = _write_state(tmp_path)
    common = {
        "provider_instance_id": "provider-instance",
        "goals_path": goals_path,
        "marker_path": marker_path,
        "expected_host_id": HOST,
        "expected_boot_id": BOOT,
        "now": NOW,
    }

    owned = ownership_result(_query("host-a:lane-one:0.0"), **common)
    unowned = ownership_result(_query("host-a:absent:0.0"), **common)

    assert owned["authoritative"] is True
    assert owned["result"] == {
        "session_ref": "host-a:lane-one:0.0",
        "status": "owned",
        "lane_id": "lane-one",
        "lane_generation": 1,
    }
    assert unowned["authoritative"] is True
    assert unowned["result"] == {"session_ref": "host-a:absent:0.0", "status": "unowned"}


@pytest.mark.parametrize(
    ("state_kwargs", "reason"),
    [
        ({"heartbeat": NOW - timedelta(minutes=2)}, "state_stale"),
        ({"complete": False}, "state_partial"),
        ({"marker_host": "other-host"}, "state_host_mismatch"),
    ],
)
def test_stale_partial_and_host_mismatched_state_fail_unknown(
    tmp_path: Path,
    state_kwargs: dict[str, object],
    reason: str,
) -> None:
    goals_path, marker_path = _write_state(tmp_path, **state_kwargs)  # type: ignore[arg-type]

    response = ownership_result(
        _query("host-a:lane-one:0.0"),
        provider_instance_id="provider-instance",
        goals_path=goals_path,
        marker_path=marker_path,
        expected_host_id=HOST,
        expected_boot_id=BOOT,
        now=NOW,
    )

    assert response["authoritative"] is False
    assert response["result"] == {
        "session_ref": "host-a:lane-one:0.0",
        "status": "unknown",
        "reason": reason,
    }


def test_state_is_not_complete_without_current_digest_bound_marker(tmp_path: Path) -> None:
    goals_path, marker_path = _write_state(tmp_path)
    marker_path.unlink()
    missing = load_managed_state(
        goals_path=goals_path,
        marker_path=marker_path,
        host_id=HOST,
        boot_id=BOOT,
        now=NOW,
    )
    _, marker_path = _write_state(tmp_path)
    goals_path.write_text("{}\n", encoding="utf-8")
    changed = load_managed_state(
        goals_path=goals_path,
        marker_path=marker_path,
        host_id=HOST,
        boot_id=BOOT,
        now=NOW,
    )

    assert missing.authoritative is False and missing.reason == "managed_marker_missing"
    assert changed.authoritative is False and changed.reason == "state_digest_mismatch"


def test_state_files_are_bounded_regular_and_owned_when_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    goals_path, marker_path = _write_state(tmp_path)
    monkeypatch.setattr(provider, "MAX_STATE_BYTES", 100)
    oversized = load_managed_state(
        goals_path=goals_path,
        marker_path=marker_path,
        host_id=HOST,
        boot_id=BOOT,
        now=NOW,
    )
    assert oversized.authoritative is False and oversized.reason == "state_oversized"

    monkeypatch.setattr(provider, "MAX_STATE_BYTES", 2 * 1024 * 1024)
    goals_path.unlink()
    goals_path.symlink_to(marker_path)
    unsafe = load_managed_state(
        goals_path=goals_path,
        marker_path=marker_path,
        host_id=HOST,
        boot_id=BOOT,
        now=NOW,
    )
    assert unsafe.authoritative is False and unsafe.reason == "state_unsafe"

    goals_path.unlink()
    goals_path, marker_path = _write_state(tmp_path)
    wrong_owner = load_managed_state(
        goals_path=goals_path,
        marker_path=marker_path,
        host_id=HOST,
        boot_id=BOOT,
        now=NOW,
        expected_owner_uid=os.geteuid() + 1,
    )
    assert wrong_owner.authoritative is False and wrong_owner.reason == "state_untrusted"


def test_generation_fence_rejects_rollback_or_same_generation_rewrite(tmp_path: Path) -> None:
    goals_path, marker_path = _write_state(tmp_path, generation=17)
    fence_path = tmp_path / "ownership-generation.json"
    first = load_managed_state(
        goals_path=goals_path,
        marker_path=marker_path,
        host_id=HOST,
        boot_id=BOOT,
        now=NOW,
        generation_fence_path=fence_path,
    )
    assert first.authoritative is True

    goals_path, marker_path = _write_state(tmp_path, generation=16)
    rollback = load_managed_state(
        goals_path=goals_path,
        marker_path=marker_path,
        host_id=HOST,
        boot_id=BOOT,
        now=NOW,
        generation_fence_path=fence_path,
    )
    assert rollback.authoritative is False and rollback.reason == "state_generation_rollback"

    document = json.loads(goals_path.read_text(encoding="utf-8"))
    document["goals"][0]["goal"] = "A different record must not reuse a fenced generation"
    changed_raw = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    goals_path.write_bytes(changed_raw)
    marker = managed_marker_for_state(
        changed_raw,
        host_id=HOST,
        boot_id=BOOT,
        generation=17,
        manager_heartbeat_at=NOW,
    )
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    rewritten = load_managed_state(
        goals_path=goals_path,
        marker_path=marker_path,
        host_id=HOST,
        boot_id=BOOT,
        now=NOW,
        generation_fence_path=fence_path,
    )
    assert rewritten.authoritative is False and rewritten.reason == "state_generation_rollback"


def test_query_requires_exact_fields_and_canonical_session_ref(tmp_path: Path) -> None:
    invalid = _query("host-a:lane-one") | {"operation": "pause"}
    with pytest.raises(ProtocolError, match="fields must match"):
        ownership_result(
            invalid,
            provider_instance_id="provider-instance",
            goals_path=tmp_path / "goals.json",
            marker_path=tmp_path / "goals.managed.json",
            expected_host_id=HOST,
            expected_boot_id=BOOT,
            now=NOW,
        )


def test_json_line_reader_rejects_more_than_64_kib() -> None:
    class _ChunkedConnection:
        def __init__(self, data: bytes) -> None:
            self.data = data

        def recv(self, size: int) -> bytes:
            chunk, self.data = self.data[:size], self.data[size:]
            return chunk

    connection = _ChunkedConnection(b'"' + b"x" * MAX_MESSAGE_BYTES + b'"\n')
    with pytest.raises(ProtocolError, match="64 KiB"):
        read_json_line(connection)  # type: ignore[arg-type]
