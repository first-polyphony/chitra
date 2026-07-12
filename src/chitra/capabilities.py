"""Typed capability-manifest loading and reversible runtime authorization.

The manifest describes chitra's shipped console surfaces.  It is deliberately
not an executor: this module never shells out, contacts a remote service, or
widens one enabled capability into authority over another command.  The small
runtime overlay is an operator-recorded, atomic set of per-capability toggles.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError, field_validator, model_validator

from chitra.state_paths import state_dir

logger = structlog.get_logger(__name__)

SCHEMA = "chitra.capabilities.v1"
CapabilityKind = Literal["tool", "daemon"]
AuthorityLevel = Literal["observe", "record", "act"]
ParameterType = Literal["string", "integer", "number", "boolean"]


class CapabilityError(ValueError):
    """Base error for invalid capability-manifest or overlay state."""


class CapabilityNotFoundError(KeyError):
    """Raised when a requested capability is absent from the shipped manifest."""


class CapabilityDisabledError(PermissionError):
    """Raised when a command requires a capability that is not enabled."""


class NotToggleableError(CapabilityError):
    """Raised when an operator tries to toggle an always-on daemon."""


class CapabilityOverlayError(CapabilityError):
    """Raised when the runtime toggle document is malformed or unsafe."""


class _StrictModel(BaseModel):
    """Shared model contract: immutable instances and no undocumented fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CapabilityParameter(_StrictModel):
    """One typed argument exposed by a manifest command."""

    name: StrictStr
    type: ParameterType
    required: StrictBool
    description: StrictStr

    @field_validator("name", "description")
    @classmethod
    def validate_nonempty_text(cls, value: str) -> str:
        """Reject empty parameter names and descriptions at manifest load time."""
        if not value.strip():
            raise ValueError("must be non-empty")
        return value


class CapabilityCommand(_StrictModel):
    """A single console command, with an explicitly typed invocation surface."""

    name: StrictStr
    description: StrictStr
    argv: tuple[StrictStr, ...]
    params: tuple[CapabilityParameter, ...]
    mutates: StrictBool

    @field_validator("name", "description")
    @classmethod
    def validate_nonempty_text(cls, value: str) -> str:
        """Reject empty command names and descriptions."""
        if not value.strip():
            raise ValueError("must be non-empty")
        return value

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require a non-empty argv template with only non-empty tokens."""
        if not value or any(not item.strip() for item in value):
            raise ValueError("argv must contain at least one non-empty token")
        return value

    @model_validator(mode="after")
    def validate_template_parameters(self) -> CapabilityCommand:
        """Ensure every ``{param}`` template refers to a declared parameter."""
        names = {param.name for param in self.params}
        if len(names) != len(self.params):
            raise ValueError("command params must have unique names")
        referenced = {name for token in self.argv for name in re.findall(r"\{([^{}]+)\}", token)}
        unknown = referenced - names
        if unknown:
            raise ValueError(f"argv references undeclared params: {sorted(unknown)}")
        return self


class CapabilityAuthority(_StrictModel):
    """The narrow authority declared for one capability."""

    level: AuthorityLevel
    grants: tuple[StrictStr, ...]
    excludes: tuple[StrictStr, ...]

    @field_validator("grants", "excludes")
    @classmethod
    def validate_authority_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Keep authority declarations unambiguous and readable."""
        if any(not item.strip() for item in value):
            raise ValueError("authority entries must be non-empty strings")
        if len(set(value)) != len(value):
            raise ValueError("authority entries must not repeat")
        return value


class Capability(_StrictModel):
    """One manifest capability and the commands it is permitted to expose."""

    name: StrictStr
    kind: CapabilityKind
    purpose: StrictStr
    when_to_use: StrictStr
    authority: CapabilityAuthority
    default_enabled: StrictBool
    commands: tuple[CapabilityCommand, ...]

    @field_validator("name", "purpose", "when_to_use")
    @classmethod
    def validate_nonempty_text(cls, value: str) -> str:
        """Require meaningful user-facing capability text."""
        if not value.strip():
            raise ValueError("must be non-empty")
        return value

    @model_validator(mode="after")
    def validate_command_names(self) -> Capability:
        """Reject duplicate commands inside one capability."""
        names = [command.name for command in self.commands]
        if not names:
            raise ValueError("capability must declare at least one command")
        if len(set(names)) != len(names):
            raise ValueError("capability command names must be unique")
        return self


