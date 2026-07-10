"""ledger — HMAC-signed, append-only delivery ledger for dispatchd.

Incident-driven (2026-07-09): a genuine operator instruction typed directly
into a lane's pane was nearly overridden by the monitor because relayed
[C]-tagged messages carried no way for the receiving lane to tell "chitra
really sent this at T" from an unauthenticated claim. This module is
deliberately minimal — one signing key, one append-only JSONL file — and
adds no friction to a normal send: `dispatchd` signs and logs automatically
on every successful delivery; nothing else changes about the send path.

Threat model: trusted host. This assumes whoever can write to the ledger's
state directory (a systemd-supervised `dispatchd`, plus the host's own
root/admin) is trusted. It is not designed to resist a malicious actor with
filesystem write access to `ledger.jsonl` itself.

What this proves, and to whom:
- POSITIVE (cryptographic): "chitra delivered this exact message to this
  session at this time" — a lane (or any local reader) greps `ledger.jsonl`
  for its own session_ref + message hash and verifies the HMAC with the
  shared key. If you have the entry, its authenticity is provable.
- ABSENCE (convention, not cryptographic): `dispatchd` only ever appends to
  this file, so under normal operation a message's absence from it suggests
  no such delivery happened. But that append-only-ness is enforced by
  convention and file permissions, NOT by a hash chain or counter linking
  entries to each other — nothing here would let a reader detect a wholesale
  truncation or edit of the file. Anyone with write access to the ledger can
  rewrite or shorten it undetected. Treat "not in the ledger" as a strong
  signal under the trusted-host assumption, not as tamper-proof evidence.
  (Operator-direct typing into a pane needs no entry and no authentication —
  the pane itself is the operator's own channel; only chitra-relayed [C]
  messages go through this ledger.)

No LLM calls in this module's own code path — deterministic signing/logging only.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

DEFAULT_KEY_PATH = Path("/var/lib/chitra/ledger.key")
DEFAULT_LEDGER_PATH = Path("/var/lib/chitra/ledger.jsonl")
_KEY_BYTES = 32


class LedgerEntry(BaseModel):
    """One signed, append-only delivery record.

    ``routing_hint`` is an opaque, caller-supplied value copied through
    unchanged from the originating ``DispatchOrder`` — chitra signs and logs
    it for audit purposes only, never interprets it, exactly like ``tag``.
    """

    order_id: str
    session_ref: str
    tag: str
    routing_hint: str | None = None
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


def sign(key: bytes, *, session_ref: str, tag: str, digest: str, sent_at: str, routing_hint: str | None = None) -> str:
    """HMAC-SHA256 signature over (sent_at, session_ref, tag, message_hash,
    routing_hint), hex-encoded. The canonical string is a fixed, unambiguous
    field order — changing any field changes the signature. ``routing_hint``
    is part of the signed payload (an empty placeholder when absent) since
    it is part of the record being attested to, same as every other field
    here."""
    canonical = "|".join([sent_at, session_ref, tag, digest, routing_hint or ""]).encode("utf-8")
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def append_entry(
    ledger_path: Path,
    *,
    order_id: str,
    session_ref: str,
    tag: str,
    nudge: str,
    key: bytes,
    routing_hint: str | None = None,
    sent_at: str | None = None,
) -> LedgerEntry:
    """Sign and append one delivery record. Append-only: never rewrites or
    truncates existing entries."""
    stamp = sent_at or datetime.now(UTC).isoformat()
    digest = message_hash(nudge)
    signature = sign(key, session_ref=session_ref, tag=tag, digest=digest, sent_at=stamp, routing_hint=routing_hint)
    entry = LedgerEntry(
        order_id=order_id,
        session_ref=session_ref,
        tag=tag,
        routing_hint=routing_hint,
        message_hash=digest,
        sent_at=stamp,
        signature=signature,
    )
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as fh:
        fh.write(entry.model_dump_json() + "\n")
    return entry


def verify_entry(entry: LedgerEntry, *, key: bytes) -> bool:
    """Recompute the signature and compare (constant-time) against the
    entry's recorded signature."""
    expected = sign(
        key,
        session_ref=entry.session_ref,
        tag=entry.tag,
        digest=entry.message_hash,
        sent_at=entry.sent_at,
        routing_hint=entry.routing_hint,
    )
    return hmac.compare_digest(expected, entry.signature)


def verify_delivery(
    ledger_path: Path,
    *,
    key: bytes,
    session_ref: str,
    nudge: str,
) -> LedgerEntry | None:
    """Return the first ledger entry proving ``nudge`` was delivered to
    ``session_ref`` with a valid signature, or None if no such entry exists.

    ``None`` is a strong signal under this module's trusted-host threat
    model (chitra only ever appends, so nothing it wrote is missing) but is
    NOT cryptographic proof of absence: append-only-ness here is convention
    and file permissions, not a hash chain or counter, so a caller with
    write access to ``ledger.jsonl`` could truncate or edit it undetected.
    """
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
