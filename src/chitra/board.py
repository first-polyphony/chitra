"""Render Chitra's validated board facts into the operator-facing HTML board.

The renderer is deliberately a consumer of ``facts.json``: it performs no
classification or language-model work.  It renders the bundled, accessible
template atomically, records success/failure in ``health.json``, and can show
short tmux tails for configured local or remote hosts.

A lane with unresolved open asks is an operator-attention item; doctrine owns
the marker, while this module surfaces every stored ask in every roster.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import socket
import subprocess
import textwrap
import time
import unicodedata
from collections.abc import Sequence
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, Protocol

import structlog

from chitra.board_updater import validate_board_facts
from chitra.goals import GoalStatus, session_host, session_name
from chitra.state_paths import state_dir as default_state_dir

logger = structlog.get_logger(__name__)

DEFAULT_REMOTE_USER = "ubuntu"
MAX_SUBHUB_AGE_SECONDS = 600
TOKEN_RE = re.compile(r"\{\{[a-zA-Z0-9_]+}}")
FALLBACK_ACCENTS = (
    "#e9a23b", "#5e9ed6", "#b48ee0", "#53b583", "#d98ca0", "#52b3c4",
    "#d9825f", "#bcae57", "#8a93e8", "#6fbf9a", "#c98bd6", "#7d8aa5",
)
STATE_KEYS = {"st-done": "done", "st-stuck": "stuck"}
PROVIDER_LABELS = {
    "anthropic": "Claude subscription",
    "openai-codex": "Codex subscription",
    "anthropic-admin": "Claude Admin API",
    "anthropic-api": "Claude API key",
    "openai-platform": "OpenAI Platform API",
    "google-gemini": "Gemini API key",
    "openrouter": "OpenRouter API key",
    "tavily": "Tavily API key",
    "github": "GitHub OAuth",
}
# Minimum column widths (display columns). Cells WRAP to multiple lines rather
# than truncate, so nothing is lost; these are floors for the terminal-width-
# aware allocation, and the Session column caps here.
ROSTER_SESSION_MAX_WIDTH = 20
ROSTER_GOAL_MAX_WIDTH = 34
ROSTER_NOW_MAX_WIDTH = 28
ROSTER_NEEDS_MAX_WIDTH = 26
ROSTER_GOAL_MIN_WIDTH = 24
ROSTER_NOW_MIN_WIDTH = 18
ROSTER_NEEDS_MIN_WIDTH = 8
ROSTER_MARKER_WIDTH = 2  # emoji markers render 2 terminal columns
ROSTER_DEFAULT_TERM_WIDTH = 100
ROSTER_MAX_TERM_WIDTH = 160
ROSTER_MARKERS: dict[GoalStatus, str] = {
    "blocked": "🔴",
    "held": "🟡",
    "idle": "🟡",
    "working": "🟢",
    "done-pending-verification": "🟢",
    "done-pending-close": "🟢",
}


class RosterRecord(Protocol):
    """The goal-state fields consumed by the small terminal roster."""

    @property
    def session_ref(self) -> str: ...

    @property
    def goal(self) -> str: ...

    @property
    def done_when(self) -> str: ...

    @property
    def status(self) -> GoalStatus: ...

    @property
    def now(self) -> str: ...

    @property
    def open_asks(self) -> tuple[str, ...]: ...

    @property
    def needs(self) -> str: ...


class ArtifactRosterRecord(Protocol):
    """The published-artifact fields rendered in the operator roster."""

    @property
    def title(self) -> str: ...

    @property
    def url(self) -> str: ...

    @property
    def published_at(self) -> str: ...

    @property
    def review_status(self) -> Literal["unreviewed", "reviewed"]: ...


def marker_for(status: GoalStatus) -> str:
    """Return the status-only marker, rejecting status outside the six states."""
    try:
        return ROSTER_MARKERS[status]
    except KeyError as exc:
        raise ValueError(f"unknown goal status: {status}") from exc


def compute_marker(record: RosterRecord) -> str:
    """Return the deterministic roster marker with this precedence.

    Open asks or ``blocked`` are red first because they need a named human
    unblock. ``held`` and ``idle`` are yellow because they are idle by design.
    ``working`` and the active Chitra completion states are green. Any other
    status is uncolorable.
    """
    if record.open_asks or record.status == "blocked":
        return "🔴"
    if record.status in ("held", "idle"):
        return "🟡"
    if record.status in ("working", "done-pending-verification", "done-pending-close"):
        return "🟢"
    raise ValueError(f"uncolorable status: {record.status}")


_ZERO_WIDTH_CHARS = frozenset(("️", "︎", "‍"))


def _char_width(char: str) -> int:
    """Display width of a single character in terminal columns.

    Variation selectors / ZWJ and combining marks are zero-width; East-Asian
    wide/fullwidth and the emoji blocks (which the status markers live in)
    render two columns. Everything else is one.
    """
    if char in _ZERO_WIDTH_CHARS or unicodedata.combining(char):
        return 0
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 2
    code = ord(char)
    if 0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF:
        return 2
    return 1


def display_width(text: str) -> int:
    """Terminal display width of a string (emoji/CJK-aware, unlike ``len``)."""
    return sum(_char_width(char) for char in text)


def _pad(text: str, width: int) -> str:
    """Right-pad ``text`` to ``width`` DISPLAY columns (not code points)."""
    return text + " " * max(0, width - display_width(text))


def _wrap_cell(text: str, width: int) -> list[str]:
    """Word-wrap collapsed ``text`` into lines no wider than ``width`` columns.

    Wrapping (never single-line truncation) so a Goal's done-condition and a
    long Now/Needs survive intact — they just flow onto more lines. Overlong
    unbroken tokens are hard-split so a row can never exceed its column width.
    """
    compact = " ".join(text.split())
    if not compact:
        return [""]
    lines = textwrap.wrap(
        compact,
        width=max(1, width),
        break_long_words=True,
        break_on_hyphens=False,
    )
    return lines or [""]


def _roster_needs(record: RosterRecord, marker: str) -> str:
    """The Needs cell: for a red lane, the named unblock; otherwise a dash."""
    if marker == "🔴":
        return record.needs or "; ".join(record.open_asks) or "(name the block)"
    return "—"


def _roster_goal(record: RosterRecord) -> str:
    """Return the goal text with a minimal marker for redirected versions."""
    goal_version = getattr(record, "goal_version", 1)
    version_marker = f" v{goal_version}" if goal_version > 1 else ""
    return f"{record.goal}{version_marker}"


def _roster_rows(records: Sequence[RosterRecord]) -> list[tuple[str, str, str, str, str]]:
    """Build FULL (untruncated) cell rows in stable host/session/ref order.

    Truncation/wrapping is the renderer's job (box wraps; markdown lets the
    client wrap), so the cells here carry the complete text.
    """
    rows: list[tuple[str, str, str, str, str]] = []
    for record in _ordered_roster_records(records):
        marker = compute_marker(record)
        rows.append(
            (
                marker,
                session_name(record.session_ref),
                f"{_roster_goal(record)} — done: {record.done_when}",
                record.now,
                _roster_needs(record, marker),
            )
        )
    return rows


def _ordered_roster_records(records: Sequence[RosterRecord]) -> list[RosterRecord]:
    """Return records in the stable host, session, then full-reference order."""
    return sorted(
        records,
        key=lambda record: (
            session_host(record.session_ref),
            session_name(record.session_ref),
            record.session_ref,
        ),
    )


def _awaiting_ruling_lines(records: Sequence[RosterRecord], *, fmt: Literal["box", "markdown"]) -> list[str]:
    """Render every stored ask below the table, preserving stable lane order."""
    asks = [(record.session_ref, ask) for record in _ordered_roster_records(records) for ask in record.open_asks]
    if not asks:
        return []
    if fmt == "markdown":
        return ["**AWAITING RULING — surfaced every report until you rule:**", "", *(f"- {session}: {ask}" for session, ask in asks)]
    return ["AWAITING RULING — surfaced every report until you rule:", *(f"  • {session}: {ask}" for session, ask in asks)]


def _unreviewed_artifact_block(
    artifacts: Sequence[ArtifactRosterRecord], *, fmt: Literal["cards", "box", "markdown"]
) -> str:
    """Render each injected unreviewed artifact on one unwrapped, copyable line."""
    unreviewed = sorted(
        (artifact for artifact in artifacts if artifact.review_status == "unreviewed"),
        key=lambda artifact: (artifact.published_at, artifact.url),
    )
    if not unreviewed:
        return ""
    prefix = "- " if fmt == "markdown" else "  • "
    return "\n".join(
        ("UNREVIEWED ARTIFACTS:", *(f"{prefix}{artifact.title} — {artifact.url}" for artifact in unreviewed))
    )


ROSTER_CARD_LABEL_WIDTH = 6


def _render_cards(records: Sequence[RosterRecord]) -> str:
    """Render each lane as a labelled stanza rather than a table.

    Goal/Now are full sentences, which never fit table columns cleanly (wrap =
    noisy, truncate = useless). A card puts the marker + session on a header
    line and the full fields beneath, wrapped to the terminal with a hanging
    indent. Nothing truncated, no column/emoji-width alignment to break — the
    markers down the left edge are the scan surface.
    """
    indent = " " * (ROSTER_MARKER_WIDTH + 1)  # align field labels under the session name
    avail = max(24, _terminal_width() - len(indent) - ROSTER_CARD_LABEL_WIDTH)

    def field(label: str, text: str) -> list[str]:
        lines = textwrap.wrap(" ".join(text.split()), width=avail, break_long_words=True, break_on_hyphens=False) or [""]
        head = f"{indent}{label:<{ROSTER_CARD_LABEL_WIDTH}}{lines[0]}"
        tail = [f"{indent}{'':<{ROSTER_CARD_LABEL_WIDTH}}{line}" for line in lines[1:]]
        return [head, *tail]

    blocks: list[str] = []
    for record in _ordered_roster_records(records):
        marker = compute_marker(record)
        block = [f"{marker} {session_name(record.session_ref)}"]
        block += field("Goal", f"{_roster_goal(record)}  ·  done: {record.done_when}")
        block += field("Now", record.now)
        if marker == "🔴":
            block += field("Needs", _roster_needs(record, marker))
        blocks.append("\n".join(block))
    body = "\n\n".join(blocks)
    awaiting = _awaiting_ruling_lines(records, fmt="box")
    return "\n\n".join((body, "\n".join(awaiting))) if awaiting else body


def render_roster(
    records: Sequence[RosterRecord],
    *,
    fmt: Literal["cards", "box", "markdown"] = "box",
    artifacts: Sequence[ArtifactRosterRecord] = (),
) -> str:
    """Render every stored lane, stable order (host, session name, full ref).

    Default ``box``: the fixed-column table with the operator's agreed color
    legend (🟢/🟡/🔴 marker column), wrapped cells, and emoji-aware alignment
    — this is the format the operator confirmed ("table format noted — box
    tables from here on"). ``cards``: one labelled stanza per lane, offered
    as an alternate, narrower-terminal layout. ``markdown``: a
    client-rendered table. Artifact records are injected by the caller;
    their URLs are emitted in a separate unwrapped block so operators can
    copy each complete URL.
    """
    artifact_block = _unreviewed_artifact_block(artifacts, fmt=fmt)
    if not records and not artifact_block:
        return "no lanes recorded"
    if fmt == "cards":
        roster = _render_cards(records) if records else "no lanes recorded"
        return "\n\n".join((roster, artifact_block)) if artifact_block else roster
    headers = ("", "Session", "Goal", "Now", "Needs")
    rows = _roster_rows(records)
    if fmt == "markdown":
        def markdown_cell(value: str) -> str:
            return value.replace("|", "\\|")

        rendered = ["| " + " | ".join(headers) + " |", "| --- | --- | --- | --- | --- |"]
        rendered.extend("| " + " | ".join(markdown_cell(cell) for cell in row) + " |" for row in rows)
        roster = "\n".join([*rendered, *_awaiting_ruling_lines(records, fmt="markdown")])
        return "\n\n".join((roster, artifact_block)) if artifact_block else roster
    if fmt != "box":
        raise ValueError(f"unknown roster format: {fmt}")
    widths = _roster_column_widths(rows)

    def border(left: str, middle: str, right: str, fill: str) -> str:
        return left + middle.join(fill * (width + 2) for width in widths) + right

    def physical_lines(row: tuple[str, str, str, str, str]) -> list[str]:
        # Marker is a single glyph in a fixed slot (never wrapped); the text
        # columns wrap to as many lines as they need. Pad every segment by
        # DISPLAY width so emoji/CJK never shift a column.
        wrapped = [
            [row[0]] if index == 0 else _wrap_cell(row[index], widths[index])
            for index in range(len(row))
        ]
        height = max(len(cell) for cell in wrapped)
        out: list[str] = []
        for r in range(height):
            segments = [
                " " + _pad(cell[r] if r < len(cell) else "", widths[index]) + " "
                for index, cell in enumerate(wrapped)
            ]
            out.append("│" + "│".join(segments) + "│")
        return out

    body: list[str] = []
    for position, row in enumerate(rows):
        if position:
            body.append(border("├", "┼", "┤", "─"))  # rule between multi-line lanes
        body.extend(physical_lines(row))

    table = "\n".join(
        (
            border("┌", "┬", "┐", "─"),
            *physical_lines(headers),
            border("├", "┼", "┤", "─"),
            *body,
            border("└", "┴", "┘", "─"),
        )
    )
    awaiting_ruling = _awaiting_ruling_lines(records, fmt="box")
    roster = "\n\n".join((table, "\n".join(awaiting_ruling))) if awaiting_ruling else table
    return "\n\n".join((roster, artifact_block)) if artifact_block else roster


def _terminal_width() -> int:
    """Usable terminal width in columns, clamped to a sane band."""
    try:
        cols = int(os.environ.get("COLUMNS", "") or 0)
    except ValueError:
        cols = 0
    if cols <= 0:
        cols = ROSTER_DEFAULT_TERM_WIDTH
    return max(80, min(cols, ROSTER_MAX_TERM_WIDTH))


def _roster_column_widths(rows: Sequence[tuple[str, str, str, str, str]]) -> list[int]:
    """Allocate the five column widths to the terminal, wrapping absorbs the rest.

    marker + Session are fixed; Goal / Now / Needs split the remainder by
    priority (Goal widest), each floored so nothing collapses to a sliver.
    Because cells wrap, a narrow terminal just makes taller rows — content is
    never dropped.
    """
    term = _terminal_width()
    # box overhead: one leading '│', then per column '│ … ' → (ncols+1) bars + ncols*2 spaces
    overhead = (5 + 1) + 5 * 2
    budget = max(40, term - overhead)
    marker_w = ROSTER_MARKER_WIDTH
    session_source = [display_width("Session"), *(display_width(row[1]) for row in rows)]
    session_w = max(display_width("Session"), min(max(session_source), ROSTER_SESSION_MAX_WIDTH))
    remaining = max(ROSTER_GOAL_MIN_WIDTH + ROSTER_NOW_MIN_WIDTH + ROSTER_NEEDS_MIN_WIDTH, budget - marker_w - session_w)
    goal_w = max(ROSTER_GOAL_MIN_WIDTH, int(remaining * 0.46))
    now_w = max(ROSTER_NOW_MIN_WIDTH, int(remaining * 0.32))
    needs_w = max(ROSTER_NEEDS_MIN_WIDTH, remaining - goal_w - now_w)
    return [marker_w, session_w, goal_w, now_w, needs_w]


def _env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser()


def default_board_dir() -> Path:
    return _env_path("CHITRA_BOARD_DIR", default_state_dir() / "board")


def _env_hosts(value: str | None) -> set[str]:
    return {host.strip() for host in (value or "").split(",") if host.strip()}


def accent_for(session_id: str) -> str:
    """Return a stable, deployment-agnostic accent colour for a session."""
    return FALLBACK_ACCENTS[sum(session_id.encode("utf-8")) % len(FALLBACK_ACCENTS)]


def needs_operator(session: dict[str, Any]) -> bool:
    return bool(session["wants"]) or session.get("you") is not None


def row_state_key(session: dict[str, Any]) -> str:
    if needs_operator(session) or session["state"]["cls"] == "st-you":
        return "needs"
    return STATE_KEYS.get(session["state"]["cls"], "work")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_health(board_dir: Path, ok: bool, error: str | None = None) -> None:
    _write_json_atomic(board_dir / "health.json", {"ok": ok, "error": error, "ts": int(time.time())})


def normalize_tail(raw: str) -> list[str]:
    """Discard terminal chrome and an open input box from a captured pane."""
    lines = raw.splitlines()
    prompts = [index for index, line in enumerate(lines) if line.startswith("❯")]
    if prompts:
        lines = lines[: prompts[-1]]
    volatile = re.compile(r"^\s*[·✻✽✳✢*●○◐◯]|tokens\b|🪟|⏵⏵|esc to interrupt|ctrl\+b|^─+$|^\s*$|Press up to edit")
    timer = re.compile(r"\([0-9]+m? ?[0-9]*s?[^)]*\)")
    return [cleaned for line in lines if not volatile.search(line) if (cleaned := timer.sub("", line).rstrip())]


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def capture_tail(
    session: dict[str, Any], *, local_host: str, remote_hosts: set[str], remote_user: str
) -> str:
    """Return a compact pane tail, or an honest availability message.

    Remote capture is opt-in through ``CHITRA_BOARD_REMOTE_HOSTS``.  This
    prevents untrusted facts files from turning board rendering into arbitrary
    SSH requests.
    """
    target = session["tmux"]["session"]
    host = session["tmux"]["host"]
    if host == local_host:
        command = ["tmux", "capture-pane", "-p", "-J", "-t", target, "-S", "-60"]
    elif host in remote_hosts:
        remote = f"tmux capture-pane -p -J -t {_shell_quote(target)} -S -60"
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"{remote_user}@{host}", remote]
    else:
        return "tail unavailable: host is not configured for board capture"
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=6, check=False)
    except subprocess.TimeoutExpired:
        return "tail unavailable"
    except OSError as exc:
        return f"tail unavailable: {exc}"
    if result.returncode != 0:
        output = (result.stderr or result.stdout or "capture failed").strip().splitlines()
        return "tail unavailable" if not output else f"tail unavailable: {output[-1][:120]}"
    tail = normalize_tail(result.stdout)
    return "\n".join(tail[-6:]) if tail else "tail unavailable"


def answer_html(note: str) -> str:
    marker = "One later answer is waiting when you're ready."
    if marker not in note:
        return html.escape(note)
    before, after = note.split(marker, 1)
    return html.escape(before) + f'<span class="y">{html.escape(marker)}</span>' + html.escape(after)


def render_detail(items: list[dict[str, Any]], tail: str) -> str:
    parts: list[str] = []
    for item in items:
        if item.get("kv") is not None:
            parts.append(f'        <span class="kv">{html.escape(item["kv"])}</span>')
        parts.append(f"        <p>{html.escape(item['text'])}</p>")
    parts.extend(('        <span class="kv">live tail</span>', f'        <div class="tail">{html.escape(tail)}</div>'))
    return "\n".join(parts)


def sort_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(sessions, key=lambda session: 0 if needs_operator(session) else 1)


def render_rows(
    sessions: list[dict[str, Any]], *, local_host: str, remote_hosts: set[str], remote_user: str
) -> str:
    rows: list[str] = []
    for session in sessions:
        state = session["state"]
        extra = f"<br>{html.escape(state['extra'])}" if state["extra"] else ""
        needs = needs_operator(session)
        needline = ""
        if needs:
            ask = session.get("you") or "this session flagged that it wants your attention"
            needline = (
                f'\n        <div class="needline"><span class="nlabel">◆ {html.escape(session["name"])} needs you:</span>'
                f"{html.escape(ask)}</div>"
            )
        tail = capture_tail(session, local_host=local_host, remote_hosts=remote_hosts, remote_user=remote_user)
        rows.append(
            f'''\
    <details class="row {'needs' if needs else 'plain'}" id="{html.escape(session['id'])}"
      style="--session: {accent_for(session['id'])}" data-session="{html.escape(session['id'])}" data-state="{row_state_key(session)}">
      <summary class="rowline">{needline}
        <div class="cell-name"><div class="sname">{html.escape(session['name'])}</div>
          <div class="sid">{html.escape(session['sid'])}</div></div>
        <div class="state {html.escape(state['cls'])}">{html.escape(state['word'])}{extra}</div>
        <div class="doing"><span class="goal">GOAL · {html.escape(session['goal'])}</span>{html.escape(session['doing'])}</div>
        <span class="btn">▸ detail</span>
      </summary>
      <div class="detailwrap">
{render_detail(session['detail'], tail)}
      </div>
    </details>'''
        )
    return "\n".join(rows)


def render_filter_chips(sessions: list[dict[str, Any]]) -> str:
    return "\n".join(
        f'        <button class="fchip on" data-kind="session" data-value="{html.escape(session["id"])}" '
        f'style="--session: {accent_for(session["id"])}">{html.escape(session["name"])}</button>'
        for session in sessions
    )


def render_needs_summary(sessions: list[dict[str, Any]]) -> str:
    count = sum(needs_operator(session) for session in sessions)
    if count == 0:
        return '<span class="quiet">nothing needs your input right now</span>'
    noun = "session needs" if count == 1 else "sessions need"
    return f'<span class="y">◆ {count} {noun} your input — pinned at the top of the board</span>'


def render_feed_items(rows: list[dict[str, Any]], done_sessions: list[dict[str, Any]], board_ids: set[str]) -> str:
    output: list[str] = []
    for session in done_sessions:
        accent = accent_for(session["id"])
        output.append(
            f'        <div class="fitem">\n          <span class="ft">done</span>'
            f'<span class="schip closed" style="--session: {accent}">{html.escape(session["name"])}</span>\n'
            f'          <div class="ftext">{html.escape("session closed · " + session["doing"])}</div>\n        </div>'
        )
    for row in rows:
        target = row.get("chip_target")
        chip = html.escape(row["chip"])
        if target in board_ids:
            chip_html = f'<a class="schip" href="#{html.escape(target)}" style="--session: {accent_for(target)}">{chip}</a>'
            data_session = f' data-session="{html.escape(target)}"'
        elif target:
            chip_html = f'<span class="schip closed" style="--session: {accent_for(target)}">{chip}</span>'
            data_session = ""
        else:
            chip_html, data_session = f'<span class="schip">{chip}</span>', ""
        output.append(
            f'        <div class="fitem"{data_session}>\n          <span class="ft">{html.escape(row["t"])}</span>{chip_html}\n'
            f'          <div class="ftext">{html.escape(row["text"])}</div>\n        </div>'
        )
    return "\n".join(output)


def stamp(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).strftime("%H:%M UTC %b %-d")


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _human_duration(seconds: Any) -> str | None:
    value = _number(seconds)
    if value is None or value <= 0:
        return None
    days, remainder = divmod(int(value), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    return f"{days}d {hours}h" if days else f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _iso_stamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp(int(parsed.timestamp()))


def _subhub_usage(account: dict[str, Any]) -> str:
    state = account.get("state") or "unknown"
    usage = account.get("usage") or {}
    if state != "available":
        reason = usage.get("reason") or account.get("reason")
        note = f" · {html.escape(str(reason))}" if reason else ""
        return f'<span class="withheld">{html.escape(str(state).replace("_", " "))} — capacity not shown</span>{note}'
    limiting = _number(usage.get("limiting_percent"))
    if usage.get("known") is not True or limiting is None:
        return '<span class="withheld">no verified usage data</span>'
    freshness = usage.get("freshness") or {}
    stale = freshness.get("label") == "stale" or (_number(freshness.get("age_seconds")) or 0) > MAX_SUBHUB_AGE_SECONDS
    parts: list[str] = []
    for window, key in (("session", "session_percent"), ("week", "week_percent")):
        percent = _number(usage.get(key))
        if percent is not None:
            mark = " ◂ limiting" if usage.get("limiting_window") == window else ""
            parts.append(f'{window} <span class="pct">{percent:.0f}% used</span>{html.escape(mark)}')
    if not parts:
        parts.append(f'<span class="pct">{limiting:.0f}% used</span>')
    if stale:
        verified = _iso_stamp(freshness.get("captured_at"))
        tag = f"probe stale — last verified {verified}" if verified else "probe stale"
        parts.append(f'<span class="stale-tag">{html.escape(tag)}</span>')
    elif (wait := _human_duration(usage.get("limiting_reset_in_seconds"))):
        parts.append(html.escape(f"resets in {wait}"))
    return " · ".join(parts)


def render_subhub(capacity_file: Path | None, epoch: int) -> str:
    if capacity_file is None or not capacity_file.exists():
        return '<p class="subempty">SubHub data unavailable — no capacity snapshot configured.</p>'
    try:
        mtime = int(capacity_file.stat().st_mtime)
        if epoch - mtime > MAX_SUBHUB_AGE_SECONDS:
            return (
                f'<p class="subempty">SubHub data stale as of {html.escape(stamp(mtime))} — '
                "numbers withheld until the next fresh snapshot.</p>"
            )
        data = json.loads(capacity_file.read_text(encoding="utf-8"))
        accounts = data["accounts"]
        if not isinstance(accounts, list):
            raise ValueError("accounts must be a list")
    except (OSError, ValueError, KeyError, TypeError):
        return '<p class="subempty">SubHub data unavailable — capacity snapshot is unreadable.</p>'
    subscriptions = [account for account in accounts if isinstance(account, dict) and account.get("kind") == "oauth_subscription"]
    apis = [account for account in accounts if isinstance(account, dict) and account.get("kind") != "oauth_subscription"]
    meta = [f"snapshot {stamp(mtime)}"]
    if (slots := _number(data.get("available_slots"))) is not None:
        constrained = _number(data.get("constrained_slots")) or 0
        meta.append(f"subscription slots available: {slots:.0f} · constrained: {constrained:.0f}")
    rendered = [f'<p class="submeta">{html.escape(" · ".join(meta))}</p>', "<h3>Subscriptions</h3>", '<div class="subrows">']
    for account in subscriptions:
        usage = account.get("usage") or {}
        name = usage.get("account") or account.get("expected_account") or ""
        state = str(account.get("state") or "unknown")
        rendered.append(
            '<div class="subrow">'
            f'<div class="cell-acct"><span class="aname">{html.escape(str(account.get("label", "unknown")))}</span>'
            f'<span class="aacct">{html.escape(str(name))}</span></div>'
            f'<span class="substate {"ok" if state == "available" else "warn"}">{html.escape(state.replace("_", " "))}</span>'
            f'<div class="subusage">{_subhub_usage(account)}</div></div>'
        )
    rendered.extend(("</div>", "<h3>API keys</h3>", '<div class="apichips">'))
    for account in apis:
        provider = str(account.get("provider") or "unknown")
        state = str(account.get("state") or "unknown")
        usage = account.get("usage") or {}
        headroom = _number(usage.get("capacity_headroom_percent"))
        if headroom is None:
            headroom = _number(usage.get("remaining"))
        capacity = (
            f'<span class="pct">{headroom:.0f}% left</span>{html.escape(_api_reset_suffix(usage))}'
            if state == "available" and usage.get("known") is True and headroom is not None
            else "no verified capacity value"
        )
        state_label = "live" if state == "available" else state.replace("_", " ")
        dot_class = "ok" if state == "available" else "warn"
        rendered.append(
            f'<span class="apichip"><span class="dot {dot_class}">●</span> '
            f'{html.escape(PROVIDER_LABELS.get(provider, provider))} · {html.escape(state_label)} · {capacity}</span>'
        )
    rendered.append("</div>")
    return "\n".join(rendered)


def _api_reset_suffix(usage: dict[str, Any]) -> str:
    reset_label = usage.get("reset_label")
    return f" this {reset_label.removesuffix('ly')}" if reset_label == "monthly" else ""


def bundled_template() -> str:
    return files("chitra").joinpath("templates/board.html").read_text(encoding="utf-8")


def render(
    facts: dict[str, Any], *, template: str | None = None, epoch: int | None = None, local_host: str | None = None,
    remote_hosts: set[str] | None = None, remote_user: str = DEFAULT_REMOTE_USER, capacity_file: Path | None = None,
    expected_owner: str | None = None, valid_hosts: set[str] | None = None,
) -> str:
    """Render a fully validated fact document to operator HTML."""
    validation = validate_board_facts(facts, expected_owner=expected_owner, valid_hosts=valid_hosts)
    if not validation.ok:
        raise ValueError("facts.json invalid: " + "; ".join(validation.errors))
    document = template if template is not None else bundled_template()
    generated_at = epoch if epoch is not None else int(time.time())
    host = local_host or os.environ.get("CHITRA_BOARD_LOCAL_HOST") or socket.gethostname()
    configured_remote_hosts = remote_hosts if remote_hosts is not None else _env_hosts(os.environ.get("CHITRA_BOARD_REMOTE_HOSTS"))
    sessions = sort_sessions(facts["sessions"])
    board_sessions = [session for session in sessions if session["state"]["cls"] != "st-done"]
    done_sessions = [session for session in sessions if session["state"]["cls"] == "st-done"]
    ids = {session["id"] for session in board_sessions}
    replacements = {
        "{{generated_note_html}}": answer_html(facts["generated_note"]),
        "{{snapshot_stamp}}": html.escape(stamp(generated_at)),
        "{{needs_summary_html}}": render_needs_summary(board_sessions),
        "{{session_filter_chips_html}}": render_filter_chips(board_sessions),
        "{{rows_html}}": render_rows(board_sessions, local_host=host, remote_hosts=configured_remote_hosts, remote_user=remote_user),
        "{{feed_items_html}}": render_feed_items(facts["log"], done_sessions, ids),
        "{{selfcheck_solid}}": html.escape(facts["selfcheck"]["solid"]),
        "{{selfcheck_weak}}": html.escape(facts["selfcheck"]["weak"]),
        "{{selfcheck_unsure}}": html.escape(facts["selfcheck"]["unsure"]),
        "{{subhub_html}}": render_subhub(capacity_file, generated_at),
        "{{generated_epoch}}": str(generated_at),
    }
    if tokens := TOKEN_RE.findall(document):
        unrendered_tokens = sorted(set(tokens).difference(replacements))
        if unrendered_tokens:
            raise ValueError(f"unrendered template tokens: {unrendered_tokens}")
    replacement_pattern = re.compile(
        "|".join(re.escape(token) for token in sorted(replacements, key=len, reverse=True))
    )
    document = replacement_pattern.sub(lambda match: replacements[match.group(0)], document)
    return document


def render_board(
    board_dir: Path, *, template_path: Path | None = None, capacity_file: Path | None = None,
    local_host: str | None = None, remote_hosts: set[str] | None = None, remote_user: str = DEFAULT_REMOTE_USER,
    expected_owner: str | None = None, valid_hosts: set[str] | None = None,
) -> Path:
    """Load ``facts.json`` and atomically replace ``index.html`` on success."""
    try:
        facts_path = board_dir / "facts.json"
        facts = json.loads(facts_path.read_text(encoding="utf-8"))
        if not isinstance(facts, dict):
            raise ValueError("facts.json root must be an object")
        template = template_path.read_text(encoding="utf-8") if template_path is not None else None
        output = render(
            facts,
            template=template,
            local_host=local_host,
            remote_hosts=remote_hosts,
            remote_user=remote_user,
            capacity_file=capacity_file,
            expected_owner=expected_owner,
            valid_hosts=valid_hosts,
        )
        index = board_dir / "index.html"
        index.parent.mkdir(parents=True, exist_ok=True)
        tmp = index.with_name(index.name + ".tmp")
        tmp.write_text(output, encoding="utf-8")
        tmp.replace(index)
        write_health(board_dir, True)
        return index
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        write_health(board_dir, False, str(exc))
        raise


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m chitra.board", description="Render Chitra board facts to index.html.")
    parser.add_argument("--board-dir", type=Path, default=default_board_dir())
    parser.add_argument("--template", type=Path, default=os.environ.get("CHITRA_BOARD_TEMPLATE"))
    parser.add_argument("--capacity-file", type=Path, default=os.environ.get("CHITRA_BOARD_CAPACITY_FILE"))
    parser.add_argument("--local-host", default=os.environ.get("CHITRA_BOARD_LOCAL_HOST"))
    parser.add_argument("--remote-hosts", default=os.environ.get("CHITRA_BOARD_REMOTE_HOSTS", ""))
    parser.add_argument("--remote-user", default=os.environ.get("CHITRA_BOARD_SSH_USER", DEFAULT_REMOTE_USER))
    parser.add_argument("--snapshot-owner", default=os.environ.get("CHITRA_BOARD_SNAPSHOT_OWNER"))
    parser.add_argument("--valid-hosts", default=os.environ.get("CHITRA_BOARD_VALID_HOSTS", ""))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        index = render_board(
            args.board_dir,
            template_path=args.template,
            capacity_file=args.capacity_file,
            local_host=args.local_host,
            remote_hosts=_env_hosts(args.remote_hosts),
            remote_user=args.remote_user,
            expected_owner=args.snapshot_owner,
            valid_hosts=_env_hosts(args.valid_hosts) or None,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        logger.error(
            "board_render_failed",
            board_dir=str(args.board_dir),
            index_path=str(args.board_dir / "index.html"),
            exc_info=True,
        )
        return 1
    logger.info("board_generated", board_dir=str(args.board_dir), index_path=str(index))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