class CapabilityManifest(_StrictModel):
    """The complete packaged ``chitra.capabilities.v1`` document."""

    schema_version: Literal["chitra.capabilities.v1"] = Field(alias="schema")
    capabilities: tuple[Capability, ...]

    @model_validator(mode="after")
    def validate_uniqueness(self) -> CapabilityManifest:
        """Reject drift-prone duplicate capability and command names."""
        capability_names = [capability.name for capability in self.capabilities]
        if len(set(capability_names)) != len(capability_names):
            raise ValueError("capability names must be unique")
        command_names = [command.name for capability in self.capabilities for command in capability.commands]
        if len(set(command_names)) != len(command_names):
            raise ValueError("manifest command names must be unique")
        return self


class ToggleRecord(_StrictModel):
    """One reversible runtime override, stored without a wrapper document."""

    enabled: StrictBool
    actor: StrictStr
    reason: StrictStr
    toggled_at: StrictStr
    expires_at: StrictStr

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        """Require an operator-recorded explanation for every toggle."""
        if not value.strip():
            raise ValueError("reason must be non-empty")
        return value

    @field_validator("toggled_at")
    @classmethod
    def validate_toggled_at(cls, value: str) -> str:
        """Require an aware timestamp for auditability."""
        _parse_iso8601(value, field="toggled_at")
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: str) -> str:
        """Allow an empty expiry or require an aware ISO8601 deadline."""
        if value:
            _parse_iso8601(value, field="expires_at")
        return value


