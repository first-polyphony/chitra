"""triaged — systemd-supervised daemon that tails an events log and emits a
triage event only on an actual per-lane state-transition, never on an
unchanged repeat. Phase 0 of the off-foreground relay layer.

Interface contract for the tailed log: one opaque line per event, prefixed
``<ISO8601> <LANE_ID> <TEXT>`` (whitespace-separated, TEXT may contain
spaces). This is a defensive, minimal contract — lines that don't match are
logged and skipped rather than raising, since triaged must survive a watchd
emitter format drift without crashing.

No LLM calls in this module's own code path — deterministic dedup only.
It watches state emitted by LLM-driven sessions, but never invokes a model itself.

Known limitation: the lane_id -> signature state map (``load_state`` /
``save_state``) has no eviction -- a lane that stops emitting events (e.g. a
retired session) leaves its entry in ``triaged-state.json`` forever. This is
a small, bounded string per lane, so growth is slow, but on a very
long-running deployment with many retired lanes the file will grow
unboundedly. A time-based eviction was considered (drop entries not touched
in N days) but the only per-event timestamp available is the event log's own
``ts`` field, which this module deliberately treats as an opaque, unvalidated
string (see the events-log contract above) rather than a value safe to parse
as a date for eviction decisions. Left as a documented limitation rather than
adding unvalidated timestamp parsing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_EVENTS_LOG = Path("/var/lib/chitra/events.log")
DEFAULT_STATE_FILE = Path("/var/lib/chitra/triaged-state.json")
DEFAULT_TRIAGE_LOG = Path("/var/lib/chitra/triaged.log")
DEFAULT_POLL_SECONDS = 2.0
CRITICAL_DEDUP_WINDOW_SECONDS = 900

# Receiving-pipeline interruption rules.  They are deterministic string
# matches over watchd's opaque state text; triaged never decides what a worker
# should do in response.
CRITICAL_RULES = (
    ("needs_operator", re.compile(r"needs (you|trey|operator|input)|waiting on (you|trey)", re.I)),
    ("merge_landed", re.compile(r'^\s*REVIEW_VERDICT: (CLEAN|ISSUES)\s*$|"state":\s*"MERGED"|\bMerged #\d|\bPR #?\d+ (was )?merged', re.I)),
    ("crash", re.compile(r"Traceback \(most recent call last\)|panic:|\bfatal(:| error)", re.I)),
    ("ci_red", re.compile(r"CI .*(failure|failed|red)|required check.*fail", re.I)),
    ("blocked", re.compile(r"\bBLOCKED\b")),
    ("rate_limit", re.compile(r"rate.?limit.*(8[5-9]|9[0-9])\s*%|(8[5-9]|9[0-9])\s*%.*(usage|limit)", re.I)),
)
COMMAND_ECHO = re.compile(r"^\s*[$❯>]|until \[|\$\(|do sleep|--jq|; do |&& echo|\bgh (pr|api|run)\b")

# <ISO8601-ish timestamp> <LANE_ID> <rest of line>
_LINE_RE = re.compile(r"^(?P<ts>\S+)\s+(?P<lane>\S+)\s+(?P<text>.*)$")


@dataclass(slots=True)
class ReceivingOutputs:
    """Optional compatibility artifacts for the receiving pipeline."""

    queue_file: Path
    flags_file: Path
    stats_file: Path
    alert_state_file: Path


def _env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser()


def default_receiving_outputs(state_file: Path) -> ReceivingOutputs:
    """Resolve output locations from the ``CHITRA_TRIAGE_*`` environment."""
    base = state_file.parent
    return ReceivingOutputs(
        queue_file=_env_path("CHITRA_TRIAGE_QUEUE_FILE", base / "queue.tsv"),
        flags_file=_env_path("CHITRA_TRIAGE_FLAGS_FILE", base / "flags.log"),
        stats_file=_env_path("CHITRA_TRIAGE_STATS_FILE", base / "stats.json"),
        alert_state_file=_env_path("CHITRA_TRIAGE_ALERT_STATE_FILE", base / "triaged-alert-state.json"),
    )


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


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(line.rstrip("\n") + "\n")


def critical_hits(text: str) -> list[tuple[str, str]]:
    """Return every interrupting rule matched by meaningful state text."""
    statements = [statement.strip() for statement in text.split("|") if not COMMAND_ECHO.search(statement)]
    hits: list[tuple[str, str]] = []
    for rule, pattern in CRITICAL_RULES:
        for statement in statements:
            if pattern.search(statement):
                hits.append((rule, statement))
                break
    return hits


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _counter(value: object) -> int:
    """Return a non-negative counter from an untrusted persisted value."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def emit_receiving_event(
    *,
    ts: str,
    lane_id: str,
    text: str,
    outputs: ReceivingOutputs,
    alert_state: dict[str, object],
    stats: dict[str, object],
    now: float | None = None,
) -> None:
    """Write queue/flag artifacts for one real state transition.

    Alert deduplication is persistent and keyed by lane/rule plus the matching
    statement, matching the old pilot's quiet-foreground contract while
    preserving Chitra's stronger transition dedup as the first filter.
    """
    epoch = time.time() if now is None else now
    hits = critical_hits(text)
    severity = "CRIT" if hits else "INFO"
    signature = ",".join(rule for rule, _ in hits) or "-"
    summary = (hits[0][1] if hits else text).replace("\t", " ").replace("\n", " ")[:200]
    _append_line(outputs.queue_file, f"{ts}\t{severity}\t{lane_id}\t{signature}\t{summary}")
    stats["crit_raw"] = _counter(stats.get("crit_raw", 0)) + len(hits)
    for rule, statement in hits:
        key = f"{lane_id}\x1f{rule}"
        digest = hashlib.sha256(statement.encode("utf-8", errors="replace")).hexdigest()
        previous = alert_state.get(key)
        if isinstance(previous, dict):
            previous_epoch = previous.get("epoch")
            if (
                previous.get("digest") == digest
                and isinstance(previous_epoch, int | float)
                and epoch - previous_epoch < CRITICAL_DEDUP_WINDOW_SECONDS
            ):
                continue
        alert_state[key] = {"epoch": epoch, "digest": digest}
        stats["crit_emitted"] = _counter(stats.get("crit_emitted", 0)) + 1
        _append_line(outputs.flags_file, f"CRIT {ts} {lane_id} {rule}: {statement[:300]}")


