"""ledger — HMAC-signed, append-only delivery ledger for dispatchd.

Incident-driven (2026-07-09): a genuine operator instruction typed directly
into a lane's pane was nearly overridden by the monitor because relayed
[C]-tagged messages carried no way for the receiving lane to tell "chitra
really sent this at T" from an unauthenticated claim. This module is
deliberately minimal — one signing key, one append-only JSONL file — and
adds no friction to a normal send: `dispatchd` signs and logs automatically
on every successful delivery; nothing else changes about the send path.

What this proves, and to whom:
- POSITIVE: "chitra delivered this exact message to this session at this
  time" — a lane (or any local reader) greps `ledger.jsonl` for its own
  session_ref + message hash and verifies the HMAC with the shared key.
- NEGATIVE: "chitra did NOT send this" — absence from the ledger is itself
  the proof; the ledger is append-only, so a message that isn't in it was
  never delivered by chitra. (Operator-direct typing into a pane needs no
  entry and no authentication — the pane itself is the operator's own
  channel; only chitra-relayed [C] messages go through this ledger.)

No LLM calls. Deterministic signing/logging only.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

DEFAULT_KEY_PATH = Path("/var/lib/polyphony-chitra/ledger.key")
DEFAULT_LEDGER_PATH = Path("/var/lib/polyphony-chitra/ledger.jsonl")
_KEY_BYTES = 32


class LedgerEntry(BaseModel):
    """One signed, append-only delivery record."""

    order_id: str
    session_ref: str
    tag: str
    message_hash: str
    sent_at: str
    signature: str


def message_hash(nudge: str) -> str:
    """SHA-256 hex digest of the exact delivered text."""
    return hashlib.sha256(nudge.encode("utf-8", errors="replace")).hexdigest()


def load_or_create_signing_key(key_path: Path = DEFAULT_KEY_PATH) -> bytes:
    """Load the HMAC signing key, generating and persisting a new one
    (mode 0600) on first use. Idempotent and safe under concurrent daemons —
    a race to create the key resolves to whichever writer's ``open`` with
    ``O_EXCL`` wins; the loser simply re-reads the winner's key."""
    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(key_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return key_path.read_bytes()
    key = os.urandom(_KEY_BYTES)
    os.write(fd, key)
    os.close(fd)
    return key


def sign(key: bytes, *, session_ref: str, tag: str, digest: str, sent_at: str) -> str:
    """HMAC-SHA256 signature over (sent_at, session_ref, tag, message_hash),
    hex-encoded. The canonical string is a fixed, unambiguous field order —
    changing any field changes the signature."""
    canonical = "|".join([sent_at, session_ref, tag, digest]).encode("utf-8")
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def append_entry(
    ledger_path: Path,
    *,
    order_id: str,
    session_ref: str,
    tag: str,
    nudge: str,
    key: bytes,
    sent_at: str | None = None,
) -> LedgerEntry:
    """Sign and append one delivery record. Append-only: never rewrites or
    truncates existing entries."""
    stamp = sent_at or datetime.now(UTC).isoformat()
    digest = message_hash(nudge)
    signature = sign(key, session_ref=session_ref, tag=tag, digest=digest, sent_at=stamp)
    entry = LedgerEntry(order_id=order_id, session_ref=session_ref, tag=tag, message_hash=digest, sent_at=stamp, signature=signature)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as fh:
        fh.write(entry.model_dump_json() + "\n")
    return entry


def verify_entry(entry: LedgerEntry, *, key: bytes) -> bool:
    """Recompute the signature and compare (constant-time) against the
    entry's recorded signature."""
    expected = sign(key, session_ref=entry.session_ref, tag=entry.tag, digest=entry.message_hash, sent_at=entry.sent_at)
    return hmac.compare_digest(expected, entry.signature)


def verify_delivery(
    ledger_path: Path,
    *,
    key: bytes,
    session_ref: str,
    nudge: str,
) -> LedgerEntry | None:
    """Return the first ledger entry proving ``nudge`` was delivered to
    ``session_ref`` with a valid signature, or None if no such entry exists
    (a caller can treat ``None`` as proof-of-absence — chitra never sent
    this — since the ledger is append-only and every real send is logged)."""
    if not ledger_path.exists():
        return None
    digest = message_hash(nudge)
    with ledger_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = LedgerEntry.model_validate_json(line)
            except ValueError:
                continue
            if entry.session_ref == session_ref and entry.message_hash == digest and verify_entry(entry, key=key):
                return entry
    return None
