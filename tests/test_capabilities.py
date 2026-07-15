"""Tests for the strict capability manifest and reversible runtime overlay."""

from __future__ import annotations

import json
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from chitra.capabilities import (
    CapabilityDisabledError,
    CapabilityError,
    CapabilityManifest,
    NotToggleableError,
    disable_capability,
    enable_capability,
    is_enabled,
    load_manifest,
    require_enabled,
)


def _default_off_usage_manifest() -> CapabilityManifest:
    raw = load_manifest().model_dump(mode="json", by_alias=True)
    usage = next(capability for capability in raw["capabilities"] if capability["name"] == "usage")
    usage["default_enabled"] = False
    return CapabilityManifest.model_validate(raw)


def test_packaged_manifest_loads_and_rejects_bad_authority(tmp_path: Path) -> None:
    manifest = load_manifest()

    assert manifest.schema_version == "chitra.capabilities.v1"
    assert {capability.name for capability in manifest.capabilities} >= {"usage", "capability-management"}

    malformed = manifest.model_dump(mode="json", by_alias=True)
    malformed["capabilities"][0]["authority"]["level"] = "unbounded"
    path = tmp_path / "bad-capabilities.yaml"
    path.write_text(yaml.safe_dump(malformed), encoding="utf-8")

    with pytest.raises(CapabilityError, match="authority"):
        load_manifest(path)


def test_toggle_precedence_covers_absent_true_false_and_expired(tmp_path: Path) -> None:
    manifest = _default_off_usage_manifest()
    now = datetime(2026, 7, 11, 16, tzinfo=UTC)

    assert not is_enabled("usage", tmp_path, manifest=manifest, now=now)

    enable_capability("usage", reason="approved intervention", root=tmp_path, manifest=manifest, now=now)
    assert is_enabled("usage", tmp_path, manifest=manifest, now=now)

    disable_capability("usage", reason="intervention complete", root=tmp_path, manifest=manifest, now=now)
    assert not is_enabled("usage", tmp_path, manifest=manifest, now=now)

    enable_capability(
        "usage",
        reason="expired approval",
        until=(now - timedelta(seconds=1)).isoformat(),
        root=tmp_path,
        manifest=manifest,
        now=now,
    )
    assert not is_enabled("usage", tmp_path, manifest=manifest, now=now)

    overlay = json.loads((tmp_path / "capabilities.json").read_text(encoding="utf-8"))
    assert set(overlay["usage"]) == {"enabled", "actor", "reason", "toggled_at", "expires_at"}
    assert not list(tmp_path.glob("*.tmp"))


def test_daemons_cannot_be_toggled_and_require_enabled_is_a_real_gate(tmp_path: Path) -> None:
    manifest = _default_off_usage_manifest()

    with pytest.raises(NotToggleableError, match="daemon"):
        enable_capability("dispatchd", reason="no", root=tmp_path, manifest=manifest)
    with pytest.raises(CapabilityDisabledError, match="usage"):
        require_enabled("usage", tmp_path, manifest=manifest)

    enable_capability("usage", reason="operator approval", root=tmp_path, manifest=manifest)
    require_enabled("usage", tmp_path, manifest=manifest)


def test_manifest_commands_and_project_scripts_are_bidirectionally_in_sync() -> None:
    repository = Path(__file__).resolve().parents[1]
    project = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = set(project["project"]["scripts"])
    commands = {command.name for capability in load_manifest().capabilities for command in capability.commands}

    assert commands == scripts
