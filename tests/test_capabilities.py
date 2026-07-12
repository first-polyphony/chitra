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
    to_mcp_tools,
)


def test_packaged_manifest_loads_and_rejects_bad_authority(tmp_path: Path) -> None:
    manifest = load_manifest()

    assert manifest.schema_version == "chitra.capabilities.v1"
    assert {capability.name for capability in manifest.capabilities} >= {"board", "queue-management"}

    malformed = manifest.model_dump(mode="json", by_alias=True)
    malformed["capabilities"][0]["authority"]["level"] = "unbounded"
    path = tmp_path / "bad-capabilities.yaml"
    path.write_text(yaml.safe_dump(malformed), encoding="utf-8")

    with pytest.raises(CapabilityError, match="authority"):
        load_manifest(path)


def test_toggle_precedence_covers_absent_true_false_and_expired(tmp_path: Path) -> None:
    manifest = load_manifest()
    now = datetime(2026, 7, 11, 16, tzinfo=UTC)

    assert not is_enabled("queue-management", tmp_path, manifest=manifest, now=now)

    enable_capability("queue-management", reason="approved intervention", root=tmp_path, manifest=manifest, now=now)
    assert is_enabled("queue-management", tmp_path, manifest=manifest, now=now)

    disable_capability("queue-management", reason="intervention complete", root=tmp_path, manifest=manifest, now=now)
    assert not is_enabled("queue-management", tmp_path, manifest=manifest, now=now)

    enable_capability(
        "queue-management",
        reason="expired approval",
        until=(now - timedelta(seconds=1)).isoformat(),
        root=tmp_path,
        manifest=manifest,
        now=now,
    )
    assert not is_enabled("queue-management", tmp_path, manifest=manifest, now=now)

    overlay = json.loads((tmp_path / "capabilities.json").read_text(encoding="utf-8"))
    assert set(overlay["queue-management"]) == {"enabled", "actor", "reason", "toggled_at", "expires_at"}
    assert not list(tmp_path.glob("*.tmp"))


def test_daemons_cannot_be_toggled_and_require_enabled_is_a_real_gate(tmp_path: Path) -> None:
    manifest = load_manifest()

    with pytest.raises(NotToggleableError, match="daemon"):
        enable_capability("dispatchd", reason="no", root=tmp_path, manifest=manifest)
    with pytest.raises(CapabilityDisabledError, match="queue-management"):
        require_enabled("queue-management", tmp_path, manifest=manifest)

    enable_capability("queue-management", reason="operator approval", root=tmp_path, manifest=manifest)
    require_enabled("queue-management", tmp_path, manifest=manifest)


def test_to_mcp_tools_maps_params_and_excludes_daemons_and_disabled_tools(tmp_path: Path) -> None:
    manifest = CapabilityManifest.model_validate(
        {
            "schema": "chitra.capabilities.v1",
            "capabilities": [
                {
                    "name": "reader",
                    "kind": "tool",
                    "purpose": "Read a deterministic local document.",
                    "when_to_use": "Use when a caller needs one exact local value.",
                    "authority": {"level": "observe", "grants": ["read"], "excludes": ["write"]},
                    "default_enabled": True,
                    "commands": [
                        {
                            "name": "read-local",
                            "description": "Read the requested local document.",
                            "argv": ["read-local", "{path}"],
                            "params": [
                                {"name": "path", "type": "string", "required": True, "description": "Path to read."},
                                {"name": "verbose", "type": "boolean", "required": False, "description": "Verbose output."},
                            ],
                            "mutates": False,
                        }
                    ],
                },
                {
                    "name": "daemon",
                    "kind": "daemon",
                    "purpose": "Run a deterministic daemon.",
                    "when_to_use": "Use under a supervisor.",
                    "authority": {"level": "record", "grants": ["record"], "excludes": ["merge"]},
                    "default_enabled": True,
                    "commands": [
                        {
                            "name": "run-daemon",
                            "description": "Run it.",
                            "argv": ["run-daemon"],
                            "params": [],
                            "mutates": True,
                        }
                    ],
                },
                {
                    "name": "disabled-tool",
                    "kind": "tool",
                    "purpose": "Perform a narrow action.",
                    "when_to_use": "Use only with approval.",
                    "authority": {"level": "act", "grants": ["act"], "excludes": ["merge"]},
                    "default_enabled": False,
                    "commands": [
                        {
                            "name": "disabled-command",
                            "description": "Act narrowly.",
                            "argv": ["disabled-command"],
                            "params": [],
                            "mutates": True,
                        }
                    ],
                },
            ],
        }
    )

    tools = to_mcp_tools(tmp_path, manifest=manifest)

    assert [tool["name"] for tool in tools] == ["read-local"]
    tool = tools[0]
    description = tool["description"]
    assert isinstance(description, str)
    assert "Read a deterministic local document." in description
    assert "Use when a caller needs one exact local value." in description
    assert tool["inputSchema"] == {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to read."},
            "verbose": {"type": "boolean", "description": "Verbose output."},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    assert tool["annotations"] == {"readOnlyHint": True}


def test_manifest_commands_and_project_scripts_are_bidirectionally_in_sync() -> None:
    repository = Path(__file__).resolve().parents[1]
    project = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = set(project["project"]["scripts"])
    commands = {command.name for capability in load_manifest().capabilities for command in capability.commands}

    assert commands == scripts
