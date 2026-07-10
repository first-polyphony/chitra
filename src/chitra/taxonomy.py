"""taxonomy — typed loader for chitra's shipped evasion-pattern ruleset.

The ruleset itself (``taxonomy.json``, packaged alongside this module) is a
fixed set of codes describing common AI-agent completion-evasion patterns,
distilled from operational observation of AI coding agents. This module only
loads and validates that data; it does not interpret free text against it
(``chitra.completion_gate`` does that, and only for a documented subset of
codes -- see ``docs/evasion-taxonomy.md``).

No LLM calls. Deterministic JSON loading + Pydantic validation only.
"""

from __future__ import annotations

import enum
import json
from functools import lru_cache
from importlib import resources
from pathlib import Path

from pydantic import BaseModel


class Disposition(enum.StrEnum):
    """The response class a taxonomy entry's originating ruleset assigns it.

    Carried through verbatim for documentation purposes. chitra's completion
    gate does not act differently per disposition today -- see
    ``docs/evasion-taxonomy.md`` for the honest scope note.
    """

    NUDGE = "NUDGE"
    DECISION = "DECISION"
    DEAD_STOP = "DEAD_STOP"
    ENVIRONMENTAL = "ENVIRONMENTAL"


class TaxonomyEntry(BaseModel):
    """One entry in the evasion-pattern ruleset."""

    code: str
    cue: str
    disposition: Disposition


@lru_cache(maxsize=1)
def _load_packaged_taxonomy() -> tuple[TaxonomyEntry, ...]:
    """Load and validate the shipped taxonomy, cached after first call."""
    raw = json.loads(resources.files("chitra").joinpath("taxonomy.json").read_text(encoding="utf-8"))
    return tuple(TaxonomyEntry.model_validate(item) for item in raw["entries"])


def load_taxonomy(path: str | Path | None = None) -> tuple[TaxonomyEntry, ...]:
    """Load the shipped taxonomy, or validate a configured replacement file."""
    if path is None:
        return _load_packaged_taxonomy()
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return tuple(TaxonomyEntry.model_validate(item) for item in raw["entries"])
