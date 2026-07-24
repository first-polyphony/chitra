"""Tests for Petra's observe-only dark-launch authority."""

from __future__ import annotations

import json
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chitra import petra
from chitra.ownership_provider import ProtocolError, format_timestamp
from chitra.petra import health_document, observation_handler, publish_health, validate_observation

NOW = datetime(2026, 7, 15, 16, 0, tzinfo=UTC)
HOST = "host-a"
BOOT = "boot-a"
INSTANCE = "chitra-instance-a"
GENERATION = 23


def _observation(*, event_id: str = "event-1") -> dict[str, object]:
    return {
        "schema": "petra.pressure-observation.v1",
        "event_id": event_id,
        "host_id": HOST,
        "boot_id": BOOT,
        "session_ref": "host-a:lane-one:0.0",
        "observed_at": format_timestamp(NOW - timedelta(seconds=1)),
        "expires_at": format_timestamp(NOW + timedelta(seconds=30)),
        "ownership": {
            "query_id": "00000000-0000-4000-8000-000000000001",
            "provider_id": "chitra",
            "provider_instance_id": INSTANCE,
            "lane_id": "lane-one",
            "lane_generation": 1,
            "ownership_generation": GENERATION,
        },
        "evidence": {
            "ring0_policy_generation": 2,
            "resource": "memory",
            "evidence_digest": "sha256:" + "a" * 64,
        },
    }


def _ownership_response(
    query: dict[str, object],
    *,
    lane_id: str = "lane-one",
    generation: int = GENERATION,
    current: datetime = NOW,
) -> dict[str, object]:
    return {
        "schema": "chitra.ownership.result.v1",
        "request_id": query["request_id"],
        "host_id": query["host_id"],
        "boot_id": query["boot_id"],
        "provider_id": "chitra",
        "provider_instance_id": INSTANCE,
        "generated_at": format_timestamp(current),
        "valid_until": format_timestamp(current + timedelta(seconds=5)),
        "authoritative": True,
        "source": {
            "schema": "chitra.goals.v1",
            "generation": generation,
            "complete": True,
            "manager_heartbeat_at": format_timestamp(current),
        },
        "result": {
            "session_ref": query["session_ref"],
            "status": "owned",
            "lane_id": lane_id,
            "lane_generation": 1,
        },
    }


def test_event_dedupe_records_one_atomic_decision_and_outbox_entry(tmp_path: Path) -> None:
    ledger_path = tmp_path / "decision-outbox.sqlite3"
    health_path = tmp_path / "health.json"

    def resolve(query: dict[str, object]) -> object:
        return _ownership_response(query)

    handler = observation_handler(
        host_uuid=HOST,
        ledger_path=ledger_path,
        health_path=health_path,
        ownership_resolver=resolve,
        clock=lambda: NOW,
    )

    first = handler(_observation())
    second = handler(_observation())
    with sqlite3.connect(ledger_path) as connection:
        decisions = connection.execute(
            "SELECT mode, disposition, observation_json, ownership_fence_json FROM petra_observations"
        ).fetchall()
        outbox = connection.execute("SELECT schema, mode, disposition FROM petra_outbox").fetchall()

    assert first["status"] == "accepted"
    assert second["status"] == "duplicate"
    assert len(decisions) == 1
    assert len(outbox) == 1
    assert decisions[0][:2] == ("observe", "observed")
    assert outbox[0] == ("petra.observation-recorded.v1", "observe", "observed")
    assert json.loads(decisions[0][2])["event_id"] == "event-1"
    assert json.loads(decisions[0][3])["provider_instance_id"] == INSTANCE
    assert stat.S_IMODE(ledger_path.stat().st_mode) == 0o600


