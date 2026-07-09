"""triaged — systemd-supervised daemon that tails an events log and emits a
triage event only on an actual per-lane state-transition, never on an
unchanged repeat. Phase 0 of the off-foreground relay layer.

Interface contract for the tailed log: one opaque line per event, prefixed
``<ISO8601> <LANE_ID> <TEXT>`` (whitespace-separated, TEXT may contain
spaces). This is a defensive, minimal contract — lines that don't match are
logged and skipped rather than raising, since triaged must survive a watchd
emitter format drift without crashing.

No LLM calls. Deterministic dedup only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_EVENTS_LOG = Path("/var/lib/polyphony-chitra/events.log")
DEFAULT_STATE_FILE = Path("/var/lib/polyphony-chitra/triaged-state.json")
DEFAULT_TRIAGE_LOG = Path("/var/lib/polyphony-chitra/triaged.log")
DEFAULT_POLL_SECONDS = 2.0

# <ISO8601-ish timestamp> <LANE_ID> <rest of line>
_LINE_RE = re.compile(r"^(?P<ts>\S+)\s+(?P<lane>\S+)\s+(?P<text>.*)$")


def parse_event_line(line: str) -> tuple[str, str, str] | None:
    """Parse one events.log line into ``(ts, lane_id, text)``, or None if it
    doesn't match the ``<ISO8601> <LANE_ID> <TEXT>`` contract."""
    stripped = line.rstrip("\n")
    if not stripped.strip():
        return None
    match = _LINE_RE.match(stripped)
    if not match:
        return None
    return match.group("ts"), match.group("lane"), match.group("text")


def state_signature(text: str) -> str:
    """Stable signature for a lane's state text, used to detect a real
    transition vs. an unchanged repeat (e.g. a re-emitted heartbeat)."""
    return hashlib.sha256(text.strip().encode("utf-8", errors="replace")).hexdigest()


def load_state(state_file: Path) -> dict[str, str]:
    """Load the lane_id -> last-seen-signature map. A missing file silently
    starts empty (nothing to log — there's genuinely nothing there yet). A
    file that EXISTS but fails to parse is logged before falling back to
    empty (fail-soft: a lost dedup state means one extra triage event per
    lane at worst, never a crash — but corruption is never silent)."""
    try:
        loaded: dict[str, str] = json.loads(state_file.read_text(encoding="utf-8"))
        return loaded
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("triaged_state_file_corrupt", path=str(state_file), error=str(exc))
        return {}


def save_state(state_file: Path, state: dict[str, str]) -> None:
    """Save the state map atomically (write to temp, rename)."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(state_file)


def append_triage_event(triage_log: Path, lane_id: str, ts: str, text: str) -> None:
    """Append one triage event line. Only called on an actual transition."""
    triage_log.parent.mkdir(parents=True, exist_ok=True)
    with triage_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": ts, "lane_id": lane_id, "text": text}) + "\n")


def process_lines(
    lines: list[str],
    *,
    state: dict[str, str],
    triage_log: Path,
) -> int:
    """Process a batch of new events.log lines against ``state`` (mutated in
    place). Returns the count of lines that produced a real triage event
    (i.e. an actual state transition, not a dedup'd repeat)."""
    emitted = 0
    for line in lines:
        parsed = parse_event_line(line)
        if parsed is None:
            logger.warning("triaged_unparseable_line", line=line.rstrip("\n")[:200])
            continue
        ts, lane_id, text = parsed
        sig = state_signature(text)
        if state.get(lane_id) == sig:
            continue  # unchanged repeat — dedup'd, no event.
        state[lane_id] = sig
        append_triage_event(triage_log, lane_id, ts, text)
        emitted += 1
    return emitted


def run_once(
    events_log: Path,
    *,
    state_file: Path,
    triage_log: Path,
    offset_file: Path | None = None,
) -> int:
    """Read any new lines from ``events_log`` since the last run (tracked via
    a byte offset file) and process them. Returns the count of triage events
    emitted."""
    offset_path = offset_file or state_file.with_suffix(".offset")
    try:
        last_offset = int(offset_path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        last_offset = 0
    except (OSError, ValueError) as exc:
        logger.warning("triaged_offset_file_corrupt", path=str(offset_path), error=str(exc))
        last_offset = 0

    if not events_log.exists():
        return 0

    size = events_log.stat().st_size
    if size < last_offset:
        # Log was rotated/truncated — restart from the top.
        last_offset = 0

    with events_log.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(last_offset)
        new_lines = fh.readlines()
        new_offset = fh.tell()

    state = load_state(state_file)
    emitted = process_lines(new_lines, state=state, triage_log=triage_log)
    save_state(state_file, state)
    offset_path.parent.mkdir(parents=True, exist_ok=True)
    offset_path.write_text(str(new_offset), encoding="utf-8")
    return emitted


def run_forever(
    events_log: Path,
    *,
    state_file: Path,
    triage_log: Path,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> None:
    logger.info("triaged_started", events_log=str(events_log), poll_seconds=poll_seconds)
    while True:
        run_once(events_log, state_file=state_file, triage_log=triage_log)
        time.sleep(poll_seconds)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="triaged", description="State-transition dedup daemon (chitra phase 0).")
    parser.add_argument("--events-log", type=Path, default=DEFAULT_EVENTS_LOG)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--triage-log", type=Path, default=DEFAULT_TRIAGE_LOG)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--once", action="store_true", help="Process pending lines once and exit, instead of looping forever.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.once:
        emitted = run_once(args.events_log, state_file=args.state_file, triage_log=args.triage_log)
        print(json.dumps({"emitted": emitted}))
        return 0
    run_forever(
        args.events_log,
        state_file=args.state_file,
        triage_log=args.triage_log,
        poll_seconds=args.poll_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