def process_lines(
    lines: list[str],
    *,
    state: dict[str, str],
    triage_log: Path,
    receiving_outputs: ReceivingOutputs | None = None,
    alert_state: dict[str, object] | None = None,
    stats: dict[str, object] | None = None,
) -> int:
    """Process a batch of new events.log lines against ``state`` (mutated in
    place). Returns the count of lines that produced a real triage event
    (i.e. an actual state transition, not a dedup'd repeat)."""
    emitted = 0
    active_alert_state = alert_state if alert_state is not None else {}
    active_stats = stats if stats is not None else {}
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
        if receiving_outputs is not None:
            emit_receiving_event(
                ts=ts,
                lane_id=lane_id,
                text=text,
                outputs=receiving_outputs,
                alert_state=active_alert_state,
                stats=active_stats,
            )
        emitted += 1
    return emitted


def run_once(
    events_log: Path,
    *,
    state_file: Path,
    triage_log: Path,
    offset_file: Path | None = None,
    receiving_outputs: ReceivingOutputs | None = None,
) -> int:
    """Read any new lines from ``events_log`` since the last run (tracked via
    a byte offset file) and process them. Returns the count of triage events
    emitted."""
    offset_path = offset_file or state_file.with_suffix(".offset")
    try:
        raw_offset = offset_path.read_text(encoding="utf-8").strip()
        parsed_offset = json.loads(raw_offset) if raw_offset.startswith("{") else int(raw_offset)
        if isinstance(parsed_offset, dict):
            last_offset = _counter(parsed_offset.get("offset", 0))
            last_inode = parsed_offset.get("inode")
        else:
            last_offset, last_inode = int(parsed_offset), None
    except FileNotFoundError:
        last_offset, last_inode = 0, None
    except (OSError, ValueError) as exc:
        logger.warning("triaged_offset_file_corrupt", path=str(offset_path), error=str(exc))
        last_offset, last_inode = 0, None

    if not events_log.exists():
        return 0

    event_stat = events_log.stat()
    if (last_inode is not None and event_stat.st_ino != last_inode) or event_stat.st_size < last_offset:
        # Log was rotated or truncated — restart from the top.  The inode
        # check catches a replacement whose new file happens to be larger.
        last_offset = 0

    with events_log.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(last_offset)
        new_lines = fh.readlines()
        new_offset = fh.tell()

    state = load_state(state_file)
    stats: dict[str, object] = {}
    alert_state: dict[str, object] = {}
    if receiving_outputs is not None:
        stats = _load_json_object(receiving_outputs.stats_file)
        alert_state = _load_json_object(receiving_outputs.alert_state_file)
        stats["events"] = _counter(stats.get("events", 0)) + len(new_lines)
        stats["changes"] = _counter(stats.get("changes", 0))
        stats["crit_raw"] = _counter(stats.get("crit_raw", 0))
        stats["crit_emitted"] = _counter(stats.get("crit_emitted", 0))
        stats.setdefault("started", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    emitted = process_lines(
        new_lines,
        state=state,
        triage_log=triage_log,
        receiving_outputs=receiving_outputs,
        alert_state=alert_state,
        stats=stats,
    )
    if receiving_outputs is not None:
        stats["changes"] = _counter(stats["changes"]) + emitted
        stats["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        _save_json_atomic(receiving_outputs.stats_file, stats)
        _save_json_atomic(receiving_outputs.alert_state_file, alert_state)
    save_state(state_file, state)
    offset_path.parent.mkdir(parents=True, exist_ok=True)
    _save_json_atomic(offset_path, {"offset": new_offset, "inode": event_stat.st_ino})
    return emitted


def run_forever(
    events_log: Path,
    *,
    state_file: Path,
    triage_log: Path,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    receiving_outputs: ReceivingOutputs | None = None,
) -> None:
    logger.info("triaged_started", events_log=str(events_log), poll_seconds=poll_seconds)
    while True:
        run_once(events_log, state_file=state_file, triage_log=triage_log, receiving_outputs=receiving_outputs)
        time.sleep(poll_seconds)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="triaged", description="State-transition dedup daemon (chitra phase 0).")
    parser.add_argument("--events-log", type=Path, default=_env_path("CHITRA_TRIAGE_EVENTS_LOG", DEFAULT_EVENTS_LOG))
    parser.add_argument("--state-file", type=Path, default=_env_path("CHITRA_TRIAGE_STATE_FILE", DEFAULT_STATE_FILE))
    parser.add_argument("--triage-log", type=Path, default=_env_path("CHITRA_TRIAGE_LOG", DEFAULT_TRIAGE_LOG))
    parser.add_argument("--queue-file", type=Path, default=None)
    parser.add_argument("--flags-file", type=Path, default=None)
    parser.add_argument("--stats-file", type=Path, default=None)
    parser.add_argument("--alert-state-file", type=Path, default=None)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--once", action="store_true", help="Process pending lines once and exit, instead of looping forever.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    outputs = default_receiving_outputs(args.state_file)
    if args.queue_file is not None:
        outputs.queue_file = args.queue_file
    if args.flags_file is not None:
        outputs.flags_file = args.flags_file
    if args.stats_file is not None:
        outputs.stats_file = args.stats_file
    if args.alert_state_file is not None:
        outputs.alert_state_file = args.alert_state_file
    if args.once:
        emitted = run_once(args.events_log, state_file=args.state_file, triage_log=args.triage_log, receiving_outputs=outputs)
        print(json.dumps({"emitted": emitted}))
        return 0
    run_forever(
        args.events_log,
        state_file=args.state_file,
        triage_log=args.triage_log,
        poll_seconds=args.poll_seconds,
        receiving_outputs=outputs,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
