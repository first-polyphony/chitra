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


class RouteEntry(BaseModel):
    """A structured route for a ``task_type``: an explicit ``model`` and
    ``harness`` (and an optional ``zdr`` zero-data-retention flag) that
    ``dispatchd`` RESOLVES and records at dispatch, rather than the opaque
    single string a ``defaults`` entry carries.

    This is still config-driven substitution, not a smart router — chitra
    does not decide what a task type IS or evaluate content to classify one.
    The operator states, per task_type, which concrete model+harness that
    type should route to; chitra only looks it up and records the resolved
    selection.
    """

    model: str
    harness: str
    zdr: bool = False


class ResolvedRoute(BaseModel):
    """The concrete selection ``dispatchd`` resolved for a ``task_type`` from
    a ``routes`` entry: the chosen ``model`` + ``harness`` (+ ``zdr``) and a
    ``routing_hint`` canonical string derived from them for back-compat with
    every existing consumer of the opaque hint field."""

    task_type: str
    model: str
    harness: str
    zdr: bool
    routing_hint: str


class RoutingConfig(BaseModel):
    """Schema for a ``routing.yaml`` file.

    Two lookup shapes, both keyed by ``task_type``, both operator-populated:

    - ``defaults`` — the original flat ``task_type -> routing_hint`` map.
      Chitra fills in the opaque ``routing_hint`` string but never acts on
      it. Unchanged; existing configs keep working.
    - ``routes`` — a structured ``task_type -> {model, harness, zdr?}`` map.
      Chitra RESOLVES the model+harness at dispatch and records the resolved
      selection + provenance in the ledger (see ``resolve_route``).

    A ``routes`` entry wins over a ``defaults`` entry for the same task_type
    (the richer, acted-on selection is preferred over the opaque hint)."""

    defaults: dict[str, str] = {}
    routes: dict[str, RouteEntry] = {}


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


def resolve_route(task_type: str | None, config: RoutingConfig | None) -> ResolvedRoute | None:
    """Resolve a structured ``routes`` entry for ``task_type`` into a
    concrete ``ResolvedRoute`` (model + harness + zdr, plus a derived
    ``routing_hint`` string).

    Returns ``None`` if ``config`` is ``None``, ``task_type`` is ``None``, or
    ``task_type`` is not a key in ``config.routes`` — callers then fall back
    to the flat ``defaults`` lookup (``resolve_routing_hint``). Like that
    lookup, callers must only invoke this when the order's ``routing_hint``
    is not already set — an explicit ``routing_hint`` always wins.

    The derived ``routing_hint`` is a stable ``model@harness`` string (with a
    ``+zdr`` suffix when ``zdr`` is set), so every existing consumer of the
    opaque hint field keeps working while the resolved model/harness are also
    recorded structurally in the result and ledger.
    """
    if config is None or task_type is None:
        return None
    entry = config.routes.get(task_type)
    if entry is None:
        return None
    routing_hint = f"{entry.model}@{entry.harness}"
    if entry.zdr:
        routing_hint += "+zdr"
    return ResolvedRoute(
        task_type=task_type,
        model=entry.model,
        harness=entry.harness,
        zdr=entry.zdr,
        routing_hint=routing_hint,
    )
