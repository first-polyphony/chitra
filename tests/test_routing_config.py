"""Tests for chitra.routing_config: purely mechanical task_type -> routing_hint
lookup. No LLM calls, no content judgment -- a config-driven table only."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from chitra.routing_config import (
    ROUTING_CONFIG_ENV_VAR,
    RouteEntry,
    RoutingConfig,
    load_routing_config,
    resolve_route,
    resolve_routing_hint,
)


def _write_config(path: Path, defaults: dict[str, str]) -> Path:
    path.write_text(yaml.safe_dump({"defaults": defaults}), encoding="utf-8")
    return path


def test_load_routing_config_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ROUTING_CONFIG_ENV_VAR, raising=False)
    assert load_routing_config(None) is None


def test_load_routing_config_from_explicit_path(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "routing.yaml", {"code-review": "sonnet"})
    config = load_routing_config(path)
    assert config is not None
    assert config.defaults == {"code-review": "sonnet"}


def test_load_routing_config_from_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_config(tmp_path / "routing.yaml", {"search": "haiku"})
    monkeypatch.setenv(ROUTING_CONFIG_ENV_VAR, str(path))
    config = load_routing_config(None)
    assert config is not None
    assert config.defaults == {"search": "haiku"}


def test_load_routing_config_raises_on_missing_file_when_configured(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    with pytest.raises(OSError):
        load_routing_config(missing)


def test_load_routing_config_raises_on_malformed_yaml(tmp_path: Path) -> None:
    path = tmp_path / "routing.yaml"
    path.write_text("defaults: [this is not a mapping: :", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        load_routing_config(path)


def test_resolve_routing_hint_matches(tmp_path: Path) -> None:
    config = RoutingConfig(defaults={"code-review": "sonnet"})
    assert resolve_routing_hint("code-review", config) == "sonnet"


def test_resolve_routing_hint_no_match_returns_none() -> None:
    config = RoutingConfig(defaults={"code-review": "sonnet"})
    assert resolve_routing_hint("unrelated-type", config) is None


def test_resolve_routing_hint_no_config_returns_none() -> None:
    assert resolve_routing_hint("code-review", None) is None


def test_resolve_routing_hint_no_task_type_returns_none() -> None:
    config = RoutingConfig(defaults={"code-review": "sonnet"})
    assert resolve_routing_hint(None, config) is None


# --- structured routes (active model/harness resolution) --------------------


def test_load_routing_config_parses_routes_alongside_defaults(tmp_path: Path) -> None:
    path = tmp_path / "routing.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "defaults": {"search": "haiku"},
                "routes": {
                    "design-judgment": {"model": "opus-4.8", "harness": "claude-code", "zdr": True},
                    "code-fix": {"model": "gpt-5.6-sol", "harness": "codex-cli"},
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_routing_config(path)
    assert config is not None
    assert config.defaults == {"search": "haiku"}
    assert config.routes["design-judgment"] == RouteEntry(model="opus-4.8", harness="claude-code", zdr=True)
    assert config.routes["code-fix"] == RouteEntry(model="gpt-5.6-sol", harness="codex-cli", zdr=False)


def test_resolve_route_returns_configured_selection() -> None:
    config = RoutingConfig(routes={"design-judgment": RouteEntry(model="opus-4.8", harness="claude-code", zdr=True)})
    resolved = resolve_route("design-judgment", config)
    assert resolved is not None
    assert (resolved.model, resolved.harness, resolved.zdr) == ("opus-4.8", "claude-code", True)
    assert resolved.routing_hint == "opus-4.8@claude-code+zdr"


def test_resolve_route_hint_omits_zdr_suffix_when_unset() -> None:
    config = RoutingConfig(routes={"code-fix": RouteEntry(model="gpt-5.6-sol", harness="codex-cli")})
    resolved = resolve_route("code-fix", config)
    assert resolved is not None
    assert resolved.routing_hint == "gpt-5.6-sol@codex-cli"


def test_resolve_route_no_match_returns_none() -> None:
    config = RoutingConfig(routes={"code-fix": RouteEntry(model="gpt-5.6-sol", harness="codex-cli")})
    assert resolve_route("unlisted", config) is None


def test_resolve_route_none_config_or_task_type_returns_none() -> None:
    config = RoutingConfig(routes={"code-fix": RouteEntry(model="gpt-5.6-sol", harness="codex-cli")})
    assert resolve_route("code-fix", None) is None
    assert resolve_route(None, config) is None


def test_defaults_only_config_has_empty_routes_backcompat() -> None:
    config = RoutingConfig(defaults={"code-review": "sonnet"})
    assert config.routes == {}
    assert resolve_route("code-review", config) is None
    assert resolve_routing_hint("code-review", config) == "sonnet"
