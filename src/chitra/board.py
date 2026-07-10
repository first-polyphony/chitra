"""Render Chitra's validated board facts into the operator-facing HTML board.

The renderer is deliberately a consumer of ``facts.json``: it performs no
classification or language-model work.  It renders the bundled, accessible
template atomically, records success/failure in ``health.json``, and can show
short tmux tails for configured local or remote hosts.
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
import time
from importlib.resources import files
from pathlib import Path
from typing import Any

import structlog

from chitra.board_updater import validate_board_facts
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