def _parse_iso8601(value: str, *, field: str) -> datetime:
    """Parse an ISO8601 timestamp and require an explicit timezone."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be an ISO8601 datetime with timezone")
    return parsed


def _utc_now() -> str:
    """Return one canonical timestamp for a persisted toggle record."""
    return datetime.now(UTC).isoformat()


def capabilities_path(root: Path | None = None) -> Path:
    """Return the runtime capability-toggle overlay path."""
    return (state_dir() if root is None else root) / "capabilities.json"


def _manifest_raw(path: Path | str | None = None) -> object:
    """Read YAML from a test override or the resource packaged with chitra."""
    if path is not None:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return yaml.safe_load(resources.files("chitra").joinpath("capabilities.yaml").read_text(encoding="utf-8"))


def load_manifest(path: Path | str | None = None) -> CapabilityManifest:
    """Load and strictly validate the shipped manifest or a test replacement."""
    try:
        raw = _manifest_raw(path)
        return CapabilityManifest.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError, ValueError) as exc:
        raise CapabilityError(f"invalid capability manifest: {exc}") from exc


def load_capabilities(path: Path | str | None = None) -> CapabilityManifest:
    """Compatibility-friendly name for loading the capability manifest."""
    return load_manifest(path)


def _manifest_for(
    *, manifest: CapabilityManifest | None = None, manifest_path: Path | str | None = None
) -> CapabilityManifest:
    """Resolve one explicit manifest instance without mixing test overrides."""
    if manifest is not None and manifest_path is not None:
        raise ValueError("pass either manifest or manifest_path, not both")
    return load_manifest(manifest_path) if manifest is None else manifest


def get_capability(
    name: str, *, manifest: CapabilityManifest | None = None, manifest_path: Path | str | None = None
) -> Capability:
    """Return one named capability or raise a typed absent-capability error."""
    resolved_manifest = _manifest_for(manifest=manifest, manifest_path=manifest_path)
    found = next((capability for capability in resolved_manifest.capabilities if capability.name == name), None)
    if found is None:
        raise CapabilityNotFoundError(name)
    return found


def _load_toggles(
    root: Path | None,
    *,
    manifest: CapabilityManifest,
) -> dict[str, ToggleRecord]:
    """Read the raw overlay and reject unknown or malformed records."""
    path = capabilities_path(root)
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityOverlayError(f"invalid capability overlay {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CapabilityOverlayError("capabilities.json must be an object keyed by capability name")
    known_names = {capability.name for capability in manifest.capabilities}
    toggles: dict[str, ToggleRecord] = {}
    for name, payload in raw.items():
        if not isinstance(name, str):
            raise CapabilityOverlayError("capabilities.json names must be strings")
        if name not in known_names:
            raise CapabilityNotFoundError(f"overlay names unknown capability: {name}")
        try:
            toggles[name] = ToggleRecord.model_validate(payload)
        except ValidationError as exc:
            raise CapabilityOverlayError(f"invalid capability overlay record for {name}: {exc}") from exc
    return toggles


def load_toggles(
    root: Path | None = None,
    *,
    manifest: CapabilityManifest | None = None,
    manifest_path: Path | str | None = None,
) -> dict[str, ToggleRecord]:
    """Load the strict runtime overlay without applying its expiry policy."""
    resolved_manifest = _manifest_for(manifest=manifest, manifest_path=manifest_path)
    return _load_toggles(root, manifest=resolved_manifest)


def _write_toggles(root: Path | None, toggles: dict[str, ToggleRecord]) -> None:
    """Atomically replace the overlay, following the goals-store write pattern."""
    path = capabilities_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: toggles[name].model_dump(mode="json") for name in sorted(toggles)}
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as tmp:
            tmp_name = tmp.name
            json.dump(payload, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
        os.replace(tmp_name, path)
    finally:
        if tmp_name is not None and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _now(now: datetime | None) -> datetime:
    """Return an aware current time while making test-time explicit."""
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current


def _is_active(record: ToggleRecord, *, now: datetime) -> bool:
    """Return whether a record has not reached its optional expiry deadline."""
    return not record.expires_at or _parse_iso8601(record.expires_at, field="expires_at") > now


def is_enabled(
    name: str,
    root: Path | None = None,
    *,
    manifest: CapabilityManifest | None = None,
    manifest_path: Path | str | None = None,
    now: datetime | None = None,
) -> bool:
    """Return effective enablement, reverting an expired overlay to its default."""
    resolved_manifest = _manifest_for(manifest=manifest, manifest_path=manifest_path)
    capability = get_capability(name, manifest=resolved_manifest)
    record = _load_toggles(root, manifest=resolved_manifest).get(name)
    current = _now(now)
    if record is not None and _is_active(record, now=current):
        return record.enabled
    return capability.default_enabled


def require_enabled(
    name: str,
    root: Path | None = None,
    *,
    manifest: CapabilityManifest | None = None,
    manifest_path: Path | str | None = None,
    now: datetime | None = None,
) -> None:
    """Require one capability's current effective enablement before mutation."""
    if not is_enabled(name, root, manifest=manifest, manifest_path=manifest_path, now=now):
        raise CapabilityDisabledError(f"capability is disabled: {name}")


def _require_toggleable(capability: Capability) -> None:
    """Keep daemons declarative and always outside the toggle overlay."""
    if capability.kind == "daemon":
        raise NotToggleableError(f"daemon capabilities are not toggleable: {capability.name}")


def _toggle(
    name: str,
    *,
    enabled: bool,
    reason: str,
    actor: str,
    expires_at: str,
    root: Path | None,
    manifest: CapabilityManifest | None,
    manifest_path: Path | str | None,
    now: datetime | None,
) -> ToggleRecord:
    """Persist one validated enable or disable overlay record."""
    if not reason.strip():
        raise CapabilityOverlayError("toggle reason must be non-empty")
    if expires_at:
        _parse_iso8601(expires_at, field="expires_at")
    resolved_manifest = _manifest_for(manifest=manifest, manifest_path=manifest_path)
    capability = get_capability(name, manifest=resolved_manifest)
    _require_toggleable(capability)
    current = _now(now)
    record = ToggleRecord(
        enabled=enabled,
        actor=actor,
        reason=reason,
        toggled_at=current.isoformat(),
        expires_at=expires_at,
    )
    toggles = _load_toggles(root, manifest=resolved_manifest)
    toggles[name] = record
    _write_toggles(root, toggles)
    logger.info("capability_toggled", capability=name, enabled=enabled, actor=actor, expires_at=expires_at)
    return record


