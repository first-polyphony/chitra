# License decision history

> Written 2026-07-09. Prompted by an operator request to document the reasoning/timeline behind chitra's license, since it changed twice before settling.

A plain timeline of how chitra's license was decided, since it moved twice:

1. **Apache-2.0 (default).** Applied as the repo's initial license with no historical project precedent found in `first-polyphony/polyphony` at the time — flagged explicitly in the commit message and in repo docs as a default pending operator confirmation, not a considered final choice.
2. **CC BY-NC-SA 4.0 (proposed, PR #4).** A later PR (`fix/license-cc-by-nc-sa`) proposed switching to CC BY-NC-SA 4.0, on the grounds that the archived `lean-wintermute/polyphony` mirror had carried that license (© Trey Herr) since 2025-11-16 — a real historical precedent, unlike the Apache-2.0 default. That PR also flagged, without resolving, the tension between a non-commercial share-alike license and any future plan to make the repo fully public under permissive terms.
3. **MIT (final, as decided by the operator).** The operator ultimately decided on MIT. That change is being made by another in-flight PR — this note describes the timeline only and does not assert a specific commit SHA for the MIT change, since that work is still in progress on its own branch.
