"""Tests for the deterministic Claude artifact ledger and command interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from chitra.artifacts import (
    ARTIFACT_URL_PREFIX,
    ArtifactKind,
    ArtifactNotFoundError,
    ArtifactRecord,
    ArtifactValidationError,
    get_artifact,
    list_artifacts,
    main,
    mark_reviewed,
    upsert_artifact,
)


def _record(**changes: str) -> ArtifactRecord:
    values: dict[str, str] = {
        "url": f"{ARTIFACT_URL_PREFIX}example-001",
        "title": "Operator interview notes",
        "kind": "interview",
        "source": "tophand:/var/lib/chitra/artifact.html",
    }
    values.update(changes)
    return ArtifactRecord(
        url=values["url"],
        title=values["title"],
        kind=cast(ArtifactKind, values["kind"]),
        source=values["source"],
    )


def test_record_get_round_trip_and_atomic_write(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    record = _record()

    assert (
        main(
            [
                "record",
                "--root",
                str(tmp_path),
                "--url",
                record.url,
                "--title",
                record.title,
                "--kind",
                record.kind,
                "--source",
                record.source,
            ]
        )
        == 0
    )
    capsys.readouterr()
    stored = get_artifact(tmp_path, record.url)

    assert stored is not None
    assert stored.published_at
    assert stored.updated_at
    assert stored.review_status == "unreviewed"
    assert stored.reviewed_at == ""
    assert stored.response == ""
    assert not list(tmp_path.glob("*.tmp"))
    payload = json.loads((tmp_path / "artifacts.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "chitra.artifacts.v1"
    assert main(["get", "--root", str(tmp_path), "--url", record.url]) == 0
    assert json.loads(capsys.readouterr().out)["url"] == record.url


def test_url_validation_rejects_non_claude_artifact_urls(tmp_path: Path) -> None:
    with pytest.raises(ArtifactValidationError, match="url must start"):
        upsert_artifact(tmp_path, _record(url="https://example.com/artifact/one"))


def test_upsert_resets_review_state_for_republished_content(tmp_path: Path) -> None:
    first = upsert_artifact(tmp_path, _record())
    reviewed = mark_reviewed(tmp_path, first.url, response='{"status":"accepted"}')

    republished = upsert_artifact(tmp_path, _record(title="Revised interview notes", source="tophand:/tmp/revised.html"))

    assert republished.published_at == first.published_at
    assert republished.title == "Revised interview notes"
    assert republished.source == "tophand:/tmp/revised.html"
    assert reviewed.review_status == "reviewed"
    assert republished.review_status == "unreviewed"
    assert republished.reviewed_at == ""
    assert republished.response == ""


def test_mark_reviewed_with_and_without_response(tmp_path: Path) -> None:
    first = upsert_artifact(tmp_path, _record())
    without_response = mark_reviewed(tmp_path, first.url)
    assert without_response.review_status == "reviewed"
    assert without_response.reviewed_at
    assert without_response.response == ""

    second = upsert_artifact(tmp_path, _record(url=f"{ARTIFACT_URL_PREFIX}example-002"))
    response = '{"result":"approved","notes":["ready"]}'
    with_response = mark_reviewed(tmp_path, second.url, response=response)
    assert with_response.review_status == "reviewed"
    assert with_response.response == response


def test_invalid_response_json_is_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    stored = upsert_artifact(tmp_path, _record())

    assert main(["mark-reviewed", "--root", str(tmp_path), "--url", stored.url, "--response", "not json"]) == 1
    assert "response must be valid JSON" in capsys.readouterr().err
    assert get_artifact(tmp_path, stored.url) == stored


def test_unreviewed_block_formatting_and_silent_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    first = upsert_artifact(tmp_path, _record())
    capsys.readouterr()

    assert main(["unreviewed", "--root", str(tmp_path)]) == 0
    expected = f"UNREVIEWED ARTIFACTS\n- interview: {first.title} — {first.url} (published {first.published_at}, source {first.source})\n"
    assert capsys.readouterr().out == expected

    mark_reviewed(tmp_path, first.url)
    capsys.readouterr()
    assert main(["unreviewed", "--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""


def test_list_uses_published_time_then_url_order(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    later = upsert_artifact(tmp_path, _record(url=f"{ARTIFACT_URL_PREFIX}z-last"))
    earlier = upsert_artifact(tmp_path, _record(url=f"{ARTIFACT_URL_PREFIX}a-first"))
    capsys.readouterr()

    assert [record.url for record in list_artifacts(tmp_path)] == [later.url, earlier.url]
    assert main(["list", "--root", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["url"] for line in lines] == [later.url, earlier.url]
    assert all(": " not in line for line in lines)


def test_missing_url_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = f"{ARTIFACT_URL_PREFIX}missing"

    with pytest.raises(ArtifactNotFoundError, match="artifact not found"):
        mark_reviewed(tmp_path, missing)
    assert main(["get", "--root", str(tmp_path), "--url", missing]) == 1
    assert "artifact not found" in capsys.readouterr().err
    assert main(["mark-reviewed", "--root", str(tmp_path), "--url", missing]) == 1
    assert "artifact not found" in capsys.readouterr().err