def test_conflicting_duplicate_is_rejected_without_creating_a_second_outbox_entry(tmp_path: Path) -> None:
    ledger_path = tmp_path / "decision-outbox.sqlite3"
    handler = observation_handler(
        host_uuid=HOST,
        ledger_path=ledger_path,
        health_path=tmp_path / "health.json",
        ownership_resolver=lambda query: _ownership_response(query),
        clock=lambda: NOW,
    )

    assert handler(_observation())["status"] == "accepted"
    conflicting = _observation()
    conflicting["evidence"] = {
        "ring0_policy_generation": 2,
        "resource": "memory",
        "evidence_digest": "sha256:" + "b" * 64,
    }
    with pytest.raises(ProtocolError) as exc_info:
        handler(conflicting)

    assert exc_info.value.reason == "conflicting_duplicate"
    with sqlite3.connect(ledger_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM petra_observations").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM petra_outbox").fetchone() == (1,)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda value: value.update(
                observed_at=format_timestamp(NOW - timedelta(seconds=20)),
                expires_at=format_timestamp(NOW - timedelta(seconds=1)),
            ),
            "expired_observation",
        ),
        (lambda value: value.update(operation="pause"), "invalid_observation_fields"),
        (lambda value: value["evidence"].update(signal="SIGSTOP"), "invalid_evidence"),
        (
            lambda value: value.update(
                observed_at=format_timestamp(NOW - timedelta(seconds=301)),
                expires_at=format_timestamp(NOW + timedelta(seconds=1)),
            ),
            "stale_observation",
        ),
    ],
)
def test_invalid_or_operational_advisory_is_rejected(mutate: object, reason: str) -> None:
    observation = _observation()
    mutate(observation)  # type: ignore[operator]
    with pytest.raises(ProtocolError) as exc_info:
        validate_observation(observation, host_uuid=HOST, now=NOW)
    assert exc_info.value.reason == reason


@pytest.mark.parametrize(("lane_id", "generation"), [("other-lane", GENERATION), ("lane-one", GENERATION + 1)])
def test_mismatched_ownership_lane_or_generation_is_rejected(
    tmp_path: Path,
    lane_id: str,
    generation: int,
) -> None:
    handler = observation_handler(
        host_uuid=HOST,
        ledger_path=tmp_path / "ledger.sqlite3",
        health_path=tmp_path / "health.json",
        ownership_resolver=lambda query: _ownership_response(query, lane_id=lane_id, generation=generation),
        clock=lambda: NOW,
    )

    with pytest.raises(ProtocolError, match="does not match"):
        handler(_observation())
    assert not (tmp_path / "ledger.sqlite3").exists()
    health = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    assert health["chitra"]["ownership_ready"] is False


def test_health_is_atomic_observe_mode_and_not_ready_by_default(tmp_path: Path) -> None:
    health_path = tmp_path / "run" / "health.json"
    publish_health(health_path, host_uuid=HOST, generated_at=NOW)
    health = json.loads(health_path.read_text(encoding="utf-8"))

    assert health == health_document(host_uuid=HOST, generated_at=NOW)
    assert health["schema"] == "petra.authority-health.v1"
    assert health["authority"] == "petra"
    assert health["mode"] == "observe"
    assert health["chitra"] == {"instance_id": "", "state_generation": 0, "ownership_ready": False}
    assert stat.S_IMODE(health_path.stat().st_mode) == 0o640
    assert not list(health_path.parent.glob(f".{health_path.name}.*"))


def test_valid_owned_observation_makes_health_ready(tmp_path: Path) -> None:
    health_path = tmp_path / "health.json"
    handler = observation_handler(
        host_uuid=HOST,
        ledger_path=tmp_path / "ledger.sqlite3",
        health_path=health_path,
        ownership_resolver=lambda query: _ownership_response(query),
        clock=lambda: NOW,
    )

    handler(_observation())

    health = json.loads(health_path.read_text(encoding="utf-8"))
    assert health["mode"] == "observe"
    assert health["chitra"] == {
        "instance_id": INSTANCE,
        "state_generation": GENERATION,
        "ownership_ready": True,
    }


def test_observation_expiring_during_ownership_round_trip_is_not_recorded(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    completed_at = NOW + timedelta(seconds=31)
    clock_values = iter((NOW, completed_at, completed_at))
    handler = observation_handler(
        host_uuid=HOST,
        ledger_path=ledger_path,
        health_path=tmp_path / "health.json",
        ownership_resolver=lambda query: _ownership_response(query, current=completed_at),
        clock=lambda: next(clock_values),
    )

    with pytest.raises(ProtocolError) as exc_info:
        handler(_observation())

    assert exc_info.value.reason == "expired_observation"
    assert not ledger_path.exists()


def test_ledger_capacity_fails_closed_without_pruning_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    monkeypatch.setattr(petra, "MAX_LEDGER_EVENTS", 1)
    handler = observation_handler(
        host_uuid=HOST,
        ledger_path=ledger_path,
        health_path=tmp_path / "health.json",
        ownership_resolver=lambda query: _ownership_response(query),
        clock=lambda: NOW,
    )

    assert handler(_observation(event_id="event-1"))["status"] == "accepted"
    with pytest.raises(ProtocolError) as exc_info:
        handler(_observation(event_id="event-2"))

    assert exc_info.value.reason == "ledger_capacity"
    with sqlite3.connect(ledger_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM petra_observations").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM petra_outbox").fetchone() == (1,)
