"""Tests for chitra.rate_limit_state: the durable transaction outbox behind
chitra.rate_limit_guard (see docs/SOL-ADVERSARIAL-REVIEW finding #2)."""

from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from chitra.rate_limit_state import (
    Transaction,
    get_transaction,
    load_transactions,
    remove_transaction,
    transactions_path,
    upsert_transaction,
)

ISO = "2026-07-12T00:00:00+00:00"


def _txn(session_ref: str = "tophand:lane1:0.0", phase: str = "pause_requested") -> Transaction:
    return Transaction(session_ref=session_ref, phase=phase, hold_reason="rate-limit:5h", created_at=ISO, updated_at=ISO)  # type: ignore[arg-type]


def test_upsert_and_get_round_trip(tmp_path: Path) -> None:
    stored = upsert_transaction(tmp_path, _txn())
    assert get_transaction(tmp_path, "tophand:lane1:0.0") == stored
    assert load_transactions(tmp_path) == [stored]
    assert not list(tmp_path.glob("*.tmp"))


def test_upsert_replaces_by_session_ref(tmp_path: Path) -> None:
    upsert_transaction(tmp_path, _txn(phase="pause_requested"))
    upsert_transaction(tmp_path, _txn(phase="checkpoint_sent"))
    records = load_transactions(tmp_path)
    assert len(records) == 1
    assert records[0].phase == "checkpoint_sent"


def test_remove_transaction_is_a_no_op_if_absent(tmp_path: Path) -> None:
    remove_transaction(tmp_path, "no-such-lane")  # must not raise
    upsert_transaction(tmp_path, _txn())
    remove_transaction(tmp_path, "tophand:lane1:0.0")
    assert get_transaction(tmp_path, "tophand:lane1:0.0") is None


def test_get_transaction_missing_store_returns_none(tmp_path: Path) -> None:
    assert get_transaction(tmp_path, "anything") is None
    assert load_transactions(tmp_path) == []


def test_from_dict_round_trips_every_field() -> None:
    full = Transaction(
        session_ref="tophand:lane1:0.0",
        phase="awaiting_quiescence",
        hold_reason="rate-limit:5h",
        resume_at=ISO,
        checkpoint_order_id="ord-1",
        stop_order_id="ord-2",
        resume_order_id="ord-3",
        transcript_path="/tmp/t.jsonl",
        last_transcript_mtime=1234.5,
        quiescent_since=ISO,
        attempts=2,
        escalated=True,
        deadline_at=ISO,
        created_at=ISO,
        updated_at=ISO,
    )
    assert Transaction.from_dict(full.to_dict()) == full


def test_from_dict_rejects_an_unknown_phase() -> None:
    with pytest.raises(ValueError, match="phase must be one of"):
        Transaction.from_dict({"session_ref": "x", "phase": "not-a-real-phase"})


def test_from_dict_rejects_non_object_payload() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        Transaction.from_dict("not-a-dict")


def test_load_transactions_rejects_wrong_schema(tmp_path: Path) -> None:
    transactions_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    transactions_path(tmp_path).write_text('{"schema": "wrong", "transactions": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="chitra.rate_limit_state.v1"):
        load_transactions(tmp_path)


def _mp_upsert(root_str: str, session_ref: str) -> None:
    upsert_transaction(Path(root_str), _txn(session_ref=session_ref))


def test_concurrent_writers_adding_different_transactions_do_not_lose_each_other(tmp_path: Path) -> None:
    """Same lost-update class as chitra.goals (finding #9's pattern) --
    rate_limit_state uses the identical flock-serialized read-modify-write."""
    ctx = multiprocessing.get_context("fork")
    refs = [f"host:lane-{i}:0.0" for i in range(15)]
    procs = [ctx.Process(target=_mp_upsert, args=(str(tmp_path), ref)) for ref in refs]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    stored_refs = {txn.session_ref for txn in load_transactions(tmp_path)}
    assert stored_refs == set(refs)