def enable_capability(
    name: str,
    *,
    reason: str,
    actor: str = "",
    until: str = "",
    root: Path | None = None,
    manifest: CapabilityManifest | None = None,
    manifest_path: Path | str | None = None,
    now: datetime | None = None,
) -> ToggleRecord:
    """Enable one tool capability, optionally only until an aware deadline."""
    return _toggle(
        name,
        enabled=True,
        reason=reason,
        actor=actor,
        expires_at=until,
        root=root,
        manifest=manifest,
        manifest_path=manifest_path,
        now=now,
    )


def disable_capability(
    name: str,
    *,
    reason: str,
    actor: str = "",
    root: Path | None = None,
    manifest: CapabilityManifest | None = None,
    manifest_path: Path | str | None = None,
    now: datetime | None = None,
) -> ToggleRecord:
    """Disable one tool capability until an explicit reset or expiry."""
    return _toggle(
        name,
        enabled=False,
        reason=reason,
        actor=actor,
        expires_at="",
        root=root,
        manifest=manifest,
        manifest_path=manifest_path,
        now=now,
    )


def reset_capability(
    name: str,
    *,
    root: Path | None = None,
    manifest: CapabilityManifest | None = None,
    manifest_path: Path | str | None = None,
) -> None:
    """Remove one override and return the tool capability to its manifest default."""
    resolved_manifest = _manifest_for(manifest=manifest, manifest_path=manifest_path)
    capability = get_capability(name, manifest=resolved_manifest)
    _require_toggleable(capability)
    toggles = _load_toggles(root, manifest=resolved_manifest)
    if name not in toggles:
        return
    del toggles[name]
    _write_toggles(root, toggles)
    logger.info("capability_reset", capability=name)


def _input_schema(command: CapabilityCommand) -> dict[str, object]:
    """Render a command's parameter list as a small JSON Schema object."""
    properties: dict[str, object] = {
        param.name: {"type": param.type, "description": param.description} for param in command.params
    }
    return {
        "type": "object",
        "properties": properties,
        "required": [param.name for param in command.params if param.required],
        "additionalProperties": False,
    }


