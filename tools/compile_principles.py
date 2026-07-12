"""Compile the reviewed Chitra principles manifest into a canonical index."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class SourcePrinciple(BaseModel):
    """One reviewed, citation-bearing principle."""

    model_config = ConfigDict(extra="forbid")

    principle_id: str = Field(pattern=r"^[A-Z][0-9]{2}$")
    title: str = Field(min_length=1)
    guidance: str = Field(min_length=1)
    scope: list[str] = Field(min_length=1)
    answer_categories: list[str] = Field(min_length=1)
    keywords: list[str] = Field(min_length=1)
    status: str = Field(pattern=r"^(binding|scoped-binding)$")
    citations: list[str] = Field(min_length=1)


class SourceManifest(BaseModel):
    """Reviewed compiler input."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: str = Field(alias="schema", pattern=r"^chitra\.principles\.source\.v1$")
    compiler_version: str
    principles: list[SourcePrinciple] = Field(min_length=1)


def compile_manifest(source: Path) -> dict[str, Any]:
    """Validate ``source`` and return deterministic, content-addressed output."""
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    manifest = SourceManifest.model_validate(raw)
    principles = sorted((item.model_dump(mode="json") for item in manifest.principles), key=lambda item: item["principle_id"])
    ids = [item["principle_id"] for item in principles]
    if len(ids) != len(set(ids)):
        raise ValueError("principle_id values must be unique")
    content = {
        "schema": "chitra.principles.lock.v1",
        "compiler_version": manifest.compiler_version,
        "reproducible": True,
        "principles": principles,
    }
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return {**content, "corpus_id": f"sha256:{hashlib.sha256(canonical).hexdigest()}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    compiled = compile_manifest(args.source)
    args.output.write_text(json.dumps(compiled, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
