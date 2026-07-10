"""Hardened tmux dispatch library for delivering text into a Claude Code
tmux pane.

This module was extracted and hardened from an earlier internal
implementation that had two known bugs, both fixed here:

(a) ``paste-buffer`` without ``-p`` sends no bracketed-paste wrapper, so
    newlines in multiline text act as real Enters — the original
    implementation did NOT pass ``-p`` to ``paste-buffer``. This module
    adds ``-p`` (mandatory).

(b) A pane in tmux copy-mode (``pane_in_mode=1``) silently eats all input.
    The original had no check for this. This module checks
    ``tmux display-message -p -t <target> '#{pane_in_mode}'`` and, if ``1``,
    runs ``tmux send-keys -X cancel`` and waits ~0.3s before injecting. The
    check runs against the actual target host: a plain local ``tmux`` call
    for a local target, or the identical command wrapped in ``ssh_command``
    for a remote one — checking the local tmux server for a remote target's
    copy-mode state would report on the wrong tmux server entirely.

Post-send verification uses transcript-grep against the target session's
own ``~/.claude/projects/*/*.jsonl`` transcript (found by recency + content
match, explicitly excluding the caller's own transcript), replacing the
weaker pane-capture confirmation: a spinner or status line is not evidence
that a message was actually received; the transcript is. For a remote
target this grep runs over ssh against the **target host's** filesystem
(``find_recent_transcript_remote``) — the transcript proving a remote
delivery lives on the remote host, never on the machine chitra runs on.

Single-writer rule
-----------------

``LaneLock`` enforces one writer per session id. ``dispatchd`` acquires a
lock for the order's session id before any delivery attempt and releases it
after. Acquiring a lock for an already-locked session id fails rather than
silently proceeding. ``claude -p --resume`` is permitted ONLY as a fallback
for sessions confirmed DETACHED/STOPPED after an explicit liveness check —
never for a live lane. The ``-p --resume`` fallback path itself is out of
scope for this build; only the ``LaneLock`` enforcement and a
``liveness_check`` helper stub are provided here.

Directive-voice guard
----------------------

``directive_voice_violation`` is a pure regex predicate checked at the top
of ``dispatch_to_tmux``, before the pre-dispatch pane check and before
anything is pasted. Chitra relays instructions; it never speaks as the
operator or claims the operator's authority. A nudge that attributes itself
to "the operator" / "the monitor", or has chitra claim in its own voice to
want/say/need/relay something, is rejected outright: ``dispatch_to_tmux``
returns ``DispatchResult(status=BLOCKED, reason="directive-voice: ...")``
with nothing pasted and no delivery-ledger entry (``dispatchd`` only signs
the ledger on ``SENT`` — see ``dispatchd.process_one_order``).

Origin / never-cancel guard (MANDATORY CONTRACT for any future reconciler)
----------------------------------------------------------------------------

No reconciliation/drift-detection code path exists in this codebase yet
(``dispatchd`` only delivers; nothing currently holds, cancels, or reorders
a target session's task list). This module nonetheless fixes the contract
any future reconciler MUST follow, and provides the predicate + ledger
lookup it must use: ``is_chitra_dispatched_task`` cross-references the
delivery ledger (``chitra.ledger.verify_delivery``) for a given
``session_ref`` + task text. A task with **no** matching ledger entry is
presumed operator-authored and is **immutable to chitra** — a future
reconciler may only ADD tasks or REORDER tasks for which this predicate
returns ``True`` (chitra-dispatched ones). It must never remove, hold, or
"correct away" a task for which this predicate returns ``False``. A
growing task list is not drift.

Completion-claim auditing
--------------------------

``DispatchOrder`` carries three optional fields (``completion_todo_items``,
``completion_has_deploy_evidence``, ``completion_has_live_verify_evidence``)
consumed by ``chitra.completion_gate`` and checked in
``dispatchd.process_one_order`` before delivery. This module still performs
no reasoning itself — it only carries the typed inputs the gate needs.
"""

from __future__ import annotations

import contextlib
import enum
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import structlog
from pydantic import BaseModel, Field

from chitra.completion_gate import TodoItem
from chitra.policy_config import PolicyConfig

from . import ledger as ledger_mod

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants (mirrors of the source)
# ---------------------------------------------------------------------------

DISPATCH_CAPTURE_LINES: int = 12
DISPATCH_VERIFY_WAIT_SECONDS: float = 0.15
PANE_IN_MODE_CANCEL_WAIT_SECONDS: float = 0.3
DEFAULT_REMOTE_HOSTS: str = ""

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_IDLE_INPUT_LINE_RE = re.compile(
    r"^\s*(?:(?:\([^)]+\)|\[[^\]]+\])\s*)?"
    r"(?:[\w.-]+@[\w.-]+(?::[^$#%>]*)?)?\s*"
    r"(?:[$#%>]|>>>|\.\.\.|In \[\d+\]:)\s*$"
)
_CLAUDE_CODE_HORIZONTAL_RULE_RE = re.compile(r"^─+$")
_CLAUDE_CODE_INPUT_ROW_RE = re.compile(r"^❯(?P<draft>.*)$")

# Directive-voice guard: chitra relays instructions, it never speaks AS the
# operator or claims the operator's authority. A nudge that attributes itself
# to "the operator" or "the monitor", or has chitra claim to want/say/need/
# relay something in its own voice, is a directive-voice violation.
_BANNED = re.compile(r"\boperator\b|\bthe monitor\b|\bchitra (wants|says|needs|relays)\b", re.I)
_TRANSCRIPT_GLOB_DEFAULT = "*/*.jsonl"


# ---------------------------------------------------------------------------
# Pydantic boundary models
# ---------------------------------------------------------------------------


class DispatchStatus(enum.StrEnum):
    """Outcome of a dispatch attempt."""

    SENT = "sent"
    BLOCKED = "blocked"
    FAILED = "failed"
    # A completion-claim audit (chitra.completion_gate.evaluate_completion_claim)
    # found a gap (todo residue, deferral language, or missing evidence). The
    # order was never delivered -- a disputed completion claim must never
    # silently pass through as "sent". See dispatchd.process_one_order.
    COMPLETION_DISPUTE = "completion_dispute"


