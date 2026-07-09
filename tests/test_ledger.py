"""Tests for chitra.ledger: HMAC signing + append-only delivery ledger."""

from __future__ import annotations

from pathlib import Path

from chitra.ledger import (
    append_entry,
    load_or_create_signing_key,
    message_hash,
    sign,
    verify_delivery,
    verify_entry,
)


def test_load_or_create_signing_key_persists_across_calls(tmp_path: Path) -> None:
    key_path = tmp_path / "ledger.key"
    key1 = load_or_create_signing_key(key_path)
    key2 = load_or_create_signing_key(key_path)
    assert key1 == key2
    assert len(key1) == 32
    assert oct(key_path.stat().st_mode)[-3:] == "600"


def test_append_entry_writes_a_valid_signed_record(tmp_path: Path) -> None:
    key = load_or_create_signing_key(tmp_path / "ledger.key")
    ledger_path = tmp_path / "ledger.jsonl"
    entry = append_entry(ledger_path, order_id="o1", session_ref="localhost:s:0.0", tag="[C]", nudge="hello lane", key=key)
    assert verify_entry(entry, key=key) is True
    assert ledger_path.exists()
    assert ledger_path.read_text(encoding="utf-8").count("\n") == 1


def test_append_entry_is_append_only(tmp_path: Path) -> None:
    key = load_or_create_signing_key(tmp_path / "ledger.key")
    ledger_path = tmp_path / "ledger.jsonl"
    append_entry(ledger_path, order_id="o1", session_ref="localhost:s:0.0", tag="[C]", nudge="first", key=key)
    append_entry(ledger_path, order_id="o2", session_ref="localhost:s:0.0", tag="[C]", nudge="second", key=key)
    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_verify_entry_fails_with_wrong_key(tmp_path: Path) -> None:
    key = load_or_create_signing_key(tmp_path / "ledger.key")
    other_key = load_or_create_signing_key(tmp_path / "other.key")
    ledger_path = tmp_path / "ledger.jsonl"
    entry = append_entry(ledger_path, order_id="o1", session_ref="localhost:s:0.0", tag="[C]", nudge="hello", key=key)
    assert verify_entry(entry, key=other_key) is False


def test_verify_entry_fails_if_message_hash_tampered(tmp_path: Path) -> None:
    key = load_or_create_signing_key(tmp_path / "ledger.key")
    ledger_path = tmp_path / "ledger.jsonl"
    entry = append_entry(ledger_path, order_id="o1", session_ref="localhost:s:0.0", tag="[C]", nudge="hello", key=key)
    tampered = entry.model_copy(update={"message_hash": message_hash("a different message")})
    assert verify_entry(tampered, key=key) is False


def test_verify_delivery_finds_a_real_delivery(tmp_path: Path) -> None:
    key = load_or_create_signing_key(tmp_path / "ledger.key")
    ledger_path = tmp_path / "ledger.jsonl"
    append_entry(ledger_path, order_id="o1", session_ref="localhost:s:0.0", tag="[C]", nudge="the operator ruling", key=key)
    found = verify_delivery(ledger_path, key=key, session_ref="localhost:s:0.0", nudge="the operator ruling")
    assert found is not None
    assert found.order_id == "o1"


def test_verify_delivery_returns_none_for_a_message_never_sent(tmp_path: Path) -> None:
    """Proof of absence: chitra did NOT send this — no entry, no signature."""
    key = load_or_create_signing_key(tmp_path / "ledger.key")
    ledger_path = tmp_path / "ledger.jsonl"
    append_entry(ledger_path, order_id="o1", session_ref="localhost:s:0.0", tag="[C]", nudge="real message", key=key)
    found = verify_delivery(ledger_path, key=key, session_ref="localhost:s:0.0", nudge="a message chitra never sent")
    assert found is None


def test_verify_delivery_returns_none_against_empty_ledger(tmp_path: Path) -> None:
    key = load_or_create_signing_key(tmp_path / "ledger.key")
    found = verify_delivery(tmp_path / "does-not-exist.jsonl", key=key, session_ref="localhost:s:0.0", nudge="anything")
    assert found is None


def test_sign_is_deterministic_for_the_same_inputs() -> None:
    key = b"0" * 32
    sig1 = sign(key, session_ref="s", tag="[C]", digest="abc", sent_at="2026-07-09T00:00:00Z")
    sig2 = sign(key, session_ref="s", tag="[C]", digest="abc", sent_at="2026-07-09T00:00:00Z")
    assert sig1 == sig2


def test_sign_changes_if_any_field_changes() -> None:
    key = b"0" * 32
    base = sign(key, session_ref="s", tag="[C]", digest="abc", sent_at="2026-07-09T00:00:00Z")
    different_tag = sign(key, session_ref="s", tag="[X]", digest="abc", sent_at="2026-07-09T00:00:00Z")
    assert base != different_tag
