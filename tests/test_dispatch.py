"""Tests for chitra.dispatch: pane_in_mode/-p fixes, transcript verification,
and LaneLock single-writer enforcement."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from chitra.dispatch import (
    DispatchOrder,
    DispatchStatus,
    LaneLock,
    LaneLockError,
    cancel_copy_mode,
    dispatch_to_tmux,
    ensure_pane_not_in_mode,
    find_recent_transcript,
    pane_in_mode,
    paste_nudge_to_local_tmux,
    remote_tmux_paste_command,
    tmux_pane_target,
    transcript_confirms_nudge,
)

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
