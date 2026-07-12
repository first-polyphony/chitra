"""Tests for chitra.account_registry: freshness-bounded lane -> account
identity tracking (see docs/SOL-ADVERSARIAL-REVIEW finding #6)."""

from __future__ import annotations

import multiprocessing
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from chitra.account_registry import RegistryEntry, get_entry, load_registry, registry_path, update_registry
from chitra.usage import AccountedVerdict

ISO = "2026-07-12T00:00:00+00:00"


def _now(minutes: float = 0) -> datetime:
    return datetime.fromisoformat(ISO) + timedelta(minutes=minutes)


def _verdict(*, tmux_session: str, account: str, session_id: str = "s1") -> AccountedVerdict:
    return AccountedVerdict(
        session_id=session_id,
        tmux_session=tmux_session,
        kind="claude",
        account=account,
        level="ok",
        binding_window="",
        resume_at_epoch=0,
        self_fresh=True,
        account_attributed=False,
    )


def test_update_registry_records_a_new_entry(tmp_path: Path) -> None:
    update = update_registry(tmp_path, [_verdict(tmux_session="lane1", account="a@x.com")], now=_now())
    assert update.account_changed == []
    assert update.disappeared == []
    entry = get_entry(tmp_path, "lane1")
    assert entry is not None
    assert entry.account == "a@x.com"


def test_update_registry_skips_the_synthetic_codex_probe(tmp_path: Path) -> None:
    update_registry(tmp_path, [_verdict(tmux_session="", account="a@x.com", session_id="codex-account")], now=_now())
    assert load_registry(tmp_path) == []


def test_update_registry_detects_account_change(tmp_path: Path) -> None:
    update_registry(tmp_path, [_verdict(tmux_session="lane1", account="old@x.com")], now=_now())
    update = update_registry(tmp_path, [_verdict(tmux_session="lane1", account="new@x.com")], now=_now(minutes=1))
    assert update.account_changed == [("lane1", "old@x.com", "new@x.com")]
    assert get_entry(tmp_path, "lane1").account == "new@x.com"


def test_update_registry_no_change_reported_for_the_same_account(tmp_path: Path) -> None:
    update_registry(tmp_path, [_verdict(tmux_session="lane1", account="a@x.com")], now=_now())
    update = update_registry(tmp_path, [_verdict(tmux_session="lane1", account="a@x.com")], now=_now(minutes=1))
    assert update.account_changed == []


def test_disappeared_lane_is_retained_and_reported_within_the_freshness_window(tmp_path: Path) -> None:
    update_registry(tmp_path, [_verdict(tmux_session="lane1", account="a@x.com")], now=_now())
    update = update_registry(tmp_path, [], now=_now(minutes=1), freshness_seconds=3600)
    assert [entry.tmux_session for entry in update.disappeared] == ["lane1"]
    # Still retained (not silently dropped) while within the freshness window.
    assert get_entry(tmp_path, "lane1") is not None


def test_disappeared_lane_is_pruned_once_stale_beyond_the_freshness_window(tmp_path: Path) -> None:
    update_registry(tmp_path, [_verdict(tmux_session="lane1", account="a@x.com")], now=_now())
    update_registry(tmp_path, [], now=_now(minutes=30), freshness_seconds=3600)  # still within the 60-minute window
    assert get_entry(tmp_path, "lane1") is not None
    final = update_registry(tmp_path, [], now=_now(minutes=125), freshness_seconds=3600)  # now stale
    assert final.disappeared == []  # too stale to report -- silently pruned
    assert get_entry(tmp_path, "lane1") is None


def test_from_dict_round_trips_and_rejects_malformed_payloads() -> None:
    entry = RegistryEntry(tmux_session="lane1", session_id="s1", kind="claude", account="a@x.com", updated_at=ISO)
    assert RegistryEntry.from_dict(entry.to_dict()) == entry
    with pytest.raises(ValueError):
        RegistryEntry.from_dict({"tmux_session": "lane1"})
    with pytest.raises(ValueError):
        RegistryEntry.from_dict("not-a-dict")


def test_load_registry_rejects_a_document_with_the_wrong_schema(tmp_path: Path) -> None:
    registry_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    registry_path(tmp_path).write_text('{"schema": "wrong", "entries": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="chitra.account_registry.v1"):
        load_registry(tmp_path)


def test_load_registry_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_registry(tmp_path) == []


def test_update_registry_rejects_naive_datetime(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        update_registry(tmp_path, [], now=datetime(2026, 7, 12))  # noqa: DTZ001 -- deliberately naive, testing the guard


def _mp_update(root_str: str, tmux_session: str, account: str) -> None:
    update_registry(Path(root_str), [_verdict(tmux_session=tmux_session, account=account)], now=_now())


def test_concurrent_writers_updating_different_lanes_do_not_lose_each_other(tmp_path: Path) -> None:
    """Same lost-update class as chitra.goals (finding #9's pattern) --
    account_registry uses the identical flock-serialized read-modify-write,
    so N concurrent lanes must all survive."""
    ctx = multiprocessing.get_context("fork")
    lanes = [(f"lane-{i}", f"acct-{i}@x.com") for i in range(15)]
    procs = [ctx.Process(target=_mp_update, args=(str(tmp_path), tmux, account)) for tmux, account in lanes]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    stored = {entry.tmux_session: entry.account for entry in load_registry(tmp_path)}
    assert stored == dict(lanes)
