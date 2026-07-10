"""watchd — deterministic tmux-pane change emitter for ``chitra.triaged``.

The events log is intentionally a small wire contract: each change is one
``<ISO8601> <LANE_ID> <TEXT>`` line, which is consumed directly by
``chitra.triaged``.  This module only observes panes; it never interprets
their contents or invokes an LLM.
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

import structlog

from chitra.state_paths import state_dir as default_state_dir

logger = structlog.get_logger(__name__)

EVENT_LOG_ENV_VAR = "CHITRA_WATCHD_EVENT_LOG"
INTERVAL_ENV_VAR = "CHITRA_WATCHD_INTERVAL"
PANES_ENV_VAR = "CHITRA_WATCHD_PANES"
MAX_LOG_BYTES_ENV_VAR = "CHITRA_WATCHD_MAX_LOG_BYTES"
DEFAULT_INTERVAL_SECONDS = 5.0
DEFAULT_MAX_LOG_BYTES = 5 * 1024 * 1024
CAPTURE_LINES = 60
NORMALIZED_TAIL_LINES = 25

_VOLATILE_LINE_RE = re.compile(
    r"^[\s]*[·✻✽✳✢✶*●○◐◯]|tokens\b|🪟|⏵⏵|esc to interrupt|ctrl\+b|^─+$|^[\s]*$|Press up to edit|globalVersion: [0-9.]+"
)
_TIMING_CHROME_RE = re.compile(r"\([0-9]+m? ?[0-9]*s?[^)]*\)")

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
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES


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


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def list_panes(*, runner: CommandRunner = _run_command, panes_override: Sequence[str] | None = None) -> list[Pane]:
    """Enumerate live tmux panes, deduplicated by server-assigned pane ID.

    ``panes_override`` is only for controlled tests or deployments that need
    to restrict observation temporarily; normal operation always uses
    ``tmux list-panes -a``.
    """
    if panes_override is not None:
        return [Pane(pane_id=target, target=target) for target in dict.fromkeys(panes_override) if target]

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
    baselines: dict[str, str] = field(default_factory=dict)

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

    def poll_once(self) -> int:
        """Capture all current panes and emit an event for each real change."""
        emitted = 0
        for pane in list_panes(runner=self.runner, panes_override=self.config.panes_override):
            content = capture_pane(pane, runner=self.runner)
            if content is None:
                continue
            self._save_raw_capture(pane.pane_id, content)
            digest, tail = normalized_snapshot(content)
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


def resolve_config(
    *,
    state_dir: Path | None = None,
    events_log: Path | None = None,
    interval_seconds: float | None = None,
    panes_override: Sequence[str] | None = None,
    max_log_bytes: int | None = None,
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
    return WatchdConfig(
        state_dir=configured_state_dir,
        events_log=configured_events_log,
        interval_seconds=configured_interval,
        panes_override=tuple(configured_panes) if configured_panes is not None else None,
        max_log_bytes=configured_max_log_bytes,
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
        "--max-log-bytes", type=int, default=None, help="Rotate at this size (default: CHITRA_WATCHD_MAX_LOG_BYTES or 5 MiB)."
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
        max_log_bytes=args.max_log_bytes,
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
