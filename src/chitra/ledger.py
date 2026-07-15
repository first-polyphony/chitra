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

import fcntl
import hashlib
import hmac
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, PrivateAttr, model_validator

from .reasoning import DecisionAttestation
from .state_paths import default_ledger_key_path

_KEY_BYTES = 32


class _LegacySignatureFields(BaseModel):
    """Retired fields retained only while verifying v2/v3 ledger rows."""

    routing_hint_source: str = "unset"
    resolved_model: str | None = None
    resolved_harness: str | None = None


class LedgerEntry(BaseModel):
    """One signed, append-only delivery record.

    ``routing_hint`` is an opaque, caller-supplied value copied through
    unchanged from the originating ``DispatchOrder`` — chitra signs and logs
    it for audit purposes only, never interprets it, exactly like ``tag``.
    """

    model_config = ConfigDict(extra="allow")

    order_id: str
    session_ref: str
    tag: str
    routing_hint: str | None = None
    task_type: str | None = None
    resolved_zdr: bool = False
    sig_v: int = 1
    message_hash: str
    sent_at: str
    signature: str

    _legacy_signature_fields: _LegacySignatureFields = PrivateAttr(default_factory=_LegacySignatureFields)

    @model_validator(mode="after")
    def capture_legacy_signature_fields(self) -> LedgerEntry:
        """Retain retired signed fields privately when reading old rows."""
        extras = self.__pydantic_extra__ or {}
        payload = {name: extras[name] for name in _LegacySignatureFields.model_fields if name in extras}
        self._legacy_signature_fields = _LegacySignatureFields.model_validate(payload)
        self.__pydantic_extra__ = {}
        return self


class AttestationLedgerEntry(BaseModel):
    """One our-side decision record, deliberately separate from delivery proof."""

    order_id: str
    session_ref: str
    attestation: DecisionAttestation
    logged_at: str


def append_attestation(
    ledger_path: Path,
    *,
    order_id: str,
    session_ref: str,
    attestation: DecisionAttestation,
) -> AttestationLedgerEntry:
    """Durably append an attestation once; never expose it in pane text."""
    entry = AttestationLedgerEntry(
        order_id=order_id,
        session_ref=session_ref,
        attestation=attestation,
        logged_at=datetime.now(UTC).isoformat(),
    )
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger_path.with_name(ledger_path.name + ".lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if ledger_path.exists():
                for line in ledger_path.read_text(encoding="utf-8").splitlines():
                    try:
                        existing = AttestationLedgerEntry.model_validate_json(line)
                    except ValueError:
                        continue
                    if existing.order_id == order_id and existing.attestation.attestation_id == attestation.attestation_id:
                        return existing
            with ledger_path.open("a", encoding="utf-8") as output:
                output.write(entry.model_dump_json() + "\n")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return entry


def message_hash(nudge: str) -> str:
    """SHA-256 hex digest of the exact delivered text."""
    return hashlib.sha256(nudge.encode("utf-8", errors="replace")).hexdigest()


def load_or_create_signing_key(key_path: Path | None = None) -> bytes:
    """Load the HMAC signing key, generating and persisting a new one
    (mode 0600) on first use. Idempotent and safe under concurrent daemons —
    a race to create the key resolves to whichever writer's ``open`` with
    ``O_EXCL`` wins; the loser simply re-reads the winner's key."""
    key_path = key_path or default_ledger_key_path()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(key_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return key_path.read_bytes()
    key = os.urandom(_KEY_BYTES)
    os.write(fd, key)
    os.close(fd)
    return key


def _sign_versioned(
    key: bytes,
    *,
    session_ref: str,
    tag: str,
    digest: str,
    sent_at: str,
    routing_hint: str | None = None,
    task_type: str | None = None,
    resolved_zdr: bool = False,
    sig_v: int,
    legacy_fields: _LegacySignatureFields | None = None,
) -> str:
    """Sign one current or historical canonical ledger field set."""
    if sig_v not in (1, 2, 3, 4):
        raise ValueError(f"unsupported signature version: {sig_v}")
    fields = [sent_at, session_ref, tag, digest, routing_hint or ""]
    if sig_v in (2, 3):
        legacy = legacy_fields or _LegacySignatureFields()
        fields.extend([task_type or "", legacy.routing_hint_source])
        if sig_v == 3:
            fields.extend(
                [legacy.resolved_model or "", legacy.resolved_harness or "", "1" if resolved_zdr else "0"]
            )
    elif sig_v == 4:
        fields.extend([task_type or "", "1" if resolved_zdr else "0"])
    canonical = "|".join(fields).encode("utf-8")
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def sign(
    key: bytes,
    *,
    session_ref: str,
    tag: str,
    digest: str,
    sent_at: str,
    routing_hint: str | None = None,
    task_type: str | None = None,
    resolved_zdr: bool = False,
    sig_v: int = 4,
) -> str:
    """HMAC-SHA256 signature over the current canonical ledger fields."""
    return _sign_versioned(
        key,
        session_ref=session_ref,
        tag=tag,
        digest=digest,
        sent_at=sent_at,
        routing_hint=routing_hint,
        task_type=task_type,
        resolved_zdr=resolved_zdr,
        sig_v=sig_v,
    )


def append_entry(
    ledger_path: Path,
    *,
    order_id: str,
    session_ref: str,
    tag: str,
    nudge: str,
    key: bytes,
    routing_hint: str | None = None,
    task_type: str | None = None,
    resolved_zdr: bool = False,
    sent_at: str | None = None,
) -> LedgerEntry:
    """Sign and append one delivery record. Append-only: never rewrites or
    truncates existing entries."""
    stamp = sent_at or datetime.now(UTC).isoformat()
    digest = message_hash(nudge)
    signature = sign(
        key,
        session_ref=session_ref,
        tag=tag,
        digest=digest,
        sent_at=stamp,
        routing_hint=routing_hint,
        task_type=task_type,
        resolved_zdr=resolved_zdr,
    )
    entry = LedgerEntry(
        order_id=order_id,
        session_ref=session_ref,
        tag=tag,
        routing_hint=routing_hint,
        task_type=task_type,
        resolved_zdr=resolved_zdr,
        sig_v=4,
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
    expected = _sign_versioned(
        key,
        session_ref=entry.session_ref,
        tag=entry.tag,
        digest=entry.message_hash,
        sent_at=entry.sent_at,
        routing_hint=entry.routing_hint,
        task_type=entry.task_type,
        resolved_zdr=entry.resolved_zdr,
        sig_v=entry.sig_v,
        legacy_fields=entry._legacy_signature_fields,
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
