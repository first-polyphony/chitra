"""Tests for deterministic usage-snapshot reading and policy evaluation."""

from __future__ import annotations

import json
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chitra.policy_config import UsagePolicy
from chitra.usage import (
    CodexSnapshotError,
    UsageSnapshot,
    UsageWindow,
    codex_snapshot,
    evaluate,
    evaluate_grouped,
    main,
    read_snapshots,
)

_DEFAULT_FIVE_HOUR = UsageWindow(10, 1_700_000_100)
_DEFAULT_SEVEN_DAY = UsageWindow(10, 1_700_000_200)


def _snapshot(
    *,
    five_hour: UsageWindow | None = _DEFAULT_FIVE_HOUR,
    seven_day: UsageWindow | None = _DEFAULT_SEVEN_DAY,
    ts: str = "2026-07-10T12:00:00+00:00",
    session_id: str = "lane-1",
    account: str = "",
) -> UsageSnapshot:
    return UsageSnapshot(
        kind="claude",
        ts=ts,
        session_id=session_id,
        tmux_session="fleet-1",
        five_hour=five_hour,
        seven_day=seven_day,
        account=account,
    )


def test_snapshot_from_dict_is_strict_and_round_trips() -> None:
    snapshot = _snapshot(account="account@example.com")
    assert UsageSnapshot.from_dict(snapshot.to_dict()) == snapshot
    old_payload = snapshot.to_dict()
    del old_payload["account"]
    assert UsageSnapshot.from_dict(old_payload).account == ""
    for payload in (
        {},
        {**snapshot.to_dict(), "ts": "2026-07-10T12:00:00+01:00"},
        {**snapshot.to_dict(), "kind": "other"},
        {**snapshot.to_dict(), "five_hour": {"pct": 101, "resets_at": 1}},
        {**snapshot.to_dict(), "five_hour": {"pct": True, "resets_at": 1}},
        {**snapshot.to_dict(), "account": 1},
    ):
        with pytest.raises(ValueError):
            UsageSnapshot.from_dict(payload)


def test_read_snapshots_marks_exact_staleness_boundary_and_names_malformed_file(tmp_path: Path) -> None:
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)
    fresh = _snapshot(ts=(now - timedelta(seconds=1200)).isoformat(), session_id="fresh")
    stale = _snapshot(ts=(now - timedelta(seconds=1201)).isoformat(), session_id="stale")
    (tmp_path / "fresh.json").write_text(json.dumps(fresh.to_dict()), encoding="utf-8")
    (tmp_path / "stale.json").write_text(json.dumps(stale.to_dict()), encoding="utf-8")
    assert read_snapshots(tmp_path, now=now) == [(fresh, True), (stale, False)]
    assert read_snapshots(tmp_path / "missing", now=now) == []
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="bad.json"):
        read_snapshots(tmp_path, now=now)


def test_evaluate_covers_policy_branches_ties_margins_and_null_windows() -> None:
    assert evaluate(_snapshot()).level == "ok"
    assert evaluate(_snapshot(five_hour=UsageWindow(80, 1), seven_day=None)).level == "approaching"
    assert evaluate(_snapshot(five_hour=UsageWindow(92, 11), seven_day=None)).resume_at_epoch == 11
    tied = evaluate(_snapshot(five_hour=UsageWindow(94, 11), seven_day=UsageWindow(97, 22)))
    assert (tied.level, tied.binding_window, tied.resume_at_epoch) == ("pause", "7d", 22)
    margin = evaluate(_snapshot(five_hour=UsageWindow(95, 11), seven_day=UsageWindow(95, 22)))
    assert margin.binding_window == "5h"
    assert evaluate(_snapshot(five_hour=None, seven_day=None)).level == "ok"


class _FakeCodexStdin:
    def __init__(self, process: _FakeCodexProcess) -> None:
        self.process = process
        self.closed = False

    def write(self, line: str) -> int:
        self.process.receive(json.loads(line))
        return len(line)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeCodexStdout:
    def __init__(self, process: _FakeCodexProcess) -> None:
        self.process = process
        self.closed = False

    def fileno(self) -> int:
        raise OSError("fake pipe has no file descriptor")

    def readline(self) -> str:
        if not self.process.responses:
            return ""
        line = self.process.responses.pop(0)
        payload = json.loads(line)
        if payload.get("id") == 1:
            self.process.read_response_ids.append(1)
        return line + "\n"

    def close(self) -> None:
        self.closed = True


