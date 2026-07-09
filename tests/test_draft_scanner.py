"""Tests for chitra.draft_scanner: idle-vs-draft distinction."""

from __future__ import annotations

import subprocess

from chitra.draft_scanner import scan_targets


def fake_completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Real ``subprocess.CompletedProcess[str]`` matching the ``TmuxRunner``
    protocol's return type exactly."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_scan_targets_flags_a_real_unsubmitted_draft() -> None:
    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["tmux", "capture-pane"]:
            return fake_completed(0, "some half-typed operator message with no prompt", "")
        return fake_completed(0, "", "")

    result = scan_targets(["localhost:sess:0.0"], runner=runner, local_extra={"localhost"})
    assert len(result.findings) == 1
    assert result.findings[0].session_ref == "localhost:sess:0.0"
    assert result.errors == []


def test_scan_targets_does_not_flag_an_idle_prompt() -> None:
    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["tmux", "capture-pane"]:
            return fake_completed(0, "ubuntu@tophand:~$ ", "")
        return fake_completed(0, "", "")

    result = scan_targets(["localhost:sess:0.0"], runner=runner, local_extra={"localhost"})
    assert result.findings == []
    assert result.errors == []


def test_scan_targets_records_error_for_malformed_ref() -> None:
    result = scan_targets(["not-three-parts"])
    assert result.findings == []
    assert len(result.errors) == 1
    assert "malformed" in result.errors[0]


def test_scan_targets_records_error_for_unreachable_pane() -> None:
    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        return fake_completed(1, "", "no such session")

    result = scan_targets(["localhost:sess:0.0"], runner=runner, local_extra={"localhost"})
    assert result.findings == []
    assert len(result.errors) == 1
    assert "no pane capture" in result.errors[0]


def test_to_dict_round_trips() -> None:
    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["tmux", "capture-pane"]:
            return fake_completed(0, "a draft with no prompt marker at all", "")
        return fake_completed(0, "", "")

    result = scan_targets(["localhost:sess:0.0"], runner=runner, local_extra={"localhost"})
    d = result.to_dict()
    findings = d["findings"]
    assert isinstance(findings, list)
    assert "errors" in d
    assert findings[0]["session_ref"] == "localhost:sess:0.0"
