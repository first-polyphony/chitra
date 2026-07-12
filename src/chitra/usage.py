"""Read provider usage snapshots and evaluate deterministic rate-limit policy.

This module only reads snapshots and applies caller-configured thresholds.  It
does not pause, resume, dispatch to, or otherwise make decisions for sessions.
"""

from __future__ import annotations

import argparse
import base64
import json
import select
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from chitra.policy_config import UsagePolicy, load_policy_config

SCHEMA = "chitra.usage.v1"
UsageKind = Literal["claude", "codex"]
VerdictLevel = Literal["ok", "approaching", "pause"]
AccountedVerdictLevel = Literal["unknown", "ok", "approaching", "pause"]
CodexProcessFactory = Callable[..., subprocess.Popen[str]]
CodexClock = Callable[[], float]
DEFAULT_USAGE_POLICY = UsagePolicy()
DEFAULT_CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"


class CodexSnapshotError(RuntimeError):
    """Raised when the local Codex app-server cannot provide a usage snapshot."""


@dataclass(frozen=True, slots=True)
class UsageWindow:
    """One provider usage window expressed as a percentage and reset epoch."""

    pct: float
    resets_at: int

    @classmethod
    def from_dict(cls, payload: object, *, field_name: str) -> UsageWindow:
        if not isinstance(payload, dict):
            raise ValueError(f"usage snapshot {field_name} must be an object or null")
        pct = payload.get("pct")
        resets_at = payload.get("resets_at")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 <= pct <= 100:
            raise ValueError(f"usage snapshot {field_name}.pct must be a number from 0 through 100")
        if isinstance(resets_at, bool) or not isinstance(resets_at, int):
            raise ValueError(f"usage snapshot {field_name}.resets_at must be an integer epoch")
        return cls(pct=float(pct), resets_at=resets_at)

    def to_dict(self) -> dict[str, float | int]:
        return {"pct": self.pct, "resets_at": self.resets_at}


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """A strict on-disk ``chitra.usage.v1`` provider usage snapshot."""

    kind: UsageKind
    ts: str
    session_id: str
    tmux_session: str
    five_hour: UsageWindow | None
    seven_day: UsageWindow | None
    account: str = ""

    @classmethod
    def from_dict(cls, payload: object) -> UsageSnapshot:
        if not isinstance(payload, dict):
            raise ValueError("usage snapshot must be an object")
        if payload.get("schema") != SCHEMA:
            raise ValueError("usage snapshot is not a chitra.usage.v1 document")
        kind = payload.get("kind")
        if kind not in ("claude", "codex"):
            raise ValueError("usage snapshot kind must be claude or codex")
        values: dict[str, str] = {}
        for field in ("ts", "session_id", "tmux_session"):
            value = payload.get(field)
            if not isinstance(value, str):
                raise ValueError(f"usage snapshot {field} must be a string")
            values[field] = value
        _parse_utc_timestamp(values["ts"])
        return cls(
            kind=cast(UsageKind, kind),
            ts=values["ts"],
            session_id=values["session_id"],
            tmux_session=values["tmux_session"],
            five_hour=_window_from_payload(payload, "five_hour"),
            seven_day=_window_from_payload(payload, "seven_day"),
            account=_account_from_payload(payload),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "kind": self.kind,
            "ts": self.ts,
            "session_id": self.session_id,
            "tmux_session": self.tmux_session,
            "account": self.account,
            "five_hour": None if self.five_hour is None else self.five_hour.to_dict(),
            "seven_day": None if self.seven_day is None else self.seven_day.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class Verdict:
    """The deterministic policy result for exactly one usage snapshot."""

    level: VerdictLevel
    binding_window: Literal["", "5h", "7d"]
    resume_at_epoch: int


@dataclass(frozen=True, slots=True)
class AccountedVerdict:
    """One session's verdict after attributing fresh usage to its account."""

    session_id: str
    tmux_session: str
    kind: UsageKind
    account: str
    level: AccountedVerdictLevel
    binding_window: Literal["", "5h", "7d"]
    resume_at_epoch: int
    self_fresh: bool
    account_attributed: bool


def _parse_utc_timestamp(value: str) -> datetime:
    """Parse an ISO8601 timestamp whose offset is explicitly UTC."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("usage snapshot ts must be an ISO8601-UTC datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("usage snapshot ts must be an ISO8601-UTC datetime")
    return parsed.astimezone(UTC)


def _window_from_payload(payload: dict[str, object], field_name: str) -> UsageWindow | None:
    raw_window = payload.get(field_name)
    if raw_window is None:
        return None
    return UsageWindow.from_dict(raw_window, field_name=field_name)


def _account_from_payload(payload: dict[str, object]) -> str:
    """Read optional account identity retained by older persisted snapshots."""
    account = payload.get("account", "")
    if not isinstance(account, str):
        raise ValueError("usage snapshot account must be a string")
    return account


def read_snapshots(
    directory: Path, *, staleness_seconds: int = 1200, now: datetime | None = None
) -> list[tuple[UsageSnapshot, bool]]:
    """Read snapshot files and flag whether each one is within the staleness window."""
    if staleness_seconds < 0:
        raise ValueError("staleness_seconds must be non-negative")
    if not directory.exists():
        return []
    if now is not None and now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = datetime.now(UTC) if now is None else now.astimezone(UTC)
    snapshots: list[tuple[UsageSnapshot, bool]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            snapshot = UsageSnapshot.from_dict(payload)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"malformed usage snapshot {path}: {exc}") from exc
        age_seconds = (current - _parse_utc_timestamp(snapshot.ts)).total_seconds()
        snapshots.append((snapshot, age_seconds <= staleness_seconds))
    return snapshots


def _binding_window(
    five_hour: UsageWindow | None,
    seven_day: UsageWindow | None,
    *,
    five_hour_threshold: float,
    seven_day_threshold: float,
) -> Literal["", "5h", "7d"]:
    candidates: list[tuple[float, Literal["5h", "7d"]]] = []
    if five_hour is not None and five_hour.pct >= five_hour_threshold:
        candidates.append((five_hour.pct - five_hour_threshold, "5h"))
    if seven_day is not None and seven_day.pct >= seven_day_threshold:
        candidates.append((seven_day.pct - seven_day_threshold, "7d"))
    if not candidates:
        return ""
    return max(candidates, key=lambda item: (item[0], item[1] == "7d"))[1]


def evaluate(
    snapshot: UsageSnapshot,
    *,
    pause_5h: float | None = None,
    pause_7d: float | None = None,
    warn_5h: float | None = None,
    warn_7d: float | None = None,
    policy: UsagePolicy | None = None,
) -> Verdict:
    """Return the pure deterministic policy verdict for one snapshot."""
    configured = DEFAULT_USAGE_POLICY if policy is None else policy
    resolved_pause_5h = configured.pause_5h_pct if pause_5h is None else pause_5h
    resolved_pause_7d = configured.pause_7d_pct if pause_7d is None else pause_7d
    resolved_warn_5h = configured.warn_5h_pct if warn_5h is None else warn_5h
    resolved_warn_7d = configured.warn_7d_pct if warn_7d is None else warn_7d
    pause_binding = _binding_window(
        snapshot.five_hour,
        snapshot.seven_day,
        five_hour_threshold=resolved_pause_5h,
        seven_day_threshold=resolved_pause_7d,
    )
    if pause_binding:
        window = snapshot.five_hour if pause_binding == "5h" else snapshot.seven_day
        assert window is not None
        return Verdict(level="pause", binding_window=pause_binding, resume_at_epoch=window.resets_at)
    warn_binding = _binding_window(
        snapshot.five_hour,
        snapshot.seven_day,
        five_hour_threshold=resolved_warn_5h,
        seven_day_threshold=resolved_warn_7d,
    )
    if warn_binding:
        return Verdict(level="approaching", binding_window=warn_binding, resume_at_epoch=0)
    return Verdict(level="ok", binding_window="", resume_at_epoch=0)


def _verdict_binding_pct(snapshot: UsageSnapshot, verdict: Verdict) -> float:
    if verdict.binding_window == "5h":
        assert snapshot.five_hour is not None
        return snapshot.five_hour.pct
    if verdict.binding_window == "7d":
        assert snapshot.seven_day is not None
        return snapshot.seven_day.pct
    return 0


def _account_group_key(snapshot: UsageSnapshot) -> str:
    """Return the account-grouping key for one snapshot: the account itself
    when known, or a per-session singleton key when unknown.

    Fail closed on unknown account identity: an empty ``account`` field
    (both status-input and ``.claude.json`` account lookup failed upstream,
    per ``chitra-usage-snapshot``) must never be treated as a shared
    identity with every OTHER unknown-account session. Grouping every ""
    account together would let one fresh, hot, unknown-identity session
    attribute its pause verdict to every unrelated unknown-identity sibling
    (see docs/SOL-ADVERSARIAL-REVIEW finding #6). Each blank-account
    snapshot is instead its own isolated group, keyed by session_id, so it
    can only ever receive its OWN verdict -- never another session's.
    """
    return snapshot.account if snapshot.account else f"\0unknown:{snapshot.session_id}"


def evaluate_grouped(
    items: list[tuple[UsageSnapshot, bool]], *, policy: UsagePolicy
) -> list[AccountedVerdict]:
    """Evaluate fresh readings by account, sorted by account then input order.

    Every input session receives its account's verdict, including stale siblings
    whose sidecar snapshots no longer refresh. A session with no known account
    identity is never merged with another unknown-identity session -- see
    ``_account_group_key``.
    """
    groups: dict[str, list[tuple[UsageSnapshot, bool]]] = {}
    for item in items:
        groups.setdefault(_account_group_key(item[0]), []).append(item)

    results: list[AccountedVerdict] = []
    severity = {"ok": 0, "approaching": 1, "pause": 2}
    for account in sorted(groups):
        group = groups[account]
        candidates = [(snapshot, evaluate(snapshot, policy=policy)) for snapshot, fresh in group if fresh]
        if candidates:
            _, account_verdict = max(
                candidates,
                key=lambda item: (
                    severity[item[1].level],
                    _verdict_binding_pct(item[0], item[1]),
                    item[1].binding_window == "7d",
                ),
            )
            level: AccountedVerdictLevel = account_verdict.level
            binding_window = account_verdict.binding_window
            resume_at_epoch = account_verdict.resume_at_epoch
        else:
            level = "unknown"
            binding_window = ""
            resume_at_epoch = 0
        for snapshot, fresh in group:
            results.append(
                AccountedVerdict(
                    session_id=snapshot.session_id,
                    tmux_session=snapshot.tmux_session,
                    kind=snapshot.kind,
                    account=snapshot.account,  # the real (possibly empty) account, never the internal grouping key
                    level=level,
                    binding_window=binding_window,
                    resume_at_epoch=resume_at_epoch,
                    self_fresh=fresh,
                    account_attributed=not fresh and level in ("pause", "approaching"),
                )
            )
    return results


def _start_codex(command: Sequence[str]) -> subprocess.Popen[str]:
    """Start the local app-server without a shell and with line-buffered pipes."""
    return subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )


def _write_request(process: subprocess.Popen[str], payload: dict[str, object]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()


def _read_response(
    process: subprocess.Popen[str],
    *,
    request_id: int,
    deadline: float,
    clock: CodexClock,
    require_result: bool,
) -> dict[str, object]:
    assert process.stdout is not None
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise CodexSnapshotError("codex app-server did not respond within 15 seconds")
        returncode = process.poll()
        if returncode is not None:
            raise CodexSnapshotError(f"codex app-server failed ({returncode}): ")
        try:
            ready, _, _ = select.select([process.stdout], [], [], remaining)
        except (OSError, ValueError):
            line = process.stdout.readline()
        else:
            if not ready:
                raise CodexSnapshotError("codex app-server did not respond within 15 seconds")
            line = process.stdout.readline()
        try:
            response = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(response, dict) or response.get("id") != request_id:
            continue
        if not require_result:
            return {}
        result = response.get("result")
        if isinstance(result, dict):
            return cast(dict[str, object], result)
        raise CodexSnapshotError("codex app-server returned no account/rateLimits/read response")


def _stop_codex(process: subprocess.Popen[str]) -> None:
    """Terminate an app-server process and release its pipe handles."""
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _required_number(payload: dict[str, object], names: tuple[str, ...], *, label: str) -> float:
    for name in names:
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return float(value)
    raise CodexSnapshotError(f"codex rate-limit {label} is missing or invalid")


def _codex_window(payload: object, *, now_epoch: int, label: str) -> UsageWindow | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise CodexSnapshotError(f"codex rate-limit {label} must be an object or null")
    pct = _required_number(payload, ("used_percent", "usedPercent"), label=f"{label} used percent")
    if not 0 <= pct <= 100:
        raise CodexSnapshotError(f"codex rate-limit {label} used percent must be from 0 through 100")
    for name in ("resets_at", "resetsAt"):
        value = payload.get(name)
        if not isinstance(value, bool) and isinstance(value, int):
            return UsageWindow(pct=pct, resets_at=value)
    for name in ("resets_in_seconds", "resetsInSeconds"):
        value = payload.get(name)
        if not isinstance(value, bool) and isinstance(value, int):
            return UsageWindow(pct=pct, resets_at=now_epoch + value)
    raise CodexSnapshotError(f"codex rate-limit {label} reset time is missing or invalid")


def _codex_account(auth_path: Path) -> str:
    """Read the account email from Codex's local JWT without affecting usage reads."""
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
        token = auth["tokens"]["id_token"]
        if not isinstance(token, str):
            return ""
        segments = token.split(".")
        if len(segments) != 3:
            return ""
        payload = segments[1] + "=" * (-len(segments[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        if not isinstance(claims, dict):
            return ""
        email = claims.get("email")
        if isinstance(email, str):
            return email
        profile = claims.get("https://api.openai.com/profile")
        return profile.get("email", "") if isinstance(profile, dict) and isinstance(profile.get("email"), str) else ""
    except (KeyError, OSError, TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return ""


def codex_snapshot(
    *,
    codex_bin: Path | str = "codex",
    now: datetime | None = None,
    process_factory: CodexProcessFactory = _start_codex,
    clock: CodexClock = time.monotonic,
    auth_path: Path = DEFAULT_CODEX_AUTH_PATH,
) -> UsageSnapshot:
    """Read the local Codex account's two rate-limit windows through app-server."""
    if now is not None and now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = datetime.now(UTC) if now is None else now.astimezone(UTC)
    command = [str(codex_bin), "app-server", "--stdio"]
    try:
        deadline = clock() + 15
        process = process_factory(command)
    except FileNotFoundError as exc:
        raise CodexSnapshotError(f"codex binary was not found: {codex_bin}") from exc
    try:
        _write_request(
            process,
            {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "chitra-usage", "version": "1"}}},
        )
        _read_response(process, request_id=1, deadline=deadline, clock=clock, require_result=False)
        _write_request(process, {"method": "initialized"})
        _write_request(process, {"id": 2, "method": "account/rateLimits/read", "params": None})
        result = _read_response(process, request_id=2, deadline=deadline, clock=clock, require_result=True)
    finally:
        _stop_codex(process)
    rate_limits = result.get("rateLimits")
    if not isinstance(rate_limits, dict):
        raise CodexSnapshotError("codex response is missing rateLimits")
    return UsageSnapshot(
        kind="codex",
        ts=current.isoformat(),
        session_id="codex-account",
        tmux_session="",
        five_hour=_codex_window(rate_limits.get("primary"), now_epoch=int(current.timestamp()), label="primary"),
        seven_day=_codex_window(rate_limits.get("secondary"), now_epoch=int(current.timestamp()), label="secondary"),
        account=_codex_account(auth_path),
    )


def _snapshot_with_fresh(snapshot: UsageSnapshot, fresh: bool) -> dict[str, object]:
    payload = snapshot.to_dict()
    payload["fresh"] = fresh
    return payload


def _evaluation_output(verdict: AccountedVerdict) -> dict[str, object]:
    return {
        "session_id": verdict.session_id,
        "tmux_session": verdict.tmux_session,
        "kind": verdict.kind,
        "account": verdict.account,
        "level": verdict.level,
        "binding_window": verdict.binding_window,
        "resume_at_epoch": verdict.resume_at_epoch,
        "resume_at_iso": datetime.fromtimestamp(verdict.resume_at_epoch, UTC).isoformat()
        if verdict.resume_at_epoch
        else "",
        "self_fresh": verdict.self_fresh,
        "account_attributed": verdict.account_attributed,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chitra-usage", description="Read deterministic provider usage snapshots and evaluate thresholds."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    read_claude = commands.add_parser("read-claude", help="Read Claude statusline sidecar snapshots.")
    read_claude.add_argument("--dir", type=Path, required=True)
    read_claude.add_argument("--staleness-seconds", type=int, default=1200)
    read_claude.add_argument("--json", action="store_true")

    codex = commands.add_parser("codex-snapshot", help="Read the local Codex account usage snapshot.")
    codex.add_argument("--codex-bin", type=Path, default=Path("codex"))

    evaluate_command = commands.add_parser("evaluate", help="Evaluate fresh snapshots against deterministic thresholds.")
    evaluate_command.add_argument("--dir", type=Path, required=True)
    evaluate_command.add_argument("--codex", action="store_true")
    evaluate_command.add_argument("--codex-bin", type=Path, default=Path("codex"))
    evaluate_command.add_argument("--staleness-seconds", type=int, default=1200)
    evaluate_command.add_argument("--policy-config", type=Path)
    for option in ("pause-5h", "pause-7d", "warn-5h", "warn-7d"):
        evaluate_command.add_argument(f"--{option}", type=float, default=None)

    policy_command = commands.add_parser("policy", help="Print the effective usage policy as JSON.")
    policy_command.add_argument("--policy-config", type=Path)
    return parser


def _usage_policy_with_cli_overrides(args: argparse.Namespace) -> UsagePolicy:
    """Load one policy file and apply explicit command-line threshold overrides."""
    configured = load_policy_config(args.policy_config).usage
    return UsagePolicy.model_validate(
        {
            **configured.model_dump(),
            "pause_5h_pct": configured.pause_5h_pct if args.pause_5h is None else args.pause_5h,
            "pause_7d_pct": configured.pause_7d_pct if args.pause_7d is None else args.pause_7d,
            "warn_5h_pct": configured.warn_5h_pct if args.warn_5h is None else args.warn_5h,
            "warn_7d_pct": configured.warn_7d_pct if args.warn_7d is None else args.warn_7d,
        }
    )


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if args.command == "read-claude":
            snapshots = read_snapshots(args.dir, staleness_seconds=args.staleness_seconds)
            payload = [_snapshot_with_fresh(snapshot, fresh) for snapshot, fresh in snapshots]
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                for item in payload:
                    print(json.dumps(item, sort_keys=True))
        elif args.command == "codex-snapshot":
            print(json.dumps(codex_snapshot(codex_bin=args.codex_bin).to_dict(), indent=2, sort_keys=True))
        elif args.command == "policy":
            print(json.dumps(load_policy_config(args.policy_config).usage.model_dump(), indent=2, sort_keys=True))
        else:
            policy = _usage_policy_with_cli_overrides(args)
            snapshots = read_snapshots(args.dir, staleness_seconds=args.staleness_seconds)
            if args.codex:
                snapshots.append((codex_snapshot(codex_bin=args.codex_bin), True))
            for verdict in evaluate_grouped(snapshots, policy=policy):
                print(json.dumps(_evaluation_output(verdict), sort_keys=True))
    except (CodexSnapshotError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"chitra-usage: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