class _FakeCodexProcess:
    def __init__(self, response_result: object, *, respond: bool = True, returncode: int | None = None) -> None:
        self.response_result = response_result
        self.respond = respond
        self.returncode = returncode
        self.responses = [json.dumps({"method": "notification"}), json.dumps({"id": 1, "result": {}})]
        self.requests: list[dict[str, object]] = []
        self.read_response_ids: list[int] = []
        self.initialized_sent = False
        self.terminated = False
        self.killed = False
        self.stdin = _FakeCodexStdin(self)
        self.stdout = _FakeCodexStdout(self)
        self.stderr = None

    def receive(self, payload: dict[str, object]) -> None:
        self.requests.append(payload)
        if payload.get("id") == 1:
            return
        if payload == {"method": "initialized"}:
            assert self.read_response_ids == [1]
            self.initialized_sent = True
            return
        assert payload == {"id": 2, "method": "account/rateLimits/read", "params": None}
        assert self.initialized_sent
        if self.respond:
            self.responses.append(json.dumps({"id": 2, "result": self.response_result}))

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: int) -> int:
        return 0 if self.returncode is None else self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _process_factory(process: _FakeCodexProcess):
    def factory(command: object) -> _FakeCodexProcess:
        assert command == ["codex", "app-server", "--stdio"]
        return process

    return factory


def _jwt(claims: dict[str, object]) -> str:
    payload = urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _write_auth(path: Path, claims: dict[str, object]) -> None:
    path.write_text(json.dumps({"tokens": {"id_token": _jwt(claims)}}), encoding="utf-8")


def test_codex_snapshot_sequences_exchange_and_maps_payload_variants(tmp_path: Path) -> None:
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)
    auth_path = tmp_path / "auth.json"
    _write_auth(auth_path, {"email": "first@example.com"})

    snake_process = _FakeCodexProcess({"rateLimits": {"primary": {"used_percent": 81, "resets_at": 99}, "secondary": None}})
    first = codex_snapshot(now=now, process_factory=_process_factory(snake_process), auth_path=auth_path)
    assert first.five_hour == UsageWindow(81.0, 99)
    assert first.seven_day is None
    assert first.account == "first@example.com"
    assert snake_process.requests == [
        {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "chitra-usage", "version": "1"}}},
        {"method": "initialized"},
        {"id": 2, "method": "account/rateLimits/read", "params": None},
    ]

    camel_process = _FakeCodexProcess(
        {
            "rateLimits": {
                "primary": {"usedPercent": 71, "resetsInSeconds": 60},
                "secondary": {"usedPercent": 91, "resetsAt": 123},
            }
        }
    )
    _write_auth(auth_path, {"https://api.openai.com/profile": {"email": "second@example.com"}})
    second = codex_snapshot(now=now, process_factory=_process_factory(camel_process), auth_path=auth_path)
    assert second.five_hour == UsageWindow(71.0, int(now.timestamp()) + 60)
    assert second.seven_day == UsageWindow(91.0, 123)
    assert second.account == "second@example.com"

    assert camel_process.stdin.closed
    assert camel_process.stdout.closed


def test_codex_snapshot_missing_auth_file_uses_empty_account(tmp_path: Path) -> None:
    process = _FakeCodexProcess({"rateLimits": {"primary": {"used_percent": 10, "resets_at": 99}, "secondary": None}})
    assert codex_snapshot(process_factory=_process_factory(process), auth_path=tmp_path / "missing.json").account == ""


def test_codex_snapshot_errors_for_deadline_missing_binary_missing_result_and_nonzero_exit(tmp_path: Path) -> None:
    auth_path = tmp_path / "missing.json"
    deadline_process = _FakeCodexProcess(None, respond=False)
    clock_values = iter((0.0, 0.0, 0.0, 15.0))
    with pytest.raises(CodexSnapshotError, match="within 15 seconds"):
        codex_snapshot(process_factory=_process_factory(deadline_process), clock=lambda: next(clock_values), auth_path=auth_path)

    def missing_factory(command: object) -> _FakeCodexProcess:
        raise FileNotFoundError

    with pytest.raises(CodexSnapshotError, match="not found"):
        codex_snapshot(process_factory=missing_factory, auth_path=auth_path)

    missing_result_process = _FakeCodexProcess(None)
    with pytest.raises(CodexSnapshotError, match="returned no account/rateLimits/read response"):
        codex_snapshot(process_factory=_process_factory(missing_result_process), auth_path=auth_path)

    nonzero_process = _FakeCodexProcess(None, returncode=7)
    with pytest.raises(CodexSnapshotError, match=r"failed \(7\)"):
        codex_snapshot(process_factory=_process_factory(nonzero_process), auth_path=auth_path)


def test_evaluate_grouped_attributes_fresh_account_verdicts_to_stale_siblings() -> None:
    hot_one = _snapshot(session_id="hot-one", account="hot@example.com", five_hour=UsageWindow(93, 10))
    hot_two = _snapshot(session_id="hot-two", account="hot@example.com", five_hour=UsageWindow(93, 10))
    both_fresh = evaluate_grouped([(hot_one, True), (hot_two, True)], policy=UsagePolicy())
    assert [(item.level, item.self_fresh, item.account_attributed) for item in both_fresh] == [
        ("pause", True, False),
        ("pause", True, False),
    ]

    stale = _snapshot(session_id="stale", account="hot@example.com", five_hour=UsageWindow(1, 10))
    propagated = evaluate_grouped([(hot_one, True), (stale, False)], policy=UsagePolicy())
    assert [(item.level, item.self_fresh, item.account_attributed) for item in propagated] == [
        ("pause", True, False),
        ("pause", False, True),
    ]