class DispatchOrder(BaseModel):
    """A dispatch order consumed by ``dispatchd``.

    ``session_ref`` uses the ``host:session:pane`` convention from the
    source. ``nudge`` is the verbatim text to inject. ``order_id`` is the
    caller-supplied unique id used for result-file naming. ``tag`` marks the
    message's authenticity class in the delivery ledger — ``"[C]"`` (chitra
    relay) is the default; a caller relaying verbatim operator-typed text
    with no relay in between may use a different tag, but the ledger records
    whatever tag is asserted so it can be audited later. ``routing_hint`` is
    an opaque, caller-supplied string recording a routing/model-preference
    decision already made upstream — chitra never reads, validates, or acts
    on its contents; it is only carried through to ``DispatchResult`` and
    the ledger for audit purposes, exactly like ``tag``. ``task_type`` is a
    separate, optional caller-supplied classification string (e.g.
    ``"code-review"``) — chitra does not decide what a task type IS or
    evaluate content to classify one; the caller states it. If the caller
    sets ``task_type`` but leaves ``routing_hint`` unset, ``dispatchd`` may
    fill in ``routing_hint`` from a purely mechanical ``task_type ->
    routing_hint`` lookup table (see ``chitra.routing_config``) — an
    explicit ``routing_hint`` from the caller always wins over this lookup.
    """

    order_id: str
    session_ref: str
    nudge: str
    """Verbatim text to inject. Convention (enforced in practice by
    ``directive_voice_violation``'s regex match on the ``operator`` token):
    chitra must never quote the operator verbatim or speak in the
    operator's voice — no "the operator wants/says", no "chitra
    wants/says/needs/relays", no bare "operator" attribution. A nudge that
    trips the check is rejected by ``dispatch_to_tmux`` (status
    ``BLOCKED``) before anything is pasted."""
    tag: str = "[C]"
    routing_hint: str | None = None
    task_type: str | None = None
    input_baseline_hash: str | None = None
    input_seen_hash: str | None = None
    snapshot_tail_hash: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    # Optional completion-claim audit inputs. All three are opt-in: a caller
    # that leaves ``completion_todo_items`` as None is asserting no
    # completion-gate check applies to this order (e.g. it isn't a done/
    # complete claim at all), and dispatchd.process_one_order skips the
    # audit entirely in that case -- existing callers/orders are unaffected.
    # When ``completion_todo_items`` IS set, dispatchd runs
    # chitra.completion_gate.evaluate_completion_claim before delivery; see
    # that module's docstring and docs/evasion-taxonomy.md.
    completion_todo_items: list[TodoItem] | None = None
    completion_has_deploy_evidence: bool = False
    completion_has_live_verify_evidence: bool = False


class DispatchResult(BaseModel):
    """Result of processing a dispatch order.

    ``routing_hint`` is copied through unchanged from the originating
    ``DispatchOrder`` when the caller supplied one (opaque pass-through) or
    the ``defaults`` config filled it in. When a structured ``routes`` entry
    resolved the task_type instead, ``routing_hint`` holds the derived
    ``model@harness`` string and the resolved selection is also recorded
    structurally in ``resolved_model`` / ``resolved_harness`` / ``resolved_zdr``
    (``routing_hint_source == "route"``).
    """

    order_id: str
    session_ref: str
    status: DispatchStatus
    reason: str = ""
    marker: str = ""
    tail_hash: str = ""
    transcript_path: str | None = None
    routing_hint: str | None = None
    task_type: str | None = None
    routing_hint_source: str = "unset"
    # Resolved structured selection when ``routing_hint_source == "route"``
    # (see chitra.routing_config.resolve_route): the concrete model + harness
    # (+ zdr) chitra resolved from the task_type's ``routes`` entry. None /
    # False for the opaque ``defaults`` path, an explicit caller hint, or no
    # routing config -- those record only the opaque ``routing_hint``.
    resolved_model: str | None = None
    resolved_harness: str | None = None
    resolved_zdr: bool = False
    at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


# ---------------------------------------------------------------------------
# PaneInputCheck (mirror of the source dataclass)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PaneInputCheck:
    """Result of checking whether a pane is safe to dispatch into.

    ``ok`` is True only when the pane is idle (matches a known idle hash or
    shows a bare prompt line with no draft). A pane with an unsubmitted
    operator draft is ``ok=False`` so dispatch is blocked — never silently
    overwrite an operator's pending input.
    """

    ok: bool
    reason: str
    tail_hash: str
    last_line: str


@dataclass(frozen=True, slots=True)
class DispatchTuning:
    """Dispatch reliability bounds, carried together through the daemon."""

    capture_lines: int = DISPATCH_CAPTURE_LINES
    post_paste_wait_seconds: float = DISPATCH_VERIFY_WAIT_SECONDS
    transcript_recency_seconds: float = 300.0
    lane_lock_timeout_seconds: float = 5.0


# ---------------------------------------------------------------------------
# Tmux command runner protocol (for test injection)
# ---------------------------------------------------------------------------


class TmuxRunner(Protocol):
    """Callable that runs a command and returns a CompletedProcess[str]."""

    def __call__(self, cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]: ...


class TmuxInputRunner(Protocol):
    """Callable that runs a command with stdin payload."""

    def __call__(self, cmd: list[str], payload: str, *, timeout: int = 20) -> subprocess.CompletedProcess[str]: ...


