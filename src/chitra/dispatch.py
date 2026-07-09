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
    runs ``tmux send-keys -X cancel`` and waits ~0.3s before injecting.

Post-send verification uses transcript-grep against the target session's
own ``~/.claude/projects/*/*.jsonl`` transcript (found by recency + content
match, explicitly excluding the caller's own transcript), replacing the
weaker pane-capture confirmation: a spinner or status line is not evidence
that a message was actually received; the transcript is.

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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import structlog
from pydantic import BaseModel, Field

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


# ---------------------------------------------------------------------------
# Pydantic boundary models
# ---------------------------------------------------------------------------


class DispatchStatus(enum.StrEnum):
    """Outcome of a dispatch attempt."""

    SENT = "sent"
    BLOCKED = "blocked"
    FAILED = "failed"


class DispatchOrder(BaseModel):
    """A dispatch order consumed by ``dispatchd``.

    ``session_ref`` uses the ``host:session:pane`` convention from the
    source. ``nudge`` is the verbatim text to inject. ``order_id`` is the
    caller-supplied unique id used for result-file naming. ``tag`` marks the
    message's authenticity class in the delivery ledger — ``"[C]"`` (chitra
    relay) is the default; a caller relaying verbatim operator-typed text
    with no relay in between may use a different tag, but the ledger records
    whatever tag is asserted so it can be audited later.
    """

    order_id: str
    session_ref: str
    nudge: str
    tag: str = "[C]"
    input_baseline_hash: str | None = None
    input_seen_hash: str | None = None
    snapshot_tail_hash: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class DispatchResult(BaseModel):
    """Result of processing a dispatch order."""

    order_id: str
    session_ref: str
    status: DispatchStatus
    reason: str = ""
    marker: str = ""
    tail_hash: str = ""
    transcript_path: str | None = None
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
    override = _env("POLYPHONY_CHITRA_LOCAL_HOST")
    if override:
        aliases.add(override.split(".", 1)[0])
    if extra:
        aliases |= extra
    return aliases


def is_local_host(host: str, extra: set[str] | None = None) -> bool:
    """Return True if ``host`` refers to the local machine."""
    return host.split(".", 1)[0] in local_host_aliases(extra)


# ---------------------------------------------------------------------------
# Text normalization helpers (mirrors of the source)
# ---------------------------------------------------------------------------


def strip_terminal_controls(text: str) -> str:
    """Strip ANSI escape sequences and surrounding whitespace."""
    return _ANSI_ESCAPE_RE.sub("", text).strip()


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
) -> PaneInputCheck:
    """Check whether a pane is idle and safe to dispatch into.

    Returns ``ok=True`` when the tail hash matches a known idle hash or the
    last line is a bare prompt with no draft. Returns ``ok=False`` with a
    ``blocked:`` reason otherwise — never silently overwrite a draft.
    """
    current_hash = pane_capture_tail_hash(captured_lines)
    if not captured_lines or not current_hash:
        return PaneInputCheck(False, "blocked: unable to verify pane input is idle", current_hash, "")
    known_idle_hashes = {str(item).strip() for item in (baseline_hash, snapshot_hash, seen_hash) if str(item or "").strip()}
    if current_hash in known_idle_hashes:
        return PaneInputCheck(True, "idle: pane capture matches known idle baseline", current_hash, "")
    last_line = strip_terminal_controls(str(captured_lines[-1]))
    if _IDLE_INPUT_LINE_RE.match(last_line):
        return PaneInputCheck(True, "idle: prompt line has no draft input", current_hash, last_line)
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
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=4",
    ]
    config = _env("POLYPHONY_CHITRA_SSH_CONFIG")
    if config:
        cmd.extend(["-F", config])
    identity = _env("POLYPHONY_CHITRA_SSH_IDENTITY")
    if identity:
        cmd.extend(["-i", identity, "-o", "IdentitiesOnly=yes"])
    known_hosts = _env("POLYPHONY_CHITRA_SSH_KNOWN_HOSTS")
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
    runner: TmuxRunner | None = None,
) -> bool:
    """Return True if the target pane is in tmux copy-mode (pane_in_mode=1).

    A pane in copy-mode silently eats all input — dispatching into it
    destroys the nudge. This is bug fix (b): the source has no such check.
    """
    run = runner or run_cmd
    proc = run(["tmux", "display-message", "-p", "-t", pane, "#{pane_in_mode}"], timeout=5)
    return proc.returncode == 0 and proc.stdout.strip() == "1"


def cancel_copy_mode(
    pane: str,
    *,
    runner: TmuxRunner | None = None,
    wait_seconds: float = PANE_IN_MODE_CANCEL_WAIT_SECONDS,
) -> bool:
    """Cancel tmux copy-mode on a pane and wait briefly.

    Returns True if a cancel command was issued. The caller should wait
    ``wait_seconds`` (default 0.3s) before injecting.
    """
    run = runner or run_cmd
    proc = run(["tmux", "send-keys", "-t", pane, "-X", "cancel"], timeout=5)
    if proc.returncode != 0:
        logger.warning("cancel_copy_mode_failed", pane=pane, stderr=proc.stderr.strip())
        return False
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    return True


def ensure_pane_not_in_mode(
    pane: str,
    *,
    runner: TmuxRunner | None = None,
) -> bool:
    """Ensure a pane is not in copy-mode; cancel if it is.

    Returns True if the pane is dispatch-ready (was never in copy-mode, or
    was and is now cancelled). Returns False if copy-mode was detected and
    could not be cancelled.
    """
    if not pane_in_mode(pane, runner=runner):
        return True
    return cancel_copy_mode(pane, runner=runner)


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
    cleanup = run(["tmux", "delete-buffer", "-b", buffer_name], timeout=5)
    if paste.returncode != 0:
        return paste
    if cleanup.returncode != 0:
        return cleanup
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
    if projects_root is not None:
        root = projects_root
    else:
        root = Path(_env("POLYPHONY_CHITRA_CLAUDE_PROJECTS", str(Path.home() / ".claude" / "projects")))
    if not root.is_dir():
        return []
    return [p for p in root.iterdir() if p.is_dir()]


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
    for d in _candidate_transcript_dirs(projects_root):
        for jsonl in d.glob("*.jsonl"):
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


def transcript_confirms_nudge(
    nudge: str,
    *,
    projects_root: Path | None = None,
    exclude_paths: set[Path] | None = None,
    recency_seconds: float = 300.0,
    now_ts: float | None = None,
) -> tuple[bool, Path | None]:
    """Return ``(confirmed, transcript_path)`` by grepping transcripts.

    Replaces the source's ``pane_capture_confirms_nudge`` — pane capture is
    weaker evidence (a spinner or status line is not confirmation).
    """
    marker = nudge_confirmation_marker(nudge)
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
    is out of scope for this build — only the enforcement stub is provided.

    The check inspects whether the lane's tmux session has any attached
    client. A more thorough impl (scanning for a running ``claude`` process
    bound to the session id) is left for the fallback-path build.
    """
    run = runner or run_cmd
    parts = session_ref.split(":")
    if len(parts) != 3:
        return False
    host, session, _pane = parts
    if not is_local_host(host, local_extra):
        return True  # remote: assume live; the fallback path is not yet built
    proc = run(["tmux", "list-clients", "-t", session, "-F", "#{session_name}"], timeout=5)
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
    for an already-locked session id fails (raises ``LaneLockError``) rather
    than silently proceeding — the single-writer rule.

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
        default_lock_dir = str(Path(tempfile.gettempdir()) / "polyphony-chitra-locks")
        base = Path(lock_dir) if lock_dir is not None else Path(_env("POLYPHONY_CHITRA_LANE_LOCK_DIR", default_lock_dir))
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
    verify_wait_seconds: float = DISPATCH_VERIFY_WAIT_SECONDS,
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
    parts = order.session_ref.split(":")
    if len(parts) != 3:
        return DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            status=DispatchStatus.FAILED,
            reason="unsupported session_ref (expected host:session:pane)",
        )
    host, _session, pane = parts
    hosts = allowed_hosts if allowed_hosts is not None else allowed_remote_dispatch_hosts()
    if host not in hosts and not is_local_host(host, local_extra):
        return DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            status=DispatchStatus.BLOCKED,
            reason=f"remote dispatch to {host} not in allowlist",
        )

    # Pre-dispatch idle/draft check (safety net from the source).
    pre_capture = capture_dispatch_pane(host, pane, runner=run, local_extra=local_extra)
    pre_check = pane_input_check(
        pre_capture,
        baseline_hash=order.input_baseline_hash,
        snapshot_hash=order.snapshot_tail_hash,
        seen_hash=order.input_seen_hash,
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
            status=DispatchStatus.BLOCKED,
            reason=pre_check.reason,
            tail_hash=pre_check.tail_hash,
        )

    # Bug fix (b): copy-mode detection + cancel.
    if not ensure_pane_not_in_mode(pane, runner=run):
        return DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
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
                status=DispatchStatus.FAILED,
                reason=proc.stderr.strip() or proc.stdout.strip() or f"remote tmux paste-buffer failed rc={proc.returncode}",
            )

    sleep(verify_wait_seconds)

    # Transcript-grep verification (replaces pane-capture confirmation).
    confirmed, transcript_path = transcript_confirms_nudge(
        order.nudge,
        projects_root=projects_root,
        exclude_paths=exclude_transcripts,
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
        status=DispatchStatus.FAILED,
        reason="send-failed-no-confirmation (transcript-grep found no marker)",
        marker=marker,
    )