def to_mcp_tools(
    root: Path | None = None,
    *,
    manifest: CapabilityManifest | None = None,
    manifest_path: Path | str | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Map enabled tool commands to MCP-shaped tool definitions.

    This is intentionally only a data mapping.  A future MCP server can use
    it without making the current manifest loader an executor.
    """
    resolved_manifest = _manifest_for(manifest=manifest, manifest_path=manifest_path)
    tools: list[dict[str, object]] = []
    for capability in resolved_manifest.capabilities:
        if capability.kind == "daemon" or not is_enabled(capability.name, root, manifest=resolved_manifest, now=now):
            continue
        for command in capability.commands:
            tools.append(
                {
                    "name": command.name,
                    "description": f"{capability.purpose}\n\nWhen to use: {capability.when_to_use}\n\n{command.description}",
                    "inputSchema": _input_schema(command),
                    "annotations": {"readOnlyHint": not command.mutates},
                }
            )
    return tools


def _capability_output(
    capability: Capability,
    *,
    root: Path,
    manifest: CapabilityManifest,
) -> dict[str, object]:
    """Build a CLI-facing capability document with current enablement."""
    payload = capability.model_dump(mode="json")
    payload["enabled"] = is_enabled(capability.name, root, manifest=manifest)
    return payload


def _brief(capabilities: tuple[Capability, ...], *, root: Path, manifest: CapabilityManifest, include_all: bool) -> str:
    """Render deterministic, model-facing capability guidance."""
    blocks: list[str] = []
    for capability in sorted(capabilities, key=lambda item: item.name):
        enabled = is_enabled(capability.name, root, manifest=manifest)
        if not include_all and not enabled:
            continue
        lines = [
            f"{capability.name} ({capability.authority.level}; {'enabled' if enabled else 'disabled'})",
            f"purpose: {capability.purpose}",
            f"when_to_use: {capability.when_to_use}",
        ]
        for command in capability.commands:
            lines.append("argv: " + " ".join(command.argv))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the ``chitra-capabilities`` command-line interface."""
    parser = argparse.ArgumentParser(
        prog="chitra-capabilities", description="Inspect and reversibly toggle chitra's declared capability surface."
    )
    parser.add_argument("--root", type=Path, default=state_dir())
    commands = parser.add_subparsers(dest="command", required=True)

    def add_root(command: argparse.ArgumentParser) -> None:
        command.add_argument("--root", type=Path, default=argparse.SUPPRESS)

    list_command = commands.add_parser("list", help="List every capability and effective enablement.")
    add_root(list_command)
    list_command.add_argument("--json", action="store_true")

    show_command = commands.add_parser("show", help="Show one capability and effective enablement.")
    add_root(show_command)
    show_command.add_argument("name")
    show_command.add_argument("--json", action="store_true")

    enable_command = commands.add_parser("enable", help="Enable one tool capability, optionally time-boxed.")
    add_root(enable_command)
    enable_command.add_argument("name")
    enable_command.add_argument("--reason", required=True)
    enable_command.add_argument("--actor", default="")
    enable_command.add_argument("--until", default="")

    disable_command = commands.add_parser("disable", help="Disable one tool capability.")
    add_root(disable_command)
    disable_command.add_argument("name")
    disable_command.add_argument("--reason", required=True)
    disable_command.add_argument("--actor", default="")

    reset_command = commands.add_parser("reset", help="Remove one capability override.")
    add_root(reset_command)
    reset_command.add_argument("name")

    brief_command = commands.add_parser("brief", help="Render the deterministic model-facing capability digest.")
    add_root(brief_command)
    brief_command.add_argument("--all", action="store_true", help="Include disabled capabilities as explicitly disabled.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the capability CLI and return a shell-friendly status code."""
    args = build_arg_parser().parse_args(argv)
    try:
        manifest = load_manifest()
        if args.command == "list":
            payload = [_capability_output(capability, root=args.root, manifest=manifest) for capability in manifest.capabilities]
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                for capability in manifest.capabilities:
                    enabled = is_enabled(capability.name, args.root, manifest=manifest)
                    print(
                        f"{capability.name}\t{capability.kind}\t{capability.authority.level}\t"
                        f"{'enabled' if enabled else 'disabled'}"
                    )
        elif args.command == "show":
            capability = get_capability(args.name, manifest=manifest)
            capability_payload = _capability_output(capability, root=args.root, manifest=manifest)
            if args.json:
                print(json.dumps(capability_payload, indent=2, sort_keys=True))
            else:
                print(_brief((capability,), root=args.root, manifest=manifest, include_all=True))
        elif args.command == "enable":
            record = enable_capability(
                args.name, reason=args.reason, actor=args.actor, until=args.until, root=args.root, manifest=manifest
            )
            print(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))
        elif args.command == "disable":
            record = disable_capability(args.name, reason=args.reason, actor=args.actor, root=args.root, manifest=manifest)
            print(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))
        elif args.command == "reset":
            reset_capability(args.name, root=args.root, manifest=manifest)
            print(json.dumps({"name": args.name, "reset": True}, sort_keys=True))
        else:
            print(_brief(manifest.capabilities, root=args.root, manifest=manifest, include_all=args.all))
    except (
        CapabilityError,
        CapabilityNotFoundError,
        CapabilityDisabledError,
        NotToggleableError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"chitra-capabilities: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
