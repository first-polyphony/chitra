"""watchd — deterministic tmux-pane change emitter for ``chitra.triaged``.

The events log remains a small wire contract consumed by ``chitra.triaged``.
At a detected turn-end, this watcher also forces the deterministic completion
boundary. Completion claims launch isolated watched-session reviewers against
the lane's frozen goal; ordinary turns do not. Review metadata is written only
to Chitra-owned ledgers and never to pane text.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import re
import signal
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import structlog

from chitra.completion_gate import (
    CompletionReviewRecord,
    append_completion_review,
    evaluate_turn_end,
    extract_completion_evidence,
    is_completion_claim,
)
from chitra.goal_enforcement import (
    BehaviorReviewer,
    ClaudeProcessReviewer,
    GoalReviewError,
    WatchedSessionBehavior,
    review_watched_session,
)
from chitra.goals import GoalStatus, add_ask, list_goals, update_now
from chitra.policy_config import load_policy_config
from chitra.state_paths import state_dir as default_state_dir
from chitra.taxonomy import load_taxonomy

logger = structlog.get_logger(__name__)

EVENT_LOG_ENV_VAR = "CHITRA_WATCHD_EVENT_LOG"
INTERVAL_ENV_VAR = "CHITRA_WATCHD_INTERVAL"
PANES_ENV_VAR = "CHITRA_WATCHD_PANES"
SESSION_PREFIXES_ENV_VAR = "CHITRA_WATCHD_SESSION_PREFIXES"
EXCLUDED_SESSION_PREFIXES_ENV_VAR = "CHITRA_WATCHD_EXCLUDE_SESSION_PREFIXES"
MAX_LOG_BYTES_ENV_VAR = "CHITRA_WATCHD_MAX_LOG_BYTES"
REVIEWER_COUNT_ENV_VAR = "CHITRA_WATCHD_REVIEWER_COUNT"
REVIEWER_COMMAND_ENV_VAR = "CHITRA_WATCHD_REVIEWER_COMMAND"
REVIEWER_MODEL_ENV_VAR = "CHITRA_WATCHD_REVIEWER_MODEL"
DEFAULT_INTERVAL_SECONDS = 5.0
DEFAULT_MAX_LOG_BYTES = 5 * 1024 * 1024
DEFAULT_REVIEWER_COUNT = 2
DEFAULT_REVIEWER_COMMAND = "claude"
DEFAULT_REVIEWER_MODEL = "claude-haiku-4-5"
CAPTURE_LINES = 60
NORMALIZED_TAIL_LINES = 25

_VOLATILE_LINE_RE = re.compile(
    r"^[\s]*[·✻✽✳✢✶*●○◐◯]|tokens\b|🪟|⏵⏵|esc to interrupt|ctrl\+b|^─+$|^[\s]*$|Press up to edit|globalVersion: [0-9.]+"
)
_TIMING_CHROME_RE = re.compile(r"\([0-9]+m? ?[0-9]*s?[^)]*\)")
_ACTIVE_TURN_RE = re.compile(r"esc to interrupt|thinking|working…|working\.\.\.|running…|running\.\.\.", re.I)

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class Pane:
    """A live tmux pane, identified by the server-unique ``pane_id``."""

    pane_id: str
    target: str


@dataclass(frozen=True, slots=True)
class WatchdConfig:
    state_dir: Path
    events_log: Path
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    panes_override: tuple[str, ...] | None = None
    session_prefixes: tuple[str, ...] | None = None
    excluded_session_prefixes: tuple[str, ...] = ()
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES
    goals_root: Path | None = None
    completion_review_log: Path | None = None
    reviewer_count: int = DEFAULT_REVIEWER_COUNT
    reviewer_command: str = DEFAULT_REVIEWER_COMMAND
    reviewer_model: str | None = DEFAULT_REVIEWER_MODEL

    def __post_init__(self) -> None:
        if self.reviewer_count < 1:
            raise ValueError("reviewer_count must be a positive integer")


def normalize(content: str) -> list[str]:
    """Remove volatile pane chrome and the live operator input box.

    The final line beginning with ``❯`` starts the active input box.  It and
    everything below it are intentionally excluded so an operator typing in a
    pane cannot look like a lane state transition.
    """
    lines = content.splitlines()
    prompt_indices = [index for index, line in enumerate(lines) if line.startswith("❯")]
    if prompt_indices:
        lines = lines[: prompt_indices[-1]]

    normalized: list[str] = []
    for line in lines:
        if _VOLATILE_LINE_RE.search(line):
            continue
        line = _TIMING_CHROME_RE.sub("", line).rstrip()
        if line:
            normalized.append(line)
    return normalized


def normalized_snapshot(content: str) -> tuple[str, list[str]]:
    """Return the stable digest and retained normalized tail for a capture."""
    tail = normalize(content)[-NORMALIZED_TAIL_LINES:]
    text = "\n".join(tail)
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(), tail


def pane_turn_finished(content: str) -> bool:
    """Recognize a stable input prompt after a completed lane turn."""
    lines = content.splitlines()
    has_prompt = any(line.lstrip().startswith("❯") for line in lines)
    active = any(_ACTIVE_TURN_RE.search(line) for line in lines[-12:])
    return has_prompt and not active and bool(normalize(content))


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def list_panes(
    *,
    runner: CommandRunner = _run_command,
    panes_override: Sequence[str] | None = None,
    session_prefixes: Sequence[str] | None = None,
    excluded_session_prefixes: Sequence[str] = (),
) -> list[Pane]:
    """Enumerate live tmux panes, deduplicated by server-assigned pane ID.

    ``panes_override`` is only for controlled tests or deployments that need
    to restrict observation temporarily; normal operation always uses
    ``tmux list-panes -a``. ``session_prefixes`` narrows live discovery to
    names beginning with one of the supplied prefixes; an empty value keeps
    the historical all-session behavior. ``excluded_session_prefixes`` wins
    over inclusion, so a broad legacy observer can explicitly leave an
    isolated instance's namespace alone.
    """
    if panes_override is not None:
        return [Pane(pane_id=target, target=target) for target in dict.fromkeys(panes_override) if target]

    included = tuple(prefix.strip() for prefix in (session_prefixes or ()) if prefix.strip())
    excluded = tuple(prefix.strip() for prefix in excluded_session_prefixes if prefix.strip())

    result = runner(["tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}"])
    if result.returncode != 0:
        logger.warning("watchd_list_panes_failed", stderr=result.stderr.strip())
        return []

    panes: list[Pane] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        pane_id, separator, target = line.partition("\t")
        if not separator or not pane_id or not target or pane_id in seen:
            continue
        session_name, _separator, _pane = target.partition(":")
        if included and not any(session_name.startswith(prefix) for prefix in included):
            continue
        if any(session_name.startswith(prefix) for prefix in excluded):
            continue
        seen.add(pane_id)
        panes.append(Pane(pane_id=pane_id, target=target))
    return panes


def capture_pane(pane: Pane, *, runner: CommandRunner = _run_command) -> str | None:
    """Capture one pane, returning ``None`` when it vanished or tmux failed."""
    result = runner(["tmux", "capture-pane", "-p", "-J", "-t", pane.target, "-S", f"-{CAPTURE_LINES}"])
    if result.returncode != 0:
        logger.info("watchd_capture_failed", pane_id=pane.pane_id, stderr=result.stderr.strip())
        return None
    return result.stdout


def event_line(lane_id: str, normalized_tail: Sequence[str], *, now: datetime | None = None) -> str:
    """Format one event exactly as ``triaged.parse_event_line`` consumes it."""
    timestamp = (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    text = "CHANGE DETECTED: " + " | ".join(normalized_tail)
    return f"{timestamp} {lane_id} {text}\n"


def append_event(event_log: Path, line: str, *, max_log_bytes: int = DEFAULT_MAX_LOG_BYTES) -> None:
    """Append under an exclusive lock, rotating the legacy-sized log first."""
    event_log.parent.mkdir(parents=True, exist_ok=True)
    lock_path = event_log.with_name(event_log.name + ".lock")
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            if event_log.exists() and event_log.stat().st_size >= max_log_bytes:
                event_log.replace(event_log.with_name(event_log.name + ".1"))
            with event_log.open("a", encoding="utf-8") as output:
                output.write(line)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@dataclass(slots=True)
class Watchd:
    """In-memory per-pane baselines for one long-lived watcher process."""

    config: WatchdConfig
    runner: CommandRunner = _run_command
    reviewer: BehaviorReviewer | None = None
    baselines: dict[str, str] = field(default_factory=dict)
    reviewed_turns: set[tuple[str, str]] = field(default_factory=set)

    def _raw_capture_path(self, pane_id: str) -> Path:
        """Return a filesystem-safe diagnostic capture path for one pane."""
        safe_id = hashlib.sha256(pane_id.encode("utf-8")).hexdigest()
        return self.config.state_dir / "watchd" / f"{safe_id}.raw"

    def _save_raw_capture(self, pane_id: str, content: str) -> None:
        raw_path = self._raw_capture_path(pane_id)
        try:
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            logger.warning("watchd_raw_capture_write_failed", pane_id=pane_id, path=str(raw_path), error=str(exc))

    def _session_ref(self, pane: Pane) -> str | None:
        root = self.config.goals_root or self.config.state_dir
        suffix = f":{pane.target}"
        matches = [record.session_ref for record in list_goals(root) if record.session_ref.endswith(suffix)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning("watchd_ambiguous_goal_mapping", pane_id=pane.pane_id, target=pane.target, matches=matches)
        return None

    def _review_turn_end(self, pane: Pane, content: str) -> None:
        """Force the completion/direction gate and persist only our-side detail."""
        text = "\n".join(normalize(content)).strip()
        behavior_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        key = (pane.pane_id, behavior_sha256)
        if key in self.reviewed_turns:
            return
        self.reviewed_turns.add(key)
        root = self.config.goals_root or self.config.state_dir
        review_log = self.config.completion_review_log or self.config.state_dir / "completion_reviews.jsonl"
        session_ref = self._session_ref(pane)
        if session_ref is None:
            append_completion_review(
                review_log,
                CompletionReviewRecord(
                    session_ref=pane.target,
                    pane_id=pane.pane_id,
                    behavior_sha256=behavior_sha256,
                    condition="turn_end_without_completion_claim",
                    review_verdict="unavailable",
                    status="untracked",
                    summary="turn-end review failed closed: no unique frozen goal maps to this pane",
                ),
            )
            return

        goal = next(record for record in list_goals(root) if record.session_ref == session_ref)
        policy = load_policy_config().completion_gate
        turn_audit = evaluate_turn_end(
            text,
            todo_items=[],
            evidence=extract_completion_evidence(text),
            taxonomy=load_taxonomy(policy.taxonomy_path),
            policy=policy,
            open_asks=goal.open_asks,
            blockers=(goal.needs,) if goal.needs else (),
        )
        review_signal = None
        review_error = ""
        if is_completion_claim(text):
            behavior = WatchedSessionBehavior.from_turn(session_ref, text)
            reviewer = self.reviewer or ClaudeProcessReviewer(
                command=self.config.reviewer_command,
                model=self.config.reviewer_model,
            )
            try:
                review_signal = review_watched_session(
                    root,
                    session_ref,
                    behavior,
                    reviewer=reviewer,
                    reviewer_count=self.config.reviewer_count,
                )
            except (GoalReviewError, OSError, ValueError) as exc:
                review_error = str(exc)

        completion_verdict = turn_audit.completion.verdict if turn_audit.completion is not None else None
        ask = ""
        if turn_audit.condition == "turn_end_without_completion_claim":
            status: GoalStatus = "turn-finished-unverified"
            summary = f"{turn_audit.summary}; no completion claim, so isolated review was not run"
            review_verdict: Literal["accept", "reject", "unavailable"] = "unavailable"
        elif review_signal is None:
            status = "blocked"
            summary = f"turn-end review unavailable: {review_error}"
            ask = "Review the lane manually because isolated watched-session review could not complete."
            review_verdict = "unavailable"
        elif review_signal.verdict == "reject":
            status = "blocked"
            summary = "watched-session direction or completion posture was rejected against the frozen goal"
            ask = "Review the lane's rejected direction or completion posture against its frozen goal."
            review_verdict = "reject"
        elif completion_verdict == "CLEAN":
            status = "done-pending-close"
            summary = turn_audit.summary
            review_verdict = "accept"
        else:
            status = "completion-disputed"
            summary = turn_audit.summary
            ask = "Resolve the cited completion-gate gaps before treating this lane as complete."
            review_verdict = "accept"

        update_now(
            root,
            session_ref,
            now=summary,
            status=status,
            last_verified=datetime.now(UTC).isoformat() if status == "done-pending-close" else goal.last_verified,
        )
        if ask:
            add_ask(root, session_ref, ask)
        append_completion_review(
            review_log,
            CompletionReviewRecord(
                session_ref=session_ref,
                pane_id=pane.pane_id,
                behavior_sha256=behavior_sha256,
                condition=turn_audit.condition,
                completion_verdict=completion_verdict,
                review_signal_id=review_signal.signal_id if review_signal is not None else None,
                review_verdict=review_verdict,
                status=status,
                summary=summary,
            ),
        )

    def poll_once(self) -> int:
        """Capture all current panes and emit an event for each real change."""
        emitted = 0
        for pane in list_panes(
            runner=self.runner,
            panes_override=self.config.panes_override,
            session_prefixes=self.config.session_prefixes,
            excluded_session_prefixes=self.config.excluded_session_prefixes,
        ):
            content = capture_pane(pane, runner=self.runner)
            if content is None:
                continue
            self._save_raw_capture(pane.pane_id, content)
            digest, tail = normalized_snapshot(content)
            if pane_turn_finished(content):
                self._review_turn_end(pane, content)
            previous = self.baselines.get(pane.pane_id)
            if previous is None:
                self.baselines[pane.pane_id] = digest
                continue
            if previous == digest:
                continue
            append_event(self.config.events_log, event_line(pane.pane_id, tail), max_log_bytes=self.config.max_log_bytes)
            self.baselines[pane.pane_id] = digest
            emitted += 1
        return emitted


def _env_value(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _positive_float(value: str, *, name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive number")
    return parsed


def _positive_int(value: str, *, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _split_prefixes(value: str | None) -> tuple[str, ...]:
    """Normalize a comma-separated namespace filter without inventing values."""
    if not value:
        return ()
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def resolve_config(
    *,
    state_dir: Path | None = None,
    events_log: Path | None = None,
    interval_seconds: float | None = None,
    panes_override: Sequence[str] | None = None,
    session_prefixes: Sequence[str] | None = None,
    excluded_session_prefixes: Sequence[str] | None = None,
    max_log_bytes: int | None = None,
    reviewer_count: int | None = None,
    reviewer_command: str | None = None,
    reviewer_model: str | None = None,
) -> WatchdConfig:
    """Resolve CLI values, then ``CHITRA_*`` overrides, then generic defaults."""
    configured_state_dir = state_dir or default_state_dir()
    configured_events_log = events_log or Path(_env_value(EVENT_LOG_ENV_VAR) or configured_state_dir / "events.log")
    configured_interval = interval_seconds
    if configured_interval is None:
        raw_interval = _env_value(INTERVAL_ENV_VAR)
        configured_interval = _positive_float(raw_interval, name=INTERVAL_ENV_VAR) if raw_interval else DEFAULT_INTERVAL_SECONDS
    if configured_interval <= 0:
        raise ValueError("interval_seconds must be a positive number")
    configured_max_log_bytes = max_log_bytes
    if configured_max_log_bytes is None:
        raw_max_log_bytes = _env_value(MAX_LOG_BYTES_ENV_VAR)
        configured_max_log_bytes = (
            _positive_int(raw_max_log_bytes, name=MAX_LOG_BYTES_ENV_VAR) if raw_max_log_bytes else DEFAULT_MAX_LOG_BYTES
        )
    if configured_max_log_bytes <= 0:
        raise ValueError("max_log_bytes must be a positive integer")
    configured_panes = panes_override
    if configured_panes is None:
        raw_panes = _env_value(PANES_ENV_VAR)
        configured_panes = tuple(item.strip() for item in raw_panes.split(",") if item.strip()) if raw_panes else None
    configured_session_prefixes = (
        tuple(prefix.strip() for prefix in session_prefixes if prefix.strip())
        if session_prefixes is not None
        else _split_prefixes(_env_value(SESSION_PREFIXES_ENV_VAR))
    )
    configured_excluded_session_prefixes = (
        tuple(prefix.strip() for prefix in excluded_session_prefixes if prefix.strip())
        if excluded_session_prefixes is not None
        else _split_prefixes(_env_value(EXCLUDED_SESSION_PREFIXES_ENV_VAR))
    )
    configured_reviewer_count = reviewer_count
    if configured_reviewer_count is None:
        raw_reviewer_count = _env_value(REVIEWER_COUNT_ENV_VAR)
        configured_reviewer_count = (
            _positive_int(raw_reviewer_count, name=REVIEWER_COUNT_ENV_VAR)
            if raw_reviewer_count
            else DEFAULT_REVIEWER_COUNT
        )
    if configured_reviewer_count < 1:
        raise ValueError("reviewer_count must be a positive integer")
    configured_reviewer_command = (
        _env_value(REVIEWER_COMMAND_ENV_VAR) or DEFAULT_REVIEWER_COMMAND
        if reviewer_command is None
        else reviewer_command.strip()
    )
    if not configured_reviewer_command:
        raise ValueError("reviewer_command must be non-empty")
    configured_reviewer_model = (
        _env_value(REVIEWER_MODEL_ENV_VAR) or DEFAULT_REVIEWER_MODEL if reviewer_model is None else reviewer_model.strip()
    )
    if not configured_reviewer_model:
        raise ValueError("reviewer_model must be non-empty")
    return WatchdConfig(
        state_dir=configured_state_dir,
        events_log=configured_events_log,
        interval_seconds=configured_interval,
        panes_override=tuple(configured_panes) if configured_panes is not None else None,
        session_prefixes=configured_session_prefixes or None,
        excluded_session_prefixes=configured_excluded_session_prefixes,
        max_log_bytes=configured_max_log_bytes,
        reviewer_count=configured_reviewer_count,
        reviewer_command=configured_reviewer_command,
        reviewer_model=configured_reviewer_model,
    )


def run_forever(watchd: Watchd, *, stop_event: threading.Event | None = None) -> None:
    """Run until a SIGTERM/SIGINT handler (or caller) requests a clean stop."""
    stop_event = stop_event or threading.Event()
    logger.info("watchd_started", events_log=str(watchd.config.events_log), interval_seconds=watchd.config.interval_seconds)
    while not stop_event.is_set():
        watchd.poll_once()
        stop_event.wait(watchd.config.interval_seconds)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="watchd", description="Deterministic tmux-pane change emitter for triaged.")
    parser.add_argument("--state-dir", type=Path, default=None, help="Watcher state root (default: CHITRA_STATE_DIR or /var/lib/chitra).")
    parser.add_argument(
        "--events-log", type=Path, default=None, help="Events log (default: CHITRA_WATCHD_EVENT_LOG or <state-dir>/events.log)."
    )
    parser.add_argument("--interval-seconds", type=float, default=None, help="Poll interval (default: CHITRA_WATCHD_INTERVAL or 5).")
    parser.add_argument(
        "--panes", default=None, help="Comma-separated tmux targets for a controlled override (default: live tmux enumeration)."
    )
    parser.add_argument(
        "--session-prefix",
        action="append",
        default=None,
        help="Observe only tmux sessions with this prefix (repeatable; default: CHITRA_WATCHD_SESSION_PREFIXES).",
    )
    parser.add_argument(
        "--exclude-session-prefix",
        action="append",
        default=None,
        help="Never observe tmux sessions with this prefix (repeatable; default: CHITRA_WATCHD_EXCLUDE_SESSION_PREFIXES).",
    )
    parser.add_argument(
        "--max-log-bytes", type=int, default=None, help="Rotate at this size (default: CHITRA_WATCHD_MAX_LOG_BYTES or 5 MiB)."
    )
    parser.add_argument(
        "--reviewer-count",
        type=int,
        default=None,
        help="Reviewers in the normal completion-claim round (default: CHITRA_WATCHD_REVIEWER_COUNT or 2).",
    )
    parser.add_argument(
        "--reviewer-command",
        default=None,
        help="Isolated reviewer command (default: CHITRA_WATCHD_REVIEWER_COMMAND or claude).",
    )
    parser.add_argument(
        "--reviewer-model",
        default=None,
        help="Pinned isolated reviewer model (default: CHITRA_WATCHD_REVIEWER_MODEL or claude-haiku-4-5).",
    )
    parser.add_argument("--once", action="store_true", help="Capture and compare once, then exit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    panes_override = tuple(item.strip() for item in args.panes.split(",") if item.strip()) if args.panes is not None else None
    config = resolve_config(
        state_dir=args.state_dir,
        events_log=args.events_log,
        interval_seconds=args.interval_seconds,
        panes_override=panes_override,
        session_prefixes=args.session_prefix,
        excluded_session_prefixes=args.exclude_session_prefix,
        max_log_bytes=args.max_log_bytes,
        reviewer_count=args.reviewer_count,
        reviewer_command=args.reviewer_command,
        reviewer_model=args.reviewer_model,
    )
    watcher = Watchd(config)
    if args.once:
        print(f"{{\"emitted\": {watcher.poll_once()}}}")
        return 0

    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run_forever(watcher, stop_event=stop_event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