def test_evaluate_grouped_keeps_accounts_separate_and_unknown_without_fresh_readings() -> None:
    stale = _snapshot(session_id="stale", account="stale@example.com", five_hour=UsageWindow(99, 10))
    hot = _snapshot(session_id="hot", account="hot@example.com", five_hour=UsageWindow(93, 10))
    okay = _snapshot(session_id="okay", account="okay@example.com", five_hour=UsageWindow(10, 10))
    empty = _snapshot(session_id="empty", account="", five_hour=UsageWindow(10, 10))
    grouped = evaluate_grouped([(stale, False), (hot, True), (okay, True), (empty, False)], policy=UsagePolicy())
    assert [(item.account, item.level) for item in grouped] == [
        ("", "unknown"),
        ("hot@example.com", "pause"),
        ("okay@example.com", "ok"),
        ("stale@example.com", "unknown"),
    ]


def test_evaluate_grouped_fails_closed_on_unknown_account_never_merging_unrelated_unknowns() -> None:
    """Regression for SOL finding #6: two unrelated sessions that both have
    an unknown (blank) account identity must never be merged into one
    account group. Before this fix, ``evaluate_grouped`` grouped by the raw
    (possibly empty) account string, so one hot fresh unknown-identity
    session could pause every unrelated unknown-identity sibling."""
    hot_unknown = _snapshot(session_id="hot-unknown", account="", five_hour=UsageWindow(99, 10))
    other_unknown = _snapshot(session_id="other-unknown", account="", five_hour=UsageWindow(5, 10))
    grouped = evaluate_grouped([(hot_unknown, True), (other_unknown, True)], policy=UsagePolicy())
    by_session = {item.session_id: item for item in grouped}

    assert by_session["hot-unknown"].level == "pause"
    assert by_session["other-unknown"].level == "ok"  # must never inherit the unrelated session's pause verdict
    assert by_session["hot-unknown"].account == ""
    assert by_session["other-unknown"].account == ""


def test_evaluate_grouped_still_shares_a_verdict_across_the_same_real_account() -> None:
    """The fail-closed isolation is specific to the unknown (blank) account
    -- two sessions sharing a REAL, known account identity still correctly
    share one account-level verdict, exactly as before."""
    hot = _snapshot(session_id="hot-real", account="real@example.com", five_hour=UsageWindow(99, 10))
    sibling = _snapshot(session_id="sibling-real", account="real@example.com", five_hour=UsageWindow(5, 10))
    grouped = evaluate_grouped([(hot, True), (sibling, True)], policy=UsagePolicy())
    assert {item.level for item in grouped} == {"pause"}


def test_evaluate_grouped_propagates_approaching_verdict() -> None:
    fresh = _snapshot(session_id="fresh", account="shared@example.com", five_hour=UsageWindow(80, 10))
    stale = _snapshot(session_id="stale", account="shared@example.com", five_hour=UsageWindow(1, 10))
    grouped = evaluate_grouped([(fresh, True), (stale, False)], policy=UsagePolicy())
    assert [(item.level, item.account_attributed) for item in grouped] == [("approaching", False), ("approaching", True)]


def test_usage_cli_evaluate_policy_and_flag_precedence(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    current = datetime.now(UTC)
    fresh = _snapshot(five_hour=UsageWindow(75, 1_700_000_100), ts=current.isoformat(), session_id="fresh", account="shared@example.com")
    stale = _snapshot(
        five_hour=UsageWindow(99, 1_700_000_100),
        ts=(current - timedelta(hours=1)).isoformat(),
        session_id="stale",
        account="shared@example.com",
    )
    (tmp_path / "fresh.json").write_text(json.dumps(fresh.to_dict()), encoding="utf-8")
    (tmp_path / "stale.json").write_text(json.dumps(stale.to_dict()), encoding="utf-8")
    policy = tmp_path / "policy.yaml"
    policy.write_text("usage:\n  pause_5h_pct: 80\n  warn_5h_pct: 60\n", encoding="utf-8")

    assert main(["evaluate", "--dir", str(tmp_path), "--policy-config", str(policy)]) == 0
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[0]["level"] == "approaching"
    assert output[1] == {
        "session_id": "stale",
        "tmux_session": "fleet-1",
        "kind": "claude",
        "account": "shared@example.com",
        "level": "approaching",
        "binding_window": "5h",
        "resume_at_epoch": 0,
        "resume_at_iso": "",
        "self_fresh": False,
        "account_attributed": True,
    }

    assert main(["evaluate", "--dir", str(tmp_path), "--policy-config", str(policy), "--pause-5h", "70"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[0])["level"] == "pause"
    assert main(["policy", "--policy-config", str(policy)]) == 0
    assert json.loads(capsys.readouterr().out)["max_running"] is None
