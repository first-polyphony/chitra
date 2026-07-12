"""Compile and query Chitra's deterministic, citation-bearing principle index."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = structlog.get_logger(__name__)

SOURCE_SCHEMA = "chitra.principles.source.v1"
INDEX_SCHEMA = "chitra.principles.index.v1"
COMPILER_VERSION = "1"
DEFAULT_SOURCE_PATH = Path(__file__).with_name("principles.source.yaml")
DEFAULT_INDEX_PATH = Path(__file__).with_name("principles.index.json")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,}")


class PrinciplesError(ValueError):
    """Raised when a principles source or compiled index breaks its contract."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PrincipleCitation(_FrozenModel):
    path: str = Field(min_length=1)
    resolved_path: str = Field(min_length=1)
    source_repository: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["binding", "scoped-binding"]

    @model_validator(mode="after")
    def validate_span(self) -> PrincipleCitation:
        if self.line_end < self.line_start:
            raise ValueError("citation line_end must be at or after line_start")
        return self


class PrincipleSource(_FrozenModel):
    principle_id: str = Field(pattern=r"^[A-Z][0-9]{2}$")
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)
    scopes: tuple[str, ...] = Field(min_length=1)
    answer_categories: tuple[str, ...] = Field(min_length=1)
    risk_classes: tuple[Literal["A0", "A1", "A2", "A3"], ...] = Field(min_length=1)
    keywords: tuple[str, ...] = Field(min_length=1)
    citation: PrincipleCitation