def run_cmd(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing output, never raising on non-zero exit.

    Mirrors the source's ``run_cmd``. ``FileNotFoundError`` (binary missing)
    returns rc=127; ``TimeoutExpired`` returns rc=124.
    """
    try:
        return subprocess.run(
            cmd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return subprocess.CompletedProcess(cmd, 124, stdout=stdout, stderr=stderr or f"timed out after {timeout}s")
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr=str(exc))


def run_cmd_input(cmd: list[str], payload: str, *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    """Run a command with stdin payload, capturing output, never raising."""
    try:
        return subprocess.run(
            cmd,
            input=payload,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return subprocess.CompletedProcess(cmd, 124, stdout=stdout, stderr=stderr or f"timed out after {timeout}s")
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr=str(exc))


# ---------------------------------------------------------------------------
# Host allowlist + local-host detection
# ---------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def allowed_remote_dispatch_hosts(env: str | None = None) -> set[str]:
    """Return the set of remote hosts dispatch may target.

    Parameterized: reads ``REMOTE_DISPATCH_HOSTS`` (or the supplied ``env``
    string) and splits on commas. Defaults to no remote hosts allowed —
    deployments opt in to specific remote host names via the env var or by
    passing ``allowed_hosts`` directly to ``dispatch_to_tmux``.
    """
    raw = env if env is not None else _env("REMOTE_DISPATCH_HOSTS", DEFAULT_REMOTE_HOSTS)
    return {item.strip() for item in raw.split(",") if item.strip()}


def local_host_aliases(extra: set[str] | None = None) -> set[str]:
    """Return the set of aliases that refer to the local host.

    Includes the short hostname, fqdn, ``localhost``, and ``127.0.0.1``.
    Tests inject ``extra`` to pin the local identity.
    """
    aliases: set[str] = {"localhost", "127.0.0.1"}
    try:
        aliases.add(socket.gethostname().split(".", 1)[0])
        aliases.add(socket.getfqdn().split(".", 1)[0])
    except OSError:
        pass
    override = _env("CHITRA_LOCAL_HOST")
    if override:
        aliases.add(override.split(".", 1)[0])
    if extra:
        aliases |= extra
    return aliases


def is_local_host(host: str, extra: set[str] | None = None) -> bool:
    """Return True if ``host`` refers to the local machine."""
    return host.split(".", 1)[0] in local_host_aliases(extra)


def tmux_pane_target(session: str, pane: str) -> str:
    """Build a fully-qualified tmux target from a ``session_ref``'s session
    and pane components.

    A bare pane spec like ``"0.0"`` resolves against tmux's CURRENT session
    when passed alone to ``-t`` — on any host running more than one tmux
    session (this package's entire intended deployment shape), that silently
    targets the wrong session. Qualify with the session name unless the pane
    is already fully-qualified (contains ``:``) or is a globally-unique tmux
    pane id (``%N``, valid on its own regardless of session).
    """
    if not pane or ":" in pane or pane.startswith("%"):
        return pane
    return f"{session}:{pane}"


# ---------------------------------------------------------------------------
# Text normalization helpers (mirrors of the source)
# ---------------------------------------------------------------------------


def directive_voice_violation(nudge: str, *, patterns: Sequence[re.Pattern[str]] | None = None) -> str | None:
    """Return the banned attribution phrase found in ``nudge``, or ``None``.

    Pure regex predicate: chitra relays instructions, it never speaks as
    the operator or claims the operator's authority. Matches a bare
    ``operator`` token, ``the monitor``, or chitra claiming to
    want/say/need/relay something in its own voice. Case-insensitive.
    """
    if patterns is None:
        m = _BANNED.search(nudge)
        return m.group(0) if m else None
    for pattern in patterns:
        m = pattern.search(nudge)
        if m:
            return m.group(0)
    return None


def is_chitra_dispatched_task(
    task_text: str,
    *,
    session_ref: str,
    ledger_path: Path,
    key: bytes,
) -> bool:
    """Origin / never-cancel guard: is ``task_text`` something chitra itself
    dispatched to ``session_ref``?

    Cross-references the delivery ledger via ``ledger.verify_delivery``.
    Returns ``True`` only if a signed ledger entry proves chitra delivered
    this exact text to this session. ``False`` means no matching entry
    exists — the task is presumed operator-authored and MUST be treated as
    immutable by any reconciler (see this module's docstring): a
    reconciler may add tasks or reorder tasks for which this returns
    ``True``, but must never remove, hold, or "correct away" a task for
    which this returns ``False``.
    """
    return ledger_mod.verify_delivery(ledger_path, key=key, session_ref=session_ref, nudge=task_text) is not None


def strip_terminal_controls(text: str) -> str:
    """Strip ANSI escape sequences and surrounding whitespace."""
    return _ANSI_ESCAPE_RE.sub("", text).strip()


def _claude_code_input_row(captured_lines: list[str]) -> tuple[str, str] | None:
    """Return a Claude Code input row and its draft when its TUI shape matches."""
    lines = [strip_terminal_controls(str(line)) for line in captured_lines]
    for index in range(1, len(lines) - 1):
        if not (
            _CLAUDE_CODE_HORIZONTAL_RULE_RE.fullmatch(lines[index - 1]) and _CLAUDE_CODE_HORIZONTAL_RULE_RE.fullmatch(lines[index + 1])
        ):
            continue
        match = _CLAUDE_CODE_INPUT_ROW_RE.fullmatch(lines[index])
        if match:
            return lines[index], match.group("draft")
    return None


def normalized_dispatch_text(text: str) -> str:
    """Collapse whitespace and strip terminal controls for comparison."""
    return re.sub(r"\s+", " ", strip_terminal_controls(text)).strip()


def nudge_confirmation_marker(nudge: str) -> str:
    """Return a short, normalized marker line for a nudge.

    Picks the first line that normalizes to >=8 chars, else the whole
    normalized nudge. Truncated to 160 chars (mirrors the source).
    """
    for line in nudge.splitlines():
        marker = normalized_dispatch_text(line)
        if len(marker) >= 8:
            return marker[:160]
    return normalized_dispatch_text(nudge)[:160]


def tmux_buffer_name(nudge: str) -> str:
    """Stable buffer name derived from the nudge text hash."""
    return f"chitra-nudge-{hashlib.sha256(nudge.encode('utf-8', errors='replace')).hexdigest()[:12]}"


def pane_capture_tail_hash(lines: list[str]) -> str:
    """SHA-256 of the joined pane capture, or empty string if no lines."""
    text = "\n".join(str(line) for line in lines)
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest() if text else ""


def pane_input_check(
    captured_lines: list[str],
    *,
    baseline_hash: str | None = None,
    snapshot_hash: str | None = None,
    seen_hash: str | None = None,
    extra_idle_regexes: Sequence[re.Pattern[str]] = (),
) -> PaneInputCheck:
    """Check whether a pane is idle and safe to dispatch into.

    Returns ``ok=True`` when the tail hash matches a known idle hash, the
    last line is a bare shell prompt, or a Claude Code TUI input row is empty.
    Returns ``ok=False`` with a ``blocked:`` reason otherwise — never silently
    overwrite a draft.
    """
    current_hash = pane_capture_tail_hash(captured_lines)
    if not captured_lines or not current_hash:
        return PaneInputCheck(False, "blocked: unable to verify pane input is idle", current_hash, "")
    known_idle_hashes = {str(item).strip() for item in (baseline_hash, snapshot_hash, seen_hash) if str(item or "").strip()}
    if current_hash in known_idle_hashes:
        return PaneInputCheck(True, "idle: pane capture matches known idle baseline", current_hash, "")
    claude_code_input = _claude_code_input_row(captured_lines)
    if claude_code_input is not None:
        input_line, draft = claude_code_input
        if not draft.strip():
            return PaneInputCheck(True, "idle: Claude Code TUI input row has no draft input", current_hash, input_line)
        return PaneInputCheck(False, "blocked: unsubmitted operator draft detected", current_hash, input_line)
    last_line = strip_terminal_controls(str(captured_lines[-1]))
    if _IDLE_INPUT_LINE_RE.match(last_line):
        return PaneInputCheck(True, "idle: prompt line has no draft input", current_hash, last_line)
    if any(pattern.match(last_line) for pattern in extra_idle_regexes):
        return PaneInputCheck(True, "idle: matched configured idle pattern", current_hash, last_line)
    return PaneInputCheck(False, "blocked: unsubmitted operator draft detected", current_hash, last_line)


# ---------------------------------------------------------------------------
# Pane capture
# ---------------------------------------------------------------------------


def capture_local(pane_id: str, lines: int, runner: TmuxRunner | None = None) -> list[str]:
    """Capture the tail of a local tmux pane as a list of stripped lines."""
    run = runner or run_cmd
    start = "-" if lines < 0 else f"-{lines}"
    proc = run(["tmux", "capture-pane", "-p", "-t", pane_id, "-S", start], timeout=5)
    if proc.returncode != 0:
        return []
    captured = [line.rstrip() for line in proc.stdout.splitlines() if line.strip()]
    return captured if lines < 0 else captured[-lines:]


def capture_remote(host: str, pane_id: str, lines: int, runner: TmuxRunner | None = None) -> list[str]:
    """Capture the tail of a remote tmux pane over ssh."""
    run = runner or run_cmd
    quoted_pane = shlex.quote(pane_id)
    start = "-" if lines < 0 else f"-{int(lines)}"
    cmd = ssh_command(host, f"tmux capture-pane -p -t {quoted_pane} -S {shlex.quote(start)} 2>/dev/null || true")
    proc = run(cmd, timeout=8)
    if proc.returncode != 0:
        return []
    captured = [line.rstrip() for line in proc.stdout.splitlines() if line.strip()]
    return captured if lines < 0 else captured[-lines:]


def ssh_command(target: str, remote_command: str) -> list[str]:
    """Build a BatchMode ssh command (mirrors the source, parameterized)."""
    strict_host_key_checking = _env("CHITRA_SSH_STRICT_HOST_KEY_CHECKING", "accept-new")
    timeout_raw = _env("CHITRA_SSH_CONNECT_TIMEOUT_SECONDS", "4")
    try:
        connect_timeout = int(timeout_raw)
    except ValueError as exc:
        raise ValueError("CHITRA_SSH_CONNECT_TIMEOUT_SECONDS must be a positive integer") from exc
    if connect_timeout <= 0:
        raise ValueError("CHITRA_SSH_CONNECT_TIMEOUT_SECONDS must be a positive integer")
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"StrictHostKeyChecking={strict_host_key_checking}",
        "-o",
        f"ConnectTimeout={connect_timeout}",
    ]
    config = _env("CHITRA_SSH_CONFIG")
    if config:
        cmd.extend(["-F", config])
    identity = _env("CHITRA_SSH_IDENTITY")
    if identity:
        cmd.extend(["-i", identity, "-o", "IdentitiesOnly=yes"])
    known_hosts = _env("CHITRA_SSH_KNOWN_HOSTS")
    if known_hosts:
        cmd.extend(["-o", f"UserKnownHostsFile={known_hosts}"])
    cmd.extend([target, remote_command])
    return cmd


def capture_dispatch_pane(
    host: str,
    pane: str,
    *,
    lines: int = DISPATCH_CAPTURE_LINES,
    runner: TmuxRunner | None = None,
    local_extra: set[str] | None = None,
) -> list[str]:
    """Capture a dispatch pane, locally or remotely.

    Local-host detection uses ``is_local_host`` with the supplied
    ``local_extra`` aliases (for tests).
    """
    if is_local_host(host, local_extra):
        return capture_local(pane, lines, runner=runner)
    return capture_remote(host, pane, lines, runner=runner)


# ---------------------------------------------------------------------------
# Copy-mode detection + cancel (BUG FIX (b))
# ---------------------------------------------------------------------------


def pane_in_mode(
    pane: str,
    *,
    host: str = "",
    runner: TmuxRunner | None = None,
    local_extra: set[str] | None = None,
) -> bool:
    """Return True if the target pane is in tmux copy-mode (pane_in_mode=1).

    A pane in copy-mode silently eats all input — dispatching into it
    destroys the nudge. This is bug fix (b): the source has no such check.

    ``host`` selects which tmux server is checked: the default (``""``,
    treated as local) or any host that ``is_local_host`` recognizes as this
    machine runs the check via a plain local ``tmux`` invocation; any other
    host runs the identical check over ssh via ``ssh_command``, mirroring
    ``capture_dispatch_pane``'s local/remote split. Checking the local tmux
    server for a remote target's copy-mode state is meaningless — it reports
    on the wrong tmux server entirely.
    """
    run = runner or run_cmd
    if not host or is_local_host(host, local_extra):
        cmd = ["tmux", "display-message", "-p", "-t", pane, "#{pane_in_mode}"]
    else:
        cmd = ssh_command(host, f"tmux display-message -p -t {shlex.quote(pane)} '#{{pane_in_mode}}'")
    proc = run(cmd, timeout=5)
    return proc.returncode == 0 and proc.stdout.strip() == "1"


def cancel_copy_mode(
    pane: str,
    *,
    host: str = "",
    runner: TmuxRunner | None = None,
    local_extra: set[str] | None = None,
    wait_seconds: float = PANE_IN_MODE_CANCEL_WAIT_SECONDS,
) -> bool:
    """Cancel tmux copy-mode on a pane and wait briefly.

    Returns True if a cancel command was issued. The caller should wait
    ``wait_seconds`` (default 0.3s) before injecting. ``host`` selects local
    vs ssh-wrapped execution, exactly like ``pane_in_mode``.
    """
    run = runner or run_cmd
    if not host or is_local_host(host, local_extra):
        cmd = ["tmux", "send-keys", "-t", pane, "-X", "cancel"]
    else:
        cmd = ssh_command(host, f"tmux send-keys -t {shlex.quote(pane)} -X cancel")
    proc = run(cmd, timeout=5)
    if proc.returncode != 0:
        logger.warning("cancel_copy_mode_failed", pane=pane, host=host, stderr=proc.stderr.strip())
        return False
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    return True


def ensure_pane_not_in_mode(
    pane: str,
    *,
    host: str = "",
    runner: TmuxRunner | None = None,
    local_extra: set[str] | None = None,
) -> bool:
    """Ensure a pane is not in copy-mode; cancel if it is.

    Returns True if the pane is dispatch-ready (was never in copy-mode, or
    was and is now cancelled). Returns False if copy-mode was detected and
    could not be cancelled. ``host`` is forwarded to ``pane_in_mode`` /
    ``cancel_copy_mode`` so the check runs against the actual target host
    (local or ssh-wrapped) rather than always the local tmux server.
    """
    if not pane_in_mode(pane, host=host, runner=runner, local_extra=local_extra):
        return True
    return cancel_copy_mode(pane, host=host, runner=runner, local_extra=local_extra)


# ---------------------------------------------------------------------------
# Paste commands (BUG FIX (a): -p on paste-buffer)
# ---------------------------------------------------------------------------


def paste_nudge_to_local_tmux(
    pane: str,
    nudge: str,
    *,
    runner: TmuxRunner | None = None,
    input_runner: TmuxInputRunner | None = None,
) -> subprocess.CompletedProcess[str]:
    """Inject a nudge into a local tmux pane using the verified recipe.

    Steps: ``load-buffer`` from stdin, ``paste-buffer -p`` (the ``-p`` is
    mandatory — bracketed-paste wrapper so newlines don't act as Enters),
    ``delete-buffer``, then ``send-keys Enter``.

    This is bug fix (a): the source omits ``-p`` on ``paste-buffer``.
    """
    run = runner or run_cmd
    run_in = input_runner or run_cmd_input
    buffer_name = tmux_buffer_name(nudge)
    load = run_in(["tmux", "load-buffer", "-b", buffer_name, "-"], nudge, timeout=5)
    if load.returncode != 0:
        return load
    # NOTE: -p is mandatory here. Without it, newlines act as real Enters.
    paste = run(["tmux", "paste-buffer", "-p", "-b", buffer_name, "-t", pane], timeout=5)
    if paste.returncode != 0:
        return paste
    # Buffer cleanup is housekeeping, not the critical step -- a failure here
    # must never block send-keys Enter, or a successfully pasted nudge is
    # left uncommitted in the pane (an orphaned draft, exactly the failure
    # mode this package's own draft_scanner exists to catch, caused here by
    # the dispatch path itself). Log and proceed regardless of cleanup result.
    cleanup = run(["tmux", "delete-buffer", "-b", buffer_name], timeout=5)
    if cleanup.returncode != 0:
        logger.warning("tmux_buffer_cleanup_failed", pane=pane, buffer_name=buffer_name, stderr=cleanup.stderr.strip())
    return run(["tmux", "send-keys", "-t", pane, "Enter"], timeout=5)


def remote_tmux_paste_command(pane: str, nudge: str) -> str:
    """Build the remote paste command string (ssh-safe, single shell line).

    Includes ``-p`` on ``paste-buffer`` (bug fix (a)). The command is a
    single shell string suitable for ``ssh target '<command>'``.
    """
    buffer_name = tmux_buffer_name(nudge)
    return " ".join(
        [
            "printf",
            "%s",
            shlex.quote(nudge),
            "|",
            "tmux",
            "load-buffer",
            "-b",
            shlex.quote(buffer_name),
            "-",
            "&&",
            "tmux",
            "paste-buffer",
            "-p",
            "-b",
            shlex.quote(buffer_name),
            "-t",
            shlex.quote(pane),
            "&&",
            "tmux",
            "delete-buffer",
            "-b",
            shlex.quote(buffer_name),
            "&&",
            "tmux",
            "send-keys",
            "-t",
            shlex.quote(pane),
            "Enter",
        ]
    )


# ---------------------------------------------------------------------------
# Transcript-grep verification (replaces pane-capture confirmation)
# ---------------------------------------------------------------------------


def _candidate_transcript_dirs(projects_root: Path | None = None) -> list[Path]:
    """Return candidate ``~/.claude/projects/*`` transcript directories."""
    root = projects_root if projects_root is not None else Path(_env("CHITRA_CLAUDE_PROJECTS", str(Path.home() / ".claude" / "projects")))
    if not root.is_dir():
        return []
    return [p for p in root.iterdir() if p.is_dir()]


def transcript_glob() -> str:
    """Return the configured relative transcript glob, validating its scope."""
    pattern = _env("CHITRA_TRANSCRIPT_GLOB", _TRANSCRIPT_GLOB_DEFAULT) or _TRANSCRIPT_GLOB_DEFAULT
    path = Path(pattern)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("CHITRA_TRANSCRIPT_GLOB must be relative and must not contain '..'")
    return pattern


def _read_transcript_tail(path: Path, max_bytes: int = 262144) -> str:
    """Read the tail of a JSONL transcript file (last ``max_bytes`` bytes)."""
    try:
        size = path.stat().st_size
        offset = max(0, size - max_bytes)
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            return fh.read()
    except OSError:
        return ""


def find_recent_transcript(
    marker: str,
    *,
    projects_root: Path | None = None,
    exclude_paths: set[Path] | None = None,
    recency_seconds: float = 300.0,
    now_ts: float | None = None,
) -> Path | None:
    """Find the most-recently-modified transcript containing ``marker``.

    Searches ``~/.claude/projects/*/*.jsonl`` by recency + content match,
    explicitly excluding any path in ``exclude_paths`` (the monitor's /
    dispatchd's own transcript). Returns the matching path or None.
    """
    marker_norm = normalized_dispatch_text(marker)
    if not marker_norm:
        return None
    exclude = exclude_paths or set()
    now = now_ts if now_ts is not None else time.time()
    candidates: list[tuple[float, Path]] = []
    root = projects_root if projects_root is not None else Path(_env("CHITRA_CLAUDE_PROJECTS", str(Path.home() / ".claude" / "projects")))
    for jsonl in root.glob(transcript_glob()):
        if jsonl in exclude:
            continue
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        if now - mtime > recency_seconds:
            continue
        tail = _read_transcript_tail(jsonl)
        if marker_norm in normalized_dispatch_text(tail):
            candidates.append((mtime, jsonl))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


_REMOTE_CLAUDE_PROJECTS_DEFAULT = "~/.claude/projects"


def _remote_transcript_grep_command(marker: str, root: str, recency_seconds: float, max_bytes: int = 262144) -> str:
    """Build a single ssh-safe shell script that finds the most recently
    modified ``*.jsonl`` transcript(s) under ``root`` (one level of project
    subdirectories, mirroring ``_candidate_transcript_dirs``) modified within
    ``recency_seconds``, greps each candidate's tail for ``marker``, and
    prints ``"<mtime> <path>"`` for every match. Uses ``find -mmin`` (minutes,
    portable across GNU and BSD ``find``) rather than GNU-only flags, and
    tries GNU ``stat -c`` then BSD ``stat -f`` so it works whether the remote
    host is Linux or macOS.
    """
    quoted_marker = shlex.quote(marker)
    pattern = transcript_glob()
    depth = pattern.count("/") + 1
    root_pattern = f"{root}/{pattern}"
    minutes = max(1, -(-int(recency_seconds) // 60))  # ceil division, minimum 1 minute
    return (
        f"for f in $(find {shlex.quote(root)} -mindepth {depth} -maxdepth {depth} -path {shlex.quote(root_pattern)} "
        f"-mmin -{minutes} 2>/dev/null); do "
        f'if tail -c {max_bytes} "$f" 2>/dev/null | grep -qF -- {quoted_marker}; then '
        f"stat -c '%Y %n' \"$f\" 2>/dev/null || stat -f '%m %N' \"$f\" 2>/dev/null; "
        f"fi; done"
    )


def find_recent_transcript_remote(
    host: str,
    marker: str,
    *,
    root: str | None = None,
    recency_seconds: float = 300.0,
    runner: TmuxRunner | None = None,
) -> str | None:
    """Remote counterpart to ``find_recent_transcript``: find the most
    recently modified transcript containing ``marker`` on ``host`` over ssh.

    Returns the remote path as a string (there is no local ``Path`` for it),
    or ``None`` if no match is found or the ssh call fails.
    """
    run = runner or run_cmd
    remote_root = root or _env("CHITRA_REMOTE_CLAUDE_PROJECTS", _REMOTE_CLAUDE_PROJECTS_DEFAULT)
    script = _remote_transcript_grep_command(marker, remote_root, recency_seconds)
    proc = run(ssh_command(host, script), timeout=10)
    if proc.returncode != 0:
        return None
    candidates: list[tuple[float, str]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        mtime_str, _, path = line.partition(" ")
        try:
            candidates.append((float(mtime_str), path))
        except ValueError:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def transcript_confirms_nudge(
    nudge: str,
    *,
    host: str = "",
    projects_root: Path | None = None,
    exclude_paths: set[Path] | None = None,
    recency_seconds: float = 300.0,
    now_ts: float | None = None,
    runner: TmuxRunner | None = None,
    local_extra: set[str] | None = None,
    remote_root: str | None = None,
) -> tuple[bool, Path | str | None]:
    """Return ``(confirmed, transcript_path)`` by grepping transcripts.

    Replaces the source's ``pane_capture_confirms_nudge`` — pane capture is
    weaker evidence (a spinner or status line is not confirmation).

    ``host`` selects local vs remote verification: the default (``""``,
    treated as local, preserving prior behavior for existing callers) or any
    host ``is_local_host`` recognizes searches this machine's own
    ``~/.claude/projects`` (or ``projects_root``). Any other host greps the
    **target** host's transcripts over ssh instead — a remote delivery's
    transcript lives on the remote host's filesystem, not the caller's; the
    local-only search this function used to perform would never confirm a
    genuine remote delivery.
    """
    marker = nudge_confirmation_marker(nudge)
    if host and not is_local_host(host, local_extra):
        remote_path = find_recent_transcript_remote(
            host,
            marker,
            root=remote_root,
            recency_seconds=recency_seconds,
            runner=runner,
        )
        return (remote_path is not None, remote_path)
    path = find_recent_transcript(
        marker,
        projects_root=projects_root,
        exclude_paths=exclude_paths,
        recency_seconds=recency_seconds,
        now_ts=now_ts,
    )
    return (path is not None, path)


# ---------------------------------------------------------------------------
# Liveness check (stub for the -p --resume fallback path)
# ---------------------------------------------------------------------------


def liveness_check(
    session_ref: str,
    *,
    runner: TmuxRunner | None = None,
    local_extra: set[str] | None = None,
) -> bool:
    """Return True if the lane has a LIVE attached tmux/Claude Code process.

    A live lane MUST be dispatched via the tmux-injection recipe, never via
    ``claude -p --resume``. This is the single-writer-rule guard. The actual
    ``-p --resume`` fallback path (for confirmed DETACHED/STOPPED sessions)
    is out of scope for this build — only the enforcement check is provided.

    The check inspects whether the lane's tmux session has any attached
    client — locally via a direct ``tmux list-clients`` call, or over ssh
    (mirroring ``capture_remote``/``ssh_command``) for a remote host. This
    used to unconditionally return ``True`` for any remote host ("assume
    live; the fallback path is not yet built") — that was never a real
    liveness check, just an enforcement placeholder, and remote dispatch is
    now chitra's primary path, so a real check is required here. A more
    thorough impl (scanning for a running ``claude`` process bound to the
    session id, rather than just an attached tmux client) is left for the
    fallback-path build.
    """
    run = runner or run_cmd
    parts = session_ref.split(":")
    if len(parts) != 3:
        return False
    host, session, _pane = parts
    if is_local_host(host, local_extra):
        proc = run(["tmux", "list-clients", "-t", session, "-F", "#{session_name}"], timeout=5)
    else:
        proc = run(
            ssh_command(host, f"tmux list-clients -t {shlex.quote(session)} -F '#{{session_name}}'"),
            timeout=8,
        )
    if proc.returncode != 0:
        return False
    return bool(proc.stdout.strip())


# ---------------------------------------------------------------------------
# LaneLock — single-writer enforcement per session id
# ---------------------------------------------------------------------------


class LaneLockError(RuntimeError):
    """Raised when a lane lock cannot be acquired."""


class LaneLock:
    """File-based exclusive lock for a single session id.

    One writer per session id: a lock file per session id / pane target,
    acquired before any delivery attempt, released after. Acquiring a lock
    for an already-locked session id never silently proceeds: non-blocking
    ``acquire()`` (the default) returns ``False``; blocking ``acquire()``
    raises ``LaneLockError`` after ``timeout_seconds`` — the single-writer
    rule, enforced by whichever mode the caller chooses, not by the class
    on its own. ``dispatchd`` always calls ``acquire(blocking=True, ...)``.

    Implementation: an atomic ``O_CREAT|O_EXCL`` create of a lock file. The
    file holds the acquiring pid and a timestamp for diagnostics. On
    release the file is unlinked. Stale locks (pid no longer alive) are
    reclaimed.

    This is intentionally simple and crash-safe: if the process dies, the
    lock file remains but the pid inside is dead, so the next acquirer
    reclaims it.
    """

    def __init__(self, session_ref: str, *, lock_dir: Path | str | None = None) -> None:
        self.session_ref = session_ref
        default_lock_dir = str(Path(tempfile.gettempdir()) / "chitra-locks")
        base = Path(lock_dir) if lock_dir is not None else Path(_env("CHITRA_LANE_LOCK_DIR", default_lock_dir))
        base.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_ref)
        self.lock_path = base / f"lane-{safe}.lock"
        self._acquired = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def acquire(self, *, blocking: bool = False, poll_seconds: float = 0.1, timeout_seconds: float = 5.0) -> bool:
        """Acquire the lock.

        If ``blocking`` and the lock is held by a live process, poll until
        acquired or ``timeout_seconds`` elapses (then raise
        ``LaneLockError``). If non-blocking (default), return False
        immediately if the lock is held by a live process. A stale lock
        (dead pid) is reclaimed.
        """
        deadline = time.monotonic() + timeout_seconds
        while True:
            reclaimed = self._try_reclaim_stale()
            if reclaimed:
                self._acquired = True
                return True
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if not blocking:
                    return False
                if time.monotonic() >= deadline:
                    raise LaneLockError(f"lane lock held for {self.session_ref}: {self.lock_path}") from None
                time.sleep(poll_seconds)
                continue
            payload = json.dumps({"pid": os.getpid(), "session_ref": self.session_ref, "at": datetime.now(UTC).isoformat()})
            os.write(fd, payload.encode("utf-8"))
            os.close(fd)
            self._acquired = True
            return True

    def _try_reclaim_stale(self) -> bool:
        """If the lock file exists but its pid is dead, reclaim it."""
        try:
            with self.lock_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            pid = int(data.get("pid", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if self._pid_alive(pid):
            return False
        try:
            self.lock_path.unlink()
        except OSError:
            return False
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        payload = json.dumps({"pid": os.getpid(), "session_ref": self.session_ref, "at": datetime.now(UTC).isoformat()})
        os.write(fd, payload.encode("utf-8"))
        os.close(fd)
        return True

    def release(self) -> None:
        """Release the lock if held by this instance."""
        if not self._acquired:
            return
        with contextlib.suppress(OSError):
            self.lock_path.unlink()
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    def __enter__(self) -> LaneLock:
        self.acquire(blocking=True)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()


# ---------------------------------------------------------------------------
# dispatch_to_tmux — the main entry point
# ---------------------------------------------------------------------------


def dispatch_to_tmux(
    order: DispatchOrder,
    *,
    runner: TmuxRunner | None = None,
    input_runner: TmuxInputRunner | None = None,
    local_extra: set[str] | None = None,
    allowed_hosts: set[str] | None = None,
    projects_root: Path | None = None,
    exclude_transcripts: set[Path] | None = None,
    verify_wait_seconds: float | None = None,
    tuning: DispatchTuning | None = None,
    policy: PolicyConfig | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> DispatchResult:
    """Dispatch a nudge into a tmux pane using the verified recipe.

    Pipeline:
    1. Parse ``host:session:pane`` and enforce the host allowlist.
    2. Pre-dispatch idle/draft check (``pane_input_check``) — the safety net
       from the source; never overwrite an operator draft.
    3. Copy-mode detection + cancel (bug fix (b)).
    4. Paste with ``-p`` (bug fix (a)) + send-keys Enter.
    5. Verify by transcript-grep (replaces pane-capture confirmation).

    Returns a ``DispatchResult`` with status ``sent`` / ``blocked`` / ``failed``.
    """
    run = runner or run_cmd
    run_in = input_runner or run_cmd_input
    tuning = tuning or DispatchTuning()
    if verify_wait_seconds is not None:
        tuning = DispatchTuning(
            capture_lines=tuning.capture_lines,
            post_paste_wait_seconds=verify_wait_seconds,
            transcript_recency_seconds=tuning.transcript_recency_seconds,
            lane_lock_timeout_seconds=tuning.lane_lock_timeout_seconds,
        )
    voice_patterns = (
        [re.compile(pattern, re.IGNORECASE) for pattern in policy.dispatch.banned_attribution_patterns] if policy is not None else None
    )
    extra_idle_regexes = [re.compile(pattern) for pattern in policy.dispatch.extra_idle_input_regexes] if policy is not None else ()
    parts = order.session_ref.split(":")
    if len(parts) != 3:
        return DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            routing_hint=order.routing_hint,
            task_type=order.task_type,
            status=DispatchStatus.FAILED,
            reason="unsupported session_ref (expected host:session:pane)",
        )
    host, session, pane_field = parts
    pane = tmux_pane_target(session, pane_field)

    # Directive-voice guard: reject before anything is pasted. A BLOCKED
    # voice violation must never touch the pane and must never generate a
    # delivery-ledger entry (dispatchd only signs/logs on SENT).
    bad = directive_voice_violation(order.nudge, patterns=voice_patterns)
    if bad is not None:
        logger.info("tmux_dispatch_blocked_directive_voice", session_ref=order.session_ref, phrase=bad)
        return DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            routing_hint=order.routing_hint,
            task_type=order.task_type,
            status=DispatchStatus.BLOCKED,
            reason=f"directive-voice: banned attribution phrase {bad!r}",
        )

    hosts = allowed_hosts if allowed_hosts is not None else allowed_remote_dispatch_hosts()
    if host not in hosts and not is_local_host(host, local_extra):
        return DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            routing_hint=order.routing_hint,
            task_type=order.task_type,
            status=DispatchStatus.BLOCKED,
            reason=f"remote dispatch to {host} not in allowlist",
        )

    # Pre-dispatch idle/draft check (safety net from the source).
    pre_capture = capture_dispatch_pane(host, pane, lines=tuning.capture_lines, runner=run, local_extra=local_extra)
    pre_check = pane_input_check(
        pre_capture,
        baseline_hash=order.input_baseline_hash,
        snapshot_hash=order.snapshot_tail_hash,
        seen_hash=order.input_seen_hash,
        extra_idle_regexes=extra_idle_regexes,
    )
    if not pre_check.ok:
        logger.info(
            "tmux_dispatch_blocked",
            session_ref=order.session_ref,
            reason=pre_check.reason,
            tail_hash=pre_check.tail_hash,
            last_line=pre_check.last_line,
        )
        return DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            routing_hint=order.routing_hint,
            task_type=order.task_type,
            status=DispatchStatus.BLOCKED,
            reason=pre_check.reason,
            tail_hash=pre_check.tail_hash,
        )

    # Bug fix (b): copy-mode detection + cancel, run against the actual
    # target host (local or ssh-wrapped) — checking the local tmux server
    # for a remote target's copy-mode state would report on the wrong tmux
    # server entirely.
    if not ensure_pane_not_in_mode(pane, host=host, runner=run, local_extra=local_extra):
        return DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            routing_hint=order.routing_hint,
            task_type=order.task_type,
            status=DispatchStatus.BLOCKED,
            reason="blocked: pane in copy-mode and cancel failed",
        )

    # Bug fix (a): paste-buffer -p.
    if is_local_host(host, local_extra):
        proc = paste_nudge_to_local_tmux(pane, order.nudge, runner=run, input_runner=run_in)
        if proc.returncode != 0:
            return DispatchResult(
                order_id=order.order_id,
                session_ref=order.session_ref,
                routing_hint=order.routing_hint,
                task_type=order.task_type,
                status=DispatchStatus.FAILED,
                reason=proc.stderr.strip() or proc.stdout.strip() or f"tmux paste-buffer failed rc={proc.returncode}",
            )
    else:
        remote_cmd = remote_tmux_paste_command(pane, order.nudge)
        proc = run(ssh_command(host, remote_cmd), timeout=10)
        if proc.returncode != 0:
            return DispatchResult(
                order_id=order.order_id,
                session_ref=order.session_ref,
                routing_hint=order.routing_hint,
                task_type=order.task_type,
                status=DispatchStatus.FAILED,
                reason=proc.stderr.strip() or proc.stdout.strip() or f"remote tmux paste-buffer failed rc={proc.returncode}",
            )

    sleep(tuning.post_paste_wait_seconds)

    # Transcript-grep verification (replaces pane-capture confirmation).
    # host-aware: for a remote target, the delivered nudge lands in a
    # transcript on the remote host, not the local one, so verification
    # must run over ssh against that host — see transcript_confirms_nudge.
    confirmed, transcript_path = transcript_confirms_nudge(
        order.nudge,
        host=host,
        projects_root=projects_root,
        exclude_paths=exclude_transcripts,
        recency_seconds=tuning.transcript_recency_seconds,
        runner=run,
        local_extra=local_extra,
    )
    marker = nudge_confirmation_marker(order.nudge)
    if confirmed:
        logger.info(
            "tmux_dispatch_sent",
            session_ref=order.session_ref,
            marker=marker,
            transcript=str(transcript_path),
        )
        return DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            routing_hint=order.routing_hint,
            task_type=order.task_type,
            status=DispatchStatus.SENT,
            reason="sent: confirmed via transcript-grep",
            marker=marker,
            transcript_path=str(transcript_path) if transcript_path is not None else None,
        )
    logger.info(
        "tmux_dispatch_unverified",
        session_ref=order.session_ref,
        marker=marker,
    )
    return DispatchResult(
        order_id=order.order_id,
        session_ref=order.session_ref,
        routing_hint=order.routing_hint,
        task_type=order.task_type,
        status=DispatchStatus.FAILED,
        reason="send-failed-no-confirmation (transcript-grep found no marker)",
        marker=marker,
    )
