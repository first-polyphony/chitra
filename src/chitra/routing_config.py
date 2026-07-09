"""routing_config — optional, purely mechanical ``task_type -> routing_hint``
lookup for ``dispatchd``.

This is config-driven substitution, exactly like a ``.gitattributes`` or
``nginx.conf`` style mapping file — not a smart router. Chitra does not
decide what a "task type" IS or evaluate any content to classify one; the
caller (or an explicit ``task_type`` field on a ``DispatchOrder``) states
the task type, and this module's config maps that string to a preferred
``routing_hint`` string, purely by dictionary lookup. No LLM calls, no
judgment about content — this module keeps chitra's determinism invariant
intact.

Chitra ships no opinions about what task types or routing targets mean to
any given deployment: the config file is entirely operator-populated, and
chitra works fine with none present (the env var/path simply unset is a
normal no-op, not an error).
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog
import yaml
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

ROUTING_CONFIG_ENV_VAR = "CHITRA_ROUTING_CONFIG"


class RoutingConfig(BaseModel):
    """Schema for a ``routing.yaml`` file: a flat ``task_type ->
    routing_hint`` map under the ``defaults`` key."""

    defaults: dict[str, str] = {}


def load_routing_config(path: Path | None = None) -> RoutingConfig | None:
    """Load the routing config from ``path``, or from the
    ``CHITRA_ROUTING_CONFIG`` env var if ``path`` is not given.

    Returns ``None`` if neither ``path`` nor the env var is set — this is a
    normal no-op; chitra requires no routing config to function.

    If a path IS configured (explicitly or via the env var) but the file
    does not exist or fails to parse, that is a real configuration error:
    it is logged and re-raised, never silently swallowed.
    """
    resolved = path
    if resolved is None:
        env_value = os.environ.get(ROUTING_CONFIG_ENV_VAR)
        resolved = Path(env_value) if env_value else None
    if resolved is None:
        return None

    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("chitra_routing_config_unreadable", path=str(resolved), error=str(exc))
        raise

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        logger.error("chitra_routing_config_malformed", path=str(resolved), error=str(exc))
        raise

    try:
        return RoutingConfig.model_validate(data or {})
    except Exception as exc:
        logger.error("chitra_routing_config_invalid_schema", path=str(resolved), error=str(exc))
        raise


def resolve_routing_hint(task_type: str | None, config: RoutingConfig | None) -> str | None:
    """Purely mechanical lookup: ``task_type -> config.defaults[task_type]``.

    Returns ``None`` if ``config`` is ``None``, ``task_type`` is ``None``, or
    ``task_type`` is not a key in ``config.defaults``. Callers (see
    ``dispatchd.process_one_order``) must only invoke this when the order's
    ``routing_hint`` is not already set — an explicit ``routing_hint``
    always wins over this config lookup.
    """
    if config is None or task_type is None:
        return None
    return config.defaults.get(task_type)