class PrinciplesSourceDocument(_FrozenModel):
    schema_name: Literal["chitra.principles.source.v1"] = Field(alias="schema")
    principles: tuple[PrincipleSource, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> PrinciplesSourceDocument:
        ids = [principle.principle_id for principle in self.principles]
        if len(ids) != len(set(ids)):
            raise ValueError("principle_id values must be unique")
        return self


class CompiledPrinciple(PrincipleSource):
    record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PrinciplesIndex(_FrozenModel):
    schema_name: Literal["chitra.principles.index.v1"] = Field(alias="schema")
    compiler_version: Literal["1"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    core_pack: tuple[str, ...] = Field(min_length=1)
    principles: tuple[CompiledPrinciple, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_bindings(self) -> PrinciplesIndex:
        ids = [principle.principle_id for principle in self.principles]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("compiled principles must have unique, sorted IDs")
        if set(self.core_pack) != set(ids):
            raise ValueError("core_pack must contain every compiled principle exactly once")
        expected = _corpus_id(self.model_dump(by_alias=True, exclude={"corpus_id"}))
        if self.corpus_id != expected:
            raise ValueError("principles index corpus_id does not match its canonical content")
        return self


class PrincipleMatch(_FrozenModel):
    principle_id: str
    title: str
    text: str
    score: float = Field(ge=0.0, le=1.0)
    matched_terms: tuple[str, ...]
    citation: PrincipleCitation


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _corpus_id(payload: object) -> str:
    return f"sha256:{_sha256(_canonical_bytes(payload))}"


def load_source(path: Path = DEFAULT_SOURCE_PATH) -> PrinciplesSourceDocument:
    """Load the reviewed source manifest, failing loudly on malformed YAML."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PrinciplesError(f"cannot load principles source {path}: {exc}") from exc
    try:
        return PrinciplesSourceDocument.model_validate(raw)
    except ValueError as exc:
        raise PrinciplesError(f"invalid principles source {path}: {exc}") from exc


def compile_index(source_path: Path = DEFAULT_SOURCE_PATH) -> PrinciplesIndex:
    """Compile a reviewed manifest into stable, content-addressed records."""
    source_bytes = source_path.read_bytes()
    source = load_source(source_path)
    compiled: list[dict[str, object]] = []
    for principle in sorted(source.principles, key=lambda item: item.principle_id):
        payload = principle.model_dump()
        payload["record_sha256"] = _sha256(_canonical_bytes(payload))
        compiled.append(payload)
    body: dict[str, object] = {
        "schema": INDEX_SCHEMA,
        "compiler_version": COMPILER_VERSION,
        "source_sha256": _sha256(source_bytes),
        "core_pack": [item["principle_id"] for item in compiled],
        "principles": compiled,
    }
    body["corpus_id"] = _corpus_id(body)
    return PrinciplesIndex.model_validate(body)


def write_index(source_path: Path = DEFAULT_SOURCE_PATH, output_path: Path = DEFAULT_INDEX_PATH) -> PrinciplesIndex:
    """Atomically replace the compiled index with canonical JSON."""
    index = compile_index(source_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temporary.write_text(json.dumps(index.model_dump(by_alias=True), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    logger.info("principles_index_compiled", corpus_id=index.corpus_id, output_path=str(output_path))
    return index


def load_index(path: Path = DEFAULT_INDEX_PATH, *, source_path: Path | None = DEFAULT_SOURCE_PATH) -> PrinciplesIndex:
    """Load and verify an index, including its checked-in source when available."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        index = PrinciplesIndex.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise PrinciplesError(f"invalid principles index {path}: {exc}") from exc
    if source_path is not None:
        try:
            current_source_hash = _sha256(source_path.read_bytes())
        except OSError as exc:
            raise PrinciplesError(f"cannot verify principles source {source_path}: {exc}") from exc
        if current_source_hash != index.source_sha256:
            raise PrinciplesError("principles index is stale relative to its reviewed source manifest")
    return index


def _tokens(value: str) -> set[str]:
    return set(_TOKEN_RE.findall(value.casefold()))


def lookup_principles(
    index: PrinciplesIndex,
    query: str,
    *,
    scopes: tuple[str, ...] = ("global",),
    answer_category: str | None = None,
    top_k: int = 5,
) -> tuple[PrincipleMatch, ...]:
    """Return stable lexical matches; the caller cannot skip or widen selection."""
    if top_k < 1:
        raise ValueError("top_k must be at least one")
    query_tokens = _tokens(query)
    if not query_tokens:
        return ()
    requested_scopes = set(scopes) | {"global"}
    matches: list[PrincipleMatch] = []
    for principle in index.principles:
        if requested_scopes.isdisjoint(principle.scopes):
            continue
        if answer_category is not None and "any" not in principle.answer_categories and answer_category not in principle.answer_categories:
            continue
        keyword_tokens = _tokens(" ".join(principle.keywords))
        body_tokens = _tokens(f"{principle.title} {principle.text}")
        keyword_hits = query_tokens & keyword_tokens
        body_hits = query_tokens & body_tokens
        keyword_score = len(keyword_hits) / max(1, min(3, len(keyword_tokens)))
        body_score = len(body_hits) / max(1, min(5, len(query_tokens)))
        score = min(1.0, round((0.7 * keyword_score) + (0.3 * body_score), 4))
        if score == 0:
            continue
        matches.append(
            PrincipleMatch(
                principle_id=principle.principle_id,
                title=principle.title,
                text=principle.text,
                score=score,
                matched_terms=tuple(sorted(keyword_hits | body_hits)),
                citation=principle.citation,
            )
        )
    return tuple(sorted(matches, key=lambda match: (-match.score, match.principle_id))[:top_k])


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compile Chitra's reviewed principles manifest")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_INDEX_PATH)
    parser.add_argument("--check", action="store_true", help="fail if output differs from a fresh deterministic compilation")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    compiled = compile_index(args.source)
    rendered = json.dumps(compiled.model_dump(by_alias=True), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != rendered:
            logger.error("principles_index_out_of_date", source=str(args.source), output=str(args.output))
            return 1
        logger.info("principles_index_current", corpus_id=compiled.corpus_id, output=str(args.output))
        return 0
    write_index(args.source, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
