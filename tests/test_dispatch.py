"""Tests for chitra.dispatch: pane_in_mode/-p fixes, transcript verification,
and LaneLock single-writer enforcement."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from chitra.dispatch import (
    DISPATCH_VERIFY_WAIT_SECONDS,
    DispatchOrder,
    DispatchStatus,
    LaneLock,
    LaneLockError,
    _remote_transcript_grep_command,
    cancel_copy_mode,
    directive_voice_violation,
    dispatch_to_tmux,
    ensure_pane_not_in_mode,
    find_recent_transcript,
    find_recent_transcript_remote,
    pane_capture_confirms_nudge,
    pane_in_mode,
    pane_input_check,
    paste_nudge_to_local_tmux,
    remote_tmux_paste_command,
    ssh_command,
    tmux_pane_target,
    transcript_confirms_nudge,
    transcript_glob,
)
from chitra.policy_config import DispatchPolicy, PolicyConfig

HAS_TMUX = shutil.which("tmux") is not None


def fake_completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Build a real ``subprocess.CompletedProcess[str]`` for a scripted fake
    runner — matches the ``TmuxRunner``/``TmuxInputRunner`` protocol's return
    type exactly, unlike a hand-rolled duck-typed stand-in."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeRunner:
    """Records every command it's asked to run and returns scripted results."""

    def __init__(
        self,
        script: dict[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None,
        default: subprocess.CompletedProcess[str] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.script = script or {}
        self.default = default or fake_completed(0, "", "")

    def __call__(self, cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        return self.script.get(tuple(cmd), self.default)


class FakeInputRunner:
    def __init__(self, default: subprocess.CompletedProcess[str] | None = None) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.default = default or fake_completed(0, "", "")

    def __call__(self, cmd: list[str], payload: str, *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        self.calls.append((cmd, payload))
        return self.default


# --- (1) pane_in_mode detection + cancel logic ---------------------------


def test_pane_in_mode_true_when_display_message_returns_1() -> None:
    runner = FakeRunner(default=fake_completed(0, "1\n", ""))
    assert pane_in_mode("session:0.0", runner=runner) is True


def test_pane_in_mode_false_when_display_message_returns_0() -> None:
    runner = FakeRunner(default=fake_completed(0, "0\n", ""))
    assert pane_in_mode("session:0.0", runner=runner) is False


def test_ensure_pane_not_in_mode_cancels_when_in_copy_mode() -> None:
    calls: list[list[str]] = []

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:2] == ["tmux", "display-message"]:
            return fake_completed(0, "1\n", "")
        return fake_completed(0, "", "")

    ok = ensure_pane_not_in_mode("session:0.0", runner=runner)
    assert ok is True
    assert any(cmd[:4] == ["tmux", "send-keys", "-t", "session:0.0"] and "-X" in cmd for cmd in calls)


def test_cancel_copy_mode_returns_false_on_failure() -> None:
    runner = FakeRunner(default=fake_completed(1, "", "no such pane"))
    assert cancel_copy_mode("session:0.0", runner=runner, wait_seconds=0) is False


def test_pane_in_mode_checks_remote_host_over_ssh_not_local_tmux() -> None:
    """Regression test for the copy-mode-checks-the-wrong-host bug: a remote
    target's copy-mode state must be checked via ssh against the remote
    tmux server, never via a bare local ``tmux`` invocation."""
    runner = FakeRunner(default=fake_completed(0, "1\n", ""))
    result = pane_in_mode("f3:0.0", host="otherhost", runner=runner, local_extra={"localhost"})
    assert result is True
    assert len(runner.calls) == 1
    cmd = runner.calls[0]
    assert cmd[0] == "ssh"
    assert cmd[-2] == "otherhost"
    assert "tmux display-message -p -t f3:0.0" in cmd[-1]


def test_pane_in_mode_local_host_uses_bare_tmux_call() -> None:
    runner = FakeRunner(default=fake_completed(0, "0\n", ""))
    result = pane_in_mode("f3:0.0", host="localhost", runner=runner, local_extra={"localhost"})
    assert result is False
    assert runner.calls == [["tmux", "display-message", "-p", "-t", "f3:0.0", "#{pane_in_mode}"]]


def test_cancel_copy_mode_cancels_remote_host_over_ssh() -> None:
    runner = FakeRunner(default=fake_completed(0, "", ""))
    ok = cancel_copy_mode("f3:0.0", host="otherhost", runner=runner, local_extra={"localhost"}, wait_seconds=0)
    assert ok is True
    assert len(runner.calls) == 1
    cmd = runner.calls[0]
    assert cmd[0] == "ssh"
    assert cmd[-2] == "otherhost"
    assert "tmux send-keys -t f3:0.0 -X cancel" in cmd[-1]


def test_ensure_pane_not_in_mode_cancels_remote_host_over_ssh_when_in_copy_mode() -> None:
    calls: list[list[str]] = []

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[0] == "ssh" and "display-message" in cmd[-1]:
            return fake_completed(0, "1\n", "")
        return fake_completed(0, "", "")

    ok = ensure_pane_not_in_mode("f3:0.0", host="otherhost", runner=runner, local_extra={"localhost"})
    assert ok is True
    assert all(cmd[0] == "ssh" and cmd[-2] == "otherhost" for cmd in calls)
    assert any("send-keys" in cmd[-1] and "-X cancel" in cmd[-1] for cmd in calls)


# --- (2) -p is present in the constructed paste-buffer command ------------


def test_local_paste_command_includes_dash_p_flag() -> None:
    runner = FakeRunner()
    input_runner = FakeInputRunner()
    paste_nudge_to_local_tmux("session:0.0", "hello\nworld", runner=runner, input_runner=input_runner)
    paste_calls = [c for c in runner.calls if c[:2] == ["tmux", "paste-buffer"]]
    assert paste_calls, "expected a paste-buffer call"
    assert "-p" in paste_calls[0]


def test_remote_paste_command_includes_dash_p_flag() -> None:
    command = remote_tmux_paste_command("session:0.0", "hello")
    assert "paste-buffer -p" in command or "paste-buffer' '-p'" in command
    assert " -p " in command


# --- (3) transcript-grep verification against a synthetic fixture --------


def test_transcript_confirms_nudge_finds_marker(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    session_dir = projects_root / "some-project"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "abc123.jsonl"
    transcript.write_text(json.dumps({"text": "please check lane f3 status now"}) + "\n", encoding="utf-8")

    confirmed, path = transcript_confirms_nudge(
        "please check lane f3 status now",
        projects_root=projects_root,
        now_ts=time.time(),
    )
    assert confirmed is True
    assert path == transcript


def test_transcript_confirms_nudge_excludes_given_path(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    session_dir = projects_root / "some-project"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "abc123.jsonl"
    transcript.write_text("marker-text-here", encoding="utf-8")

    confirmed, path = transcript_confirms_nudge(
        "marker-text-here",
        projects_root=projects_root,
        exclude_paths={transcript},
        now_ts=time.time(),
    )
    assert confirmed is False
    assert path is None


def test_remote_transcript_find_script_expands_default_tilde_root() -> None:
    script = _remote_transcript_grep_command("marker", "~/.claude/projects", 300)

    assert 'root="$HOME"/.claude/projects' in script
    assert "'~/.claude/projects'" not in script
    assert "~/.claude/projects" not in script
    assert "grep" not in script


def test_remote_transcript_find_script_quotes_absolute_custom_root() -> None:
    script = _remote_transcript_grep_command("marker", "/srv/Claude Projects", 300)

    assert "root='/srv/Claude Projects'" in script
    assert '-path "$root"/' in script


def test_find_recent_transcript_remote_matches_tail_from_ssh_output() -> None:
    """Remote transcript candidates are located over ssh and their tails are
    compared locally before a delivery can become SENT."""

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if "find " in cmd[-1]:
            return fake_completed(0, "1720000000 /home/ubuntu/.claude/projects/foo/abc.jsonl\n", "")
        return fake_completed(0, '{"text": "please check lane f3 status now"}\n', "")

    path = find_recent_transcript_remote("otherhost", "please check lane f3 status now", runner=runner)
    assert path == "/home/ubuntu/.claude/projects/foo/abc.jsonl"

def test_find_recent_transcript_remote_matches_json_escaped_quote_and_whitespace() -> None:
    path = "/remote/projects/foo/abc.jsonl"

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if "find " in cmd[-1]:
            return fake_completed(0, f"1720000000 {path}\n", "")
        return fake_completed(0, json.dumps({"text": 'please say "hello"   now'}) + "\n", "")

    confirmed, found = transcript_confirms_nudge(
        'please say "hello"     now',
        host="otherhost",
        runner=runner,
        local_extra={"localhost"},
    )
    assert confirmed is True
    assert found == path


def test_find_recent_transcript_remote_find_command_never_quotes_default_tilde() -> None:
    runner = FakeRunner(default=fake_completed(0, "", ""))
    assert find_recent_transcript_remote("otherhost", "marker text", runner=runner) is None

    assert len(runner.calls) == 1
    cmd = runner.calls[0]
    assert cmd[0] == "ssh"
    assert cmd[-2] == "otherhost"
    assert "'~/.claude/projects'" not in cmd[-1]
    assert 'root="$HOME"/.claude/projects' in cmd[-1]


def test_find_recent_transcript_remote_picks_most_recent_of_multiple_matches() -> None:
    stdout = "1000 /old/path.jsonl\n2000 /new/path.jsonl\n"

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if "find " in cmd[-1]:
            return fake_completed(0, stdout, "")
        return fake_completed(0, "marker text", "")

    path = find_recent_transcript_remote("otherhost", "marker text", runner=runner)
    assert path == "/new/path.jsonl"


def test_find_recent_transcript_remote_returns_none_on_no_match() -> None:
    runner = FakeRunner(default=fake_completed(0, "", ""))
    assert find_recent_transcript_remote("otherhost", "marker text", runner=runner) is None


def test_find_recent_transcript_remote_returns_none_on_ssh_failure() -> None:
    runner = FakeRunner(default=fake_completed(255, "", "ssh: connect timed out"))
    assert find_recent_transcript_remote("otherhost", "marker text", runner=runner) is None


def test_transcript_confirms_nudge_uses_remote_transcript_for_a_remote_host() -> None:
    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if "find " in cmd[-1]:
            return fake_completed(0, "1720000000 /remote/projects/foo/abc.jsonl\n", "")
        return fake_completed(0, "please check lane f3 status now", "")

    confirmed, path = transcript_confirms_nudge(
        "please check lane f3 status now",
        host="otherhost",
        runner=runner,
        local_extra={"localhost"},
    )
    assert confirmed is True
    assert path == "/remote/projects/foo/abc.jsonl"


def test_transcript_confirms_nudge_stays_local_for_a_local_host(tmp_path: Path) -> None:
    """host="" (default) or a recognized-local host must not change existing
    local-only behavior."""
    projects_root = tmp_path / "projects"
    session_dir = projects_root / "some-project"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "abc123.jsonl"
    transcript.write_text(json.dumps({"text": "please check lane f3 status now"}) + "\n", encoding="utf-8")

    confirmed, path = transcript_confirms_nudge(
        "please check lane f3 status now",
        host="localhost",
        projects_root=projects_root,
        local_extra={"localhost"},
        now_ts=time.time(),
    )
    assert confirmed is True
    assert path == transcript


def test_find_recent_transcript_respects_recency_window(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    session_dir = projects_root / "some-project"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "old.jsonl"
    transcript.write_text("the marker text", encoding="utf-8")
    old_mtime = time.time() - 10_000
    os.utime(transcript, (old_mtime, old_mtime))

    found = find_recent_transcript("the marker text", projects_root=projects_root, recency_seconds=300, now_ts=time.time())
    assert found is None


# --- (8) LaneLock: single-writer enforcement ------------------------------


def test_lane_lock_second_acquire_fails_non_blocking(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    lock_a = LaneLock("examplehost:f3:0.0", lock_dir=lock_dir)
    lock_b = LaneLock("examplehost:f3:0.0", lock_dir=lock_dir)
    assert lock_a.acquire(blocking=False) is True
    assert lock_b.acquire(blocking=False) is False
    lock_a.release()
    assert lock_b.acquire(blocking=False) is True
    lock_b.release()


def test_lane_lock_blocking_raises_after_timeout(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    lock_a = LaneLock("examplehost:f3:0.0", lock_dir=lock_dir)
    lock_b = LaneLock("examplehost:f3:0.0", lock_dir=lock_dir)
    assert lock_a.acquire(blocking=False) is True
    with pytest.raises(LaneLockError):
        lock_b.acquire(blocking=True, poll_seconds=0.01, timeout_seconds=0.05)
    lock_a.release()


def test_lane_lock_reclaims_stale_lock_from_dead_pid(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True)
    stale = lock_dir / "lane-examplehost_f3_0.0.lock"
    # A pid that is (almost certainly) not alive.
    stale.write_text(json.dumps({"pid": 999999, "session_ref": "examplehost:f3:0.0", "at": "x"}), encoding="utf-8")
    lock = LaneLock("examplehost:f3:0.0", lock_dir=lock_dir)
    assert lock.acquire(blocking=False) is True
    lock.release()


def test_lane_lock_context_manager_releases_on_exit(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    session_ref = "examplehost:f3:0.0"
    with LaneLock(session_ref, lock_dir=lock_dir) as lock:
        assert lock.acquired is True
        other = LaneLock(session_ref, lock_dir=lock_dir)
        assert other.acquire(blocking=False) is False
    other2 = LaneLock(session_ref, lock_dir=lock_dir)
    assert other2.acquire(blocking=False) is True
    other2.release()


# --- tmux_pane_target ------------------------------------------------------


def test_tmux_pane_target_qualifies_a_bare_pane_with_its_session() -> None:
    assert tmux_pane_target("f3", "0.0") == "f3:0.0"


def test_tmux_pane_target_leaves_an_already_qualified_target_alone() -> None:
    assert tmux_pane_target("f3", "other-session:0.0") == "other-session:0.0"


def test_tmux_pane_target_leaves_a_global_pane_id_alone() -> None:
    assert tmux_pane_target("f3", "%42") == "%42"


def test_dispatch_to_tmux_qualifies_pane_with_session_before_any_tmux_call() -> None:
    """Regression test: capture/paste/etc must never receive a bare pane
    spec — on a host running more than one tmux session, that resolves
    against whichever session tmux considers 'current', not the session
    named in session_ref."""
    seen_targets: list[str] = []

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if "-t" in cmd:
            seen_targets.append(cmd[cmd.index("-t") + 1])
        if cmd[:2] == ["tmux", "capture-pane"]:
            return fake_completed(0, "ubuntu@host:~$ ", "")
        return fake_completed(0, "", "")

    def input_runner(cmd: list[str], payload: str, *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        return fake_completed(0, "", "")

    order = DispatchOrder(order_id="o1", session_ref="localhost:f3:0.0", nudge="hello")
    dispatch_to_tmux(order, runner=runner, input_runner=input_runner, local_extra={"localhost"})

    assert seen_targets, "expected at least one -t target to have been recorded"
    assert all(t == "f3:0.0" for t in seen_targets), seen_targets


def test_pane_capture_confirms_nudge_true_when_marker_visible() -> None:
    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["tmux", "capture-pane"]:
            return fake_completed(0, "scrollback line\n❯ please check lane f3 status now\n", "")
        return fake_completed(0, "", "")

    assert (
        pane_capture_confirms_nudge(
            "please check lane f3 status now",
            host="localhost",
            pane="f3:0.0",
            runner=runner,
            local_extra={"localhost"},
        )
        is True
    )


def test_pane_capture_confirms_nudge_false_when_marker_absent() -> None:
    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["tmux", "capture-pane"]:
            return fake_completed(0, "unrelated pane content\n❯ \n", "")
        return fake_completed(0, "", "")

    assert (
        pane_capture_confirms_nudge(
            "please check lane f3 status now",
            host="localhost",
            pane="f3:0.0",
            runner=runner,
            local_extra={"localhost"},
        )
        is False
    )


def test_dispatch_to_tmux_falls_back_to_pane_capture_when_transcript_missing(tmp_path: Path) -> None:
    """Regression: a mechanically-successful send whose transcript can't be
    located must NOT report FAILED. When transcript-grep finds nothing but the
    pane shows the delivered nudge, the result is SENT via pane-capture
    fallback."""
    empty_projects = tmp_path / "projects"
    empty_projects.mkdir()
    captures = {"n": 0}

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["tmux", "capture-pane"]:
            captures["n"] += 1
            if captures["n"] == 1:
                return fake_completed(0, "ubuntu@host:~$ ", "")  # pre-check: idle
            return fake_completed(0, "❯ diagnose the failing build\n", "")  # fallback: marker visible
        return fake_completed(0, "", "")

    def input_runner(cmd: list[str], payload: str, *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        return fake_completed(0, "", "")

    order = DispatchOrder(order_id="o1", session_ref="localhost:f3:0.0", nudge="diagnose the failing build")
    result = dispatch_to_tmux(
        order,
        runner=runner,
        input_runner=input_runner,
        local_extra={"localhost"},
        projects_root=empty_projects,
    )

    assert result.status == DispatchStatus.SENT
    assert "pane-capture fallback" in result.reason


# --- pane_input_check ------------------------------------------------------


def test_pane_input_check_allows_an_empty_claude_code_tui_input_row() -> None:
    check = pane_input_check(
        [
            "Claude Code output",
            "  ──────────────────────",
            "    ❯    ",
            "  ──────────────────────",
            "⏵⏵ accept edits on (shift+tab to cycle)",
        ]
    )

    assert check.ok is True
    assert check.last_line == "❯"


def test_pane_input_check_blocks_a_claude_code_tui_draft() -> None:
    check = pane_input_check(
        [
            "Claude Code output",
            "──────────────────────",
            "❯ some text",
            "──────────────────────",
            "⏵⏵ accept edits on (shift+tab to cycle)",
        ]
    )

    assert check.ok is False
    assert check.reason == "blocked: unsubmitted operator draft detected"
    assert check.last_line == "❯ some text"


def test_pane_input_check_treats_a_dim_placeholder_hint_as_idle() -> None:
    """A fresh/idle Claude Code session paints its input row with a dim
    (ANSI SGR 2) placeholder hint. With an escape-aware capture, that ghost
    text must classify as idle so chitra can deliver — not block as if it were
    a real draft."""
    check = pane_input_check(
        [
            "Claude Code output",
            "──────────────────────",
            '\x1b[38;5;242m❯\x1b[39m \x1b[2mTry "how does src/foo.py work?"\x1b[22m',
            "──────────────────────",
            "⏵⏵ accept edits on (shift+tab to cycle)",
        ]
    )

    assert check.ok is True
    assert check.reason == "idle: Claude Code TUI input row shows only a dim placeholder hint"
    assert check.last_line == '❯ Try "how does src/foo.py work?"'


def test_pane_input_check_blocks_a_normal_intensity_draft_even_with_styling() -> None:
    """A real draft is normal intensity. Even when the pane carries escape
    sequences (colored prompt marker), a normal-intensity draft must still
    block — the placeholder relaxation must not weaken real-draft protection."""
    check = pane_input_check(
        [
            "Claude Code output",
            "──────────────────────",
            "\x1b[38;5;242m❯\x1b[39m fix the parser bug",
            "──────────────────────",
            "⏵⏵ accept edits on (shift+tab to cycle)",
        ]
    )

    assert check.ok is False
    assert check.reason == "blocked: unsubmitted operator draft detected"
    assert check.last_line == "❯ fix the parser bug"


def test_pane_input_check_blocks_a_partially_dim_draft() -> None:
    """A draft that is only partly dim (operator typed over a placeholder, or
    mixed styling) is still a real draft — any normal-intensity visible char
    blocks."""
    check = pane_input_check(
        [
            "Claude Code output",
            "──────────────────────",
            '\x1b[2m❯ Try "x"\x1b[22m real text',
            "──────────────────────",
            "⏵⏵ accept edits on (shift+tab to cycle)",
        ]
    )

    assert check.ok is False
    assert check.reason == "blocked: unsubmitted operator draft detected"


@pytest.mark.parametrize("prompt", ["ubuntu@host:~$ ", "(venv) user@host:~$ ", ">>> "])
def test_pane_input_check_keeps_shell_prompt_idle_detection(prompt: str) -> None:
    check = pane_input_check(["previous output", prompt])

    assert check.ok is True
    assert check.last_line == prompt.strip()


def test_pane_input_check_fails_closed_for_an_unrecognizable_pane_shape() -> None:
    check = pane_input_check(["Claude Code output", "❯ ", "status line"])

    assert check.ok is False
    assert check.reason == "blocked: unsubmitted operator draft detected"
    assert check.last_line == "status line"


def test_pane_input_check_accepts_a_configured_idle_shape() -> None:
    check = pane_input_check(["previous output", "READY"], extra_idle_regexes=[re.compile(r"READY")])
    assert check.ok is True
    assert check.reason == "idle: matched configured idle pattern"


def test_transcript_glob_is_relative_and_rejects_parent_or_absolute_patterns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHITRA_TRANSCRIPT_GLOB", "runs/**/*.jsonl")
    assert transcript_glob() == "runs/**/*.jsonl"
    monkeypatch.setenv("CHITRA_TRANSCRIPT_GLOB", "../outside/*.jsonl")
    with pytest.raises(ValueError):
        transcript_glob()


def test_find_recent_transcript_uses_the_configured_glob(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "projects"
    transcript = root / "runs" / "one" / "two" / "target.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("configured marker", encoding="utf-8")
    monkeypatch.setenv("CHITRA_TRANSCRIPT_GLOB", "runs/*/*/*.jsonl")
    assert find_recent_transcript("configured marker", projects_root=root) == transcript
    monkeypatch.setenv("CHITRA_TRANSCRIPT_GLOB", "/outside/*.jsonl")
    with pytest.raises(ValueError):
        transcript_glob()


def test_ssh_command_reads_the_configurable_host_key_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHITRA_SSH_STRICT_HOST_KEY_CHECKING", "yes")
    monkeypatch.setenv("CHITRA_SSH_CONNECT_TIMEOUT_SECONDS", "7")
    command = ssh_command("example", "true")
    assert "StrictHostKeyChecking=yes" in command
    assert "ConnectTimeout=7" in command
    monkeypatch.setenv("CHITRA_SSH_CONNECT_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError):
        ssh_command("example", "true")


# --- dispatch_to_tmux end-to-end (fake runner) ----------------------------


def test_dispatch_to_tmux_blocks_on_unsubmitted_draft() -> None:
    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["tmux", "capture-pane"]:
            return fake_completed(0, "some draft text with no prompt marker", "")
        return fake_completed(0, "", "")

    order = DispatchOrder(order_id="o1", session_ref="localhost:s:0.0", nudge="hello")
    result = dispatch_to_tmux(order, runner=runner, local_extra={"localhost"})
    assert result.status == DispatchStatus.BLOCKED


def test_dispatch_to_tmux_rejects_unsupported_session_ref() -> None:
    order = DispatchOrder(order_id="o1", session_ref="not-three-parts", nudge="hello")
    result = dispatch_to_tmux(order)
    assert result.status == DispatchStatus.FAILED
    assert "unsupported" in result.reason


def test_dispatch_to_tmux_blocks_host_not_in_allowlist() -> None:
    order = DispatchOrder(order_id="o1", session_ref="untrusted-host:s:0.0", nudge="hello")
    result = dispatch_to_tmux(order, allowed_hosts=set(), local_extra=set())
    assert result.status == DispatchStatus.BLOCKED
    assert "not in allowlist" in result.reason


# --- routing_hint: opaque pass-through only, chitra never interprets it --


def test_dispatch_to_tmux_carries_routing_hint_through_unchanged() -> None:
    """routing_hint is a caller-supplied opaque value: chitra copies it
    into DispatchResult unchanged and never reads/acts on its contents,
    exactly like the existing tag pass-through."""
    order = DispatchOrder(
        order_id="o1",
        session_ref="not-three-parts",
        nudge="hello",
        routing_hint="opus-panel",
    )
    result = dispatch_to_tmux(order)
    assert result.routing_hint == "opus-panel"


def test_dispatch_to_tmux_defaults_routing_hint_to_none() -> None:
    """Backward compatibility: an order that never sets routing_hint (the
    default) behaves exactly as before this field was added."""
    order = DispatchOrder(order_id="o1", session_ref="not-three-parts", nudge="hello")
    assert order.routing_hint is None
    result = dispatch_to_tmux(order)
    assert result.status == DispatchStatus.FAILED
    assert result.routing_hint is None


# --- directive-voice guard --------------------------------------------------


def test_directive_voice_violation_none_for_a_clean_instruction() -> None:
    assert directive_voice_violation("Stop editing main and open a PR.") is None


def test_dispatch_policy_can_replace_directive_voice_patterns() -> None:
    policy = PolicyConfig(dispatch=DispatchPolicy(banned_attribution_patterns=[r"forbidden"], extra_idle_input_regexes=[]))
    order = DispatchOrder(order_id="o1", session_ref="localhost:s:0.0", nudge="The operator asked for this")
    result = dispatch_to_tmux(order, policy=policy, allowed_hosts=set(), local_extra=set())
    assert result.status == DispatchStatus.BLOCKED
    assert not result.reason.startswith("directive-voice:")


def test_unconfigured_policy_path_matches_explicit_shipped_policy() -> None:
    order = DispatchOrder(order_id="o1", session_ref="untrusted:s:0.0", nudge="A normal relay instruction")
    no_config = dispatch_to_tmux(order, allowed_hosts=set(), local_extra=set())
    shipped_policy = dispatch_to_tmux(order, policy=PolicyConfig(), allowed_hosts=set(), local_extra=set())
    assert no_config.model_dump(exclude={"at"}) == shipped_policy.model_dump(exclude={"at"})


def test_dispatch_to_tmux_sends_a_clean_order(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    session_dir = projects_root / "some-project"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "abc123.jsonl"
    transcript.write_text(json.dumps({"text": "Stop editing main and open a PR."}) + "\n", encoding="utf-8")

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["tmux", "capture-pane"]:
            return fake_completed(0, "ubuntu@host:~$ ", "")
        return fake_completed(0, "", "")

    def input_runner(cmd: list[str], payload: str, *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        return fake_completed(0, "", "")

    order = DispatchOrder(order_id="o1", session_ref="localhost:f3:0.0", nudge="Stop editing main and open a PR.")
    result = dispatch_to_tmux(
        order,
        runner=runner,
        input_runner=input_runner,
        local_extra={"localhost"},
        projects_root=projects_root,
        sleep=lambda _seconds: None,
    )
    assert result.status == DispatchStatus.SENT


def test_dispatch_to_tmux_waits_through_an_observed_slow_transcript_flush(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    session_dir = projects_root / "some-project"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "abc123.jsonl"
    waits: list[float] = []

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["tmux", "capture-pane"]:
            return fake_completed(0, "ubuntu@host:~$ ", "")
        return fake_completed(0, "", "")

    def input_runner(cmd: list[str], payload: str, *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        return fake_completed(0, "", "")

    def complete_delayed_flush(seconds: float) -> None:
        waits.append(seconds)
        transcript.write_text(json.dumps({"text": "Resume the F9 objective."}) + "\n", encoding="utf-8")

    order = DispatchOrder(order_id="o1", session_ref="localhost:f9:0.0", nudge="Resume the F9 objective.")
    result = dispatch_to_tmux(
        order,
        runner=runner,
        input_runner=input_runner,
        local_extra={"localhost"},
        projects_root=projects_root,
        sleep=complete_delayed_flush,
    )

    assert waits == [DISPATCH_VERIFY_WAIT_SECONDS]
    assert DISPATCH_VERIFY_WAIT_SECONDS == 15.0
    assert result.status == DispatchStatus.SENT
    assert result.transcript_path == str(transcript)


def test_dispatch_to_tmux_delivers_to_a_fresh_session_showing_a_dim_placeholder(tmp_path: Path) -> None:
    """Regression for the fresh-session delivery bug: a never-used Claude Code
    session renders its input row as a dim placeholder hint. Chitra must reach
    SENT, not block it as an unsubmitted draft."""
    projects_root = tmp_path / "projects"
    session_dir = projects_root / "some-project"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "abc123.jsonl"
    transcript.write_text(json.dumps({"text": "Kick off the build."}) + "\n", encoding="utf-8")

    placeholder_pane = (
        "Claude Code output\n"
        "──────────────────────\n"
        '\x1b[38;5;242m❯\x1b[39m \x1b[2mTry "how does src/foo.py work?"\x1b[22m\n'
        "──────────────────────\n"
        "⏵⏵ accept edits on (shift+tab to cycle)\n"
    )

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["tmux", "capture-pane"]:
            return fake_completed(0, placeholder_pane, "")
        return fake_completed(0, "", "")

    def input_runner(cmd: list[str], payload: str, *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        return fake_completed(0, "", "")

    order = DispatchOrder(order_id="o1", session_ref="localhost:f3:0.0", nudge="Kick off the build.")
    result = dispatch_to_tmux(
        order,
        runner=runner,
        input_runner=input_runner,
        local_extra={"localhost"},
        projects_root=projects_root,
        sleep=lambda _seconds: None,
    )
    assert result.status == DispatchStatus.SENT


def test_dispatch_to_tmux_sends_a_clean_order_to_a_remote_host() -> None:
    """End-to-end: chitra's real deployment dispatches FROM one host (e.g.
    trailhead) and delivers over ssh into another (e.g. otherhost). Every step
    -- copy-mode check, paste, and transcript verification -- must run
    against the remote host, never the local one, for a remote target to
    ever legitimately reach SENT."""

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        assert cmd[0] == "ssh", f"remote target must never shell out locally: {cmd}"
        assert cmd[-2] == "otherhost"
        remote_cmd = cmd[-1]
        if "capture-pane" in remote_cmd:
            return fake_completed(0, "ubuntu@otherhost:~$ ", "")
        if "display-message" in remote_cmd:
            return fake_completed(0, "0\n", "")
        if "paste-buffer" in remote_cmd:
            return fake_completed(0, "", "")
        if "find " in remote_cmd:
            return fake_completed(0, "1720000000 /remote/projects/foo/abc.jsonl\n", "")
        if "tail -c" in remote_cmd:
            return fake_completed(0, "Stop editing main and open a PR.", "")
        return fake_completed(0, "", "")

    order = DispatchOrder(order_id="o1", session_ref="otherhost:f3:0.0", nudge="Stop editing main and open a PR.")
    result = dispatch_to_tmux(
        order,
        runner=runner,
        local_extra={"localhost"},
        allowed_hosts={"otherhost"},
        sleep=lambda _seconds: None,
    )
    assert result.status == DispatchStatus.SENT
    assert result.transcript_path == "/remote/projects/foo/abc.jsonl"


@pytest.mark.parametrize(
    "nudge",
    [
        "the operator wants X",
        "operator is frustrated",
        "chitra relays: do X",
    ],
)
def test_dispatch_to_tmux_blocks_directive_voice_violations(nudge: str) -> None:
    calls: list[list[str]] = []

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return fake_completed(0, "", "")

    def input_runner(cmd: list[str], payload: str, *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return fake_completed(0, "", "")

    order = DispatchOrder(order_id="o1", session_ref="localhost:f3:0.0", nudge=nudge)
    result = dispatch_to_tmux(order, runner=runner, input_runner=input_runner, local_extra={"localhost"})

    assert result.status == DispatchStatus.BLOCKED
    assert result.reason.startswith("directive-voice:")
    # Nothing pasted: no tmux/paste/capture commands issued at all, and no
    # command was ever routed through the stdin-payload (load-buffer) runner.
    assert calls == []


# --- optional real-tmux integration test (skipped if tmux is unavailable) -


@pytest.mark.skipif(not HAS_TMUX, reason="tmux binary not available in this sandbox")
def test_real_tmux_paste_and_pane_in_mode_roundtrip() -> None:
    session_name = f"pytest-chitra-{uuid.uuid4().hex[:8]}"
    subprocess.run(["tmux", "new-session", "-d", "-s", session_name, "-x", "80", "-y", "24"], check=True)
    try:
        pane = f"{session_name}:0.0"
        assert pane_in_mode(pane) is False
        proc = paste_nudge_to_local_tmux(pane, "echo hi-from-chitra-test")
        assert proc.returncode == 0
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session_name], check=False)
