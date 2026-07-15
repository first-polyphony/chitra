"""Shared deterministic language patterns used by Chitra's policy gates."""

from __future__ import annotations

import re

COMPLETION_DEFERRAL_PHRASES: tuple[str, ...] = (
    "you'll need to",
    "you will need to",
    "todo",
    "not implemented",
    "notimplemented",
    "out of scope",
    "leaving for",
    "leave for",
    "deferred",
    "deferring",
    "close follow-up",
    "close follow-ups",
    "follow-up items",
    "left as an exercise",
    "in a future pr",
    "future work",
    "conditionally healthy",
    "correctly blocked",
    "parse-only",
    "not publication-ready",
    "repaired and covered by tests",
    "CI evidence",
)

COMPLETION_CLAIM_RE = re.compile(
    r"\b(done|complete(?:d)?|finished|fixed|repaired|shipped|deployed|publication-ready|ready for (?:merge|release))\b",
    re.I,
)
COMPLETION_EVIDENCE_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.I)
COMPLETION_EVIDENCE_PATH_RE = re.compile(
    r"(?:^|\s)(?:/|\./)[^\s,;]+|\b[^\s]+\.(?:json|jsonl|log|png|jpg|jpeg|webp|txt)\b", re.I
)
COMPLETION_EVIDENCE_PR_RE = re.compile(r"\b(?:merged\s+)?pr\s*#\d+\b", re.I)
COMPLETION_EVIDENCE_LIVE_RESULT_RE = re.compile(
    r"\b(?:health|probe|curl|http|status|requests?|latency|exit)\b[^\n]*\b\d+(?:\.\d+)?\b", re.I
)
COMPLETION_EVIDENCE_FAILURE_RE = re.compile(r"\b(?:error|failed|failure|http)\b[^\n]*\b(?:[45]\d\d|\d+)\b", re.I)

# Known intentional drift: delivery briefs accept a broader, single-pattern
# evidence vocabulary than completion citations. Keep both variants distinct
# until a later behavior-changing stage resolves their semantics.
ARTIFACT_WORK_EVIDENCE_RE = re.compile(
    r"(?:\b[0-9a-f]{7,40}\b|(?:^|\s)(?:/|\./)[^\s]+|\b\d+\s+(?:passed|requests?|checks?)\b|"
    r"\b(?:status|http|probe|exit)\s*[=: ]\s*\d+\b)",
    re.I,
)
ARTIFACT_PROCESS_ONLY_RE = re.compile(
    r"\b(i (?:reviewed|worked|investigated|started|followed)|steps? (?:taken|performed))\b", re.I
)

OPERATOR_GATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("spend", re.compile(r"\b(spend|purchase|buy|billing|payment|paid plan|costs?\s+\$)\b", re.I)),
    ("credentials", re.compile(r"\b(credentials?|password|secret|api[- ]?key|oauth|login|authentication token)\b", re.I)),
    ("irreversible action", re.compile(r"\b(irreversible|delete|destroy|drop database|force[- ]push|terminate|revoke)\b", re.I)),
    ("strategy redirect", re.compile(r"\b(redirect|change (?:the )?goal|switch objectives?|expand (?:the )?scope)\b", re.I)),
)
