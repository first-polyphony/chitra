"""Render Chitra's deterministic operator-facing terminal roster.

The renderer consumes stored goal and artifact records without performing
classification or language-model work. A lane with unresolved open asks is an
operator-attention item; doctrine owns the marker, while this module surfaces
every stored ask in every roster.
"""

from __future__ import annotations

import os
import textwrap
import unicodedata
from collections.abc import Sequence
from typing import Literal, Protocol

from chitra.goals import GoalRecord, GoalStatus, done_when_with_delta, session_host, session_name

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
# Single toggle: box vs. cards is an open operator decision (see
# docs/SOL-ADVERSARIAL-REVIEW finding #8) -- not re-litigated here. This is
# Both render_roster()'s default fmt= and chitra-goals roster's --format
# default read this one constant, so the operator ruling stays centralized.
ROSTER_DEFAULT_FORMAT: Literal["cards", "box", "markdown"] = "markdown"
ROSTER_MARKERS: dict[GoalStatus, str] = {
    "blocked": "🔴",
    "held": "🟡",
    "idle": "🟡",
    "working": "🟢",
    "turn-finished-unverified": "🟡",
    "completion-disputed": "🔴",
    "done-pending-verification": "🟡",
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
    """Return the status-only marker, rejecting status outside the known states."""
    try:
        return ROSTER_MARKERS[status]
    except KeyError as exc:
        raise ValueError(f"unknown goal status: {status}") from exc


def compute_marker(record: RosterRecord) -> str:
    """Return the deterministic roster marker with this precedence.

    Open asks or ``blocked`` are red first because they need a named human
    unblock. ``held`` and ``idle`` are yellow because they are idle by design.
    A finished-but-unverified turn is yellow and a completion dispute is red;
    only genuine work or a verified completion awaiting close is green.
    """
    if record.open_asks or record.status in ("blocked", "completion-disputed"):
        return "🔴"
    if record.status in ("held", "idle", "turn-finished-unverified", "done-pending-verification"):
        return "🟡"
    if record.status in ("working", "done-pending-close"):
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
    """Word-wrap collapsed ``text`` into lines no wider than ``width`` DISPLAY
    columns (not code points) — emoji/CJK-aware, matching ``_pad``'s own
    measurement (see ``display_width``).

    ``textwrap.wrap`` counts code points, not terminal columns: a Goal/Now/
    Needs cell containing emoji or CJK text is under-counted (a wide
    character is 1 code point but renders 2 columns), so a wrapped line can
    still be wider than its column once ``_pad``'s display-width padding is
    applied — the frame overflows (reproduced at ``COLUMNS=100`` producing
    frame widths ``{100, 119}`` for a 20-emoji Goal; see
    docs/SOL-ADVERSARIAL-REVIEW finding #8). This wraps by display width
    directly instead. Wrapping (never single-line truncation) so a Goal's
    done-condition and a long Now/Needs survive intact — they just flow onto
    more lines. Overlong unbroken tokens are hard-split character-by-character
    by display width so a row can never exceed its column width regardless of
    glyph width.
    """
    compact = " ".join(text.split())
    if not compact:
        return [""]
    width = max(1, width)
    lines: list[str] = []
    current = ""
    current_width = 0
    for word in compact.split(" "):
        word_width = display_width(word)
        if word_width > width:
            # Overlong unbroken token: hard-split by display width, never by
            # code-point count, so no fragment can render wider than width.
            if current:
                lines.append(current)
                current = ""
                current_width = 0
            piece = ""
            piece_width = 0
            for char in word:
                char_width = _char_width(char)
                if piece and piece_width + char_width > width:
                    lines.append(piece)
                    piece = ""
                    piece_width = 0
                piece += char
                piece_width += char_width
            current, current_width = piece, piece_width
            continue
        candidate_width = current_width + (1 if current else 0) + word_width
        if current and candidate_width > width:
            lines.append(current)
            current, current_width = word, word_width
        else:
            current = f"{current} {word}" if current else word
            current_width = candidate_width
    if current:
        lines.append(current)
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


def _roster_done_when(record: RosterRecord) -> str:
    """Surface enrolled-to-current narrowing for canonical goal records."""
    if isinstance(record, GoalRecord):
        return done_when_with_delta(record)
    return record.done_when


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
                f"{_roster_goal(record)} — done: {_roster_done_when(record)}",
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


def _unreviewed_artifact_block(artifacts: Sequence[ArtifactRosterRecord], *, fmt: Literal["cards", "box", "markdown"]) -> str:
    """Render each injected unreviewed artifact on one unwrapped, copyable line."""
    unreviewed = sorted(
        (artifact for artifact in artifacts if artifact.review_status == "unreviewed"),
        key=lambda artifact: (artifact.published_at, artifact.url),
    )
    if not unreviewed:
        return ""
    prefix = "- " if fmt == "markdown" else "  • "
    return "\n".join(("UNREVIEWED ARTIFACTS:", *(f"{prefix}{artifact.title} — {artifact.url}" for artifact in unreviewed)))


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
        block += field("Goal", f"{_roster_goal(record)}  ·  done: {_roster_done_when(record)}")
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
    fmt: Literal["cards", "box", "markdown"] = ROSTER_DEFAULT_FORMAT,
    artifacts: Sequence[ArtifactRosterRecord] = (),
) -> str:
    """Render every stored lane, stable order (host, session name, full ref).

    Default (``ROSTER_DEFAULT_FORMAT``, currently ``markdown``): a
    client-rendered table. ``cards``: one labelled stanza per lane (readable
    full sentences). ``box``: the fixed-column table. Artifact records are
    injected by the caller; their URLs are emitted in a separate unwrapped
    block so operators can copy each complete URL.
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
        wrapped = [[row[0]] if index == 0 else _wrap_cell(row[index], widths[index]) for index in range(len(row))]
        height = max(len(cell) for cell in wrapped)
        out: list[str] = []
        for r in range(height):
            segments = [" " + _pad(cell[r] if r < len(cell) else "", widths[index]) + " " for index, cell in enumerate(wrapped)]
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
