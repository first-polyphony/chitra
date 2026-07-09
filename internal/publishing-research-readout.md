# chitra OSS publishing research — readout

> Written 2026-07-09. Prompted by an operator request for a readout of the OSS governance, release-process, and security-configuration research that had already been folded into this repo's artifacts (`docs/DESIGN.md`, `CONTRIBUTING.md`, `SECURITY.md`, live GitHub settings) without a standalone summary ever being produced. Copied here from its original location (`/tmp/chitra-extract-publishing-research-readout.md`) so it's captured in-repo rather than only in a scratch file.

**What this is:** The operator asked, earlier in this program, for two parallel research passes on `first-polyphony/chitra` (a newly-extracted standalone tool, currently a **private** repo) — OSS governance model, security configuration best practice, and release-process research. Those subagents ran and their conclusions were folded directly into repo artifacts (`docs/DESIGN.md`, `CONTRIBUTING.md`, `SECURITY.md`, live GitHub security settings, CI workflows) without ever producing a standalone readout for the operator. This document is that missing readout: reconstructed from what was actually decided/built in the repo (verified by reading the repo at `main` and querying live GitHub configuration via `gh api`), plus a small amount of fresh confirmatory web research on the two open questions where current common practice is relevant.

No changes were made to the chitra repo to produce this document — it is a report only, written to `/tmp/chitra-extract-publishing-research-readout.md`.

---

## 1. Governance model chosen

chitra uses **single/small-maintainer governance with lightweight process docs**, not a formal CNCF-style governance charter (maintainer roles, voting, escalation ladders). Evidence from the repo:

- `CONTRIBUTING.md` sets one substantive gate: open an issue before a nontrivial PR, explicitly framed as protecting maintainer review capacity ("merging a PR is a permanent maintenance commitment, not a one-time favor"), not ceremony. Trivial fixes (typos, obvious bugs with a test) skip that gate.
- The scope boundary — "no LLM calls, no reasoning, no decision-making; if a change would add that, it belongs in a different project" — appears identically in `README.md`, `docs/DESIGN.md`, and `CONTRIBUTING.md`. It functions as the de facto governance rule: PRs are evaluated against this scope statement before anything else, which substitutes for a maintainer committee decision on scope creep.
- `.github/ISSUE_TEMPLATE/bug_report.md` exists; there is no PR template, no `CODEOWNERS`, no `MAINTAINERS.md`, no `GOVERNANCE.md`.
- `SECURITY.md` states plainly: "small, best-effort maintained project — there is no dedicated security team and no SLA."

**Why this fits (not under- or over-built):** chitra is a small, deterministic relay/dedup daemon set — no LLM surface, single-purpose, and (per `SECURITY.md`) explicitly scoped to trusted-host deployment, not multi-tenant. A formal governance document (defined maintainer tiers, escalation paths, a technical steering committee) is standard practice for large multi-org projects (e.g., CNCF graduated projects) where decision rights need to be legible across many contributing companies. For a repo with one contributing org and a narrow scope statement doing the job of a governance charter, that machinery would be pure overhead with no one for it to coordinate. The lightweight approach — an issue-first norm plus a written scope boundary — is proportionate to the project's actual size and contributor base.

---

## 2. Release process

- **Versioning:** plain SemVer, currently `0.2.0`, deliberately kept in the `0.x` range. `docs/DESIGN.md` states the rationale directly: SemVer reserves `0.y.z` for "anything may change," which is appropriate before there's a real external consumer depending on a stable interface; `1.0.0` is reserved for when the repo is public and maintainers are willing to promise CLI/API stability. This is a correct, standard application of the SemVer spec's own stated intent for the 0.x range, not an invented policy.
- **Distribution:** git-installable only (`pip install git+https://github.com/first-polyphony/chitra.git@<tag>`), not yet on PyPI. `docs/DESIGN.md` gives the reasoning: current consumers install a pinned revision onto systemd hosts they provision themselves, so PyPI's main advantages — name-based discovery and version-range resolution for downstream packagers — don't apply yet. The build backend is `hatchling` with a standards-based `pyproject.toml` (src-layout, `src/chitra/`), which the design doc calls out as keeping a future PyPI release "a small, mechanical step rather than a rewrite" — i.e., the packaging groundwork for PyPI is already done, only the publish step itself is deferred.
- **Changelog discipline:** `CHANGELOG.md` follows Keep a Changelog format explicitly (linked in the file header) and is current — `[0.2.0] - 2026-07-09` and `[0.1.0] - 2026-07-09` are both populated with real entries, not placeholders.
- **Tagging/release mechanics:** no `.github/workflows/release.yml` or semantic-release automation exists in this repo (unlike the main polyphony monorepo, which uses semantic-release on `main` push per `CLAUDE.md`). Releases are presumably manual (tag + push), consistent with git-installable-only distribution — there's no PyPI publish step to automate yet.

---

## 3. Security configuration applied — what's actually live on GitHub right now

Checked directly via `gh api repos/first-polyphony/chitra`, `.../branches/main/protection`, `.../dependabot/alerts`, `.../code-scanning/alerts`, `.../secret-scanning/alerts` on 2026-07-09. This is live configuration state, not just what the docs describe:

| Control | Live state | Source |
|---|---|---|
| Repo visibility | **Private** | `repos/.../chitra` → `"private": true` |
| Secret scanning | **Enabled** | `security_and_analysis.secret_scanning.status = enabled` |
| Secret scanning push protection | **Enabled** | `.secret_scanning_push_protection.status = enabled` |
| Secret scanning non-provider patterns | **Enabled** | same block |
| Secret scanning validity checks | **Enabled** | same block |
| Secret scanning AI-detection | **Disabled** | same block |
| Dependabot security updates | **Enabled** | `.dependabot_security_updates.status = enabled` |
| Dependabot version updates | **Enabled** and active | `.github/dependabot.yml` present (pip + github-actions ecosystems, weekly, grouped); currently 2 open Dependabot PRs (#1 actions bump, #3 dev-dependencies bump) |
| Dependabot alerts (open vulnerabilities) | **0 open** | `repos/.../dependabot/alerts` → `[]` |
| Code security (GHAS-style code security feature) | **Enabled** | `.code_security.status = enabled` |
| CodeQL workflow | **Configured**, runs on push/PR/weekly cron (`0 6 * * 1`), Python only | `.github/workflows/codeql.yml` |
| CodeQL — actually producing results | **No** — see finding below | live run logs |
| Branch protection on `main` | **Partial** | see below |
| Required PR reviews | **None configured** | no `required_pull_request_reviews` key present in the protection response at all |
| Required status checks | `test (3.12)`, `test (3.13)` (CI job), `strict: true` | branch protection API |
| Enforce for admins | **Disabled** | `enforce_admins.enabled = false` |
| Required conversation resolution | **Disabled** | `required_conversation_resolution.enabled = false` |
| Force-push / branch deletion | Both **blocked** | `allow_force_pushes.enabled = false`, `allow_deletions.enabled = false` |
| SECURITY.md vulnerability reporting | Points to **GitHub private vulnerability reporting** (`.../security/advisories/new`), states no SLA, "best-effort," no CVE-issuance promise | `SECURITY.md` |

**Live finding not previously documented: CodeQL is configured but not actually functioning.** Every recent CodeQL run (checked via `gh run list` / `gh run view --log-failed`) completes its analysis successfully but then fails at the upload/reporting step with `Resource not accessible by integration — .../rest/actions/workflow-runs#get-a-workflow-run`, a GitHub Actions token-permissions error. Consequently `gh api repos/first-polyphony/chitra/code-scanning/alerts` returns `"no analysis found"` — there is currently zero enforced static-analysis security signal reaching GitHub's code-scanning UI, despite the workflow file existing and appearing green in the CI summary for the `test` job. This is a real, live gap: the CodeQL job needs either an `actions: read` permission added to its workflow permissions block, or the underlying GITHUB_TOKEN permission model reviewed, before it's actually providing security coverage.

**Also worth flagging: zero required PR reviewers.** Branch protection requires the CI status checks but no human/CODEOWNERS approval before merge. This is consistent with the fleet-wide "no PR-review gate — auto-merge" convention (CI + automated review → merge, no manual approval gate) rather than an oversight, but it's a deliberate choice worth naming explicitly for a repo that may go public, since public-repo audiences often expect at least one required review as a baseline (see §4 and §5(c) web-research note below).

---

## 4. CI posture — what's checked, and what would need to change before flipping private → public

**What's actually checked today** (`.github/workflows/ci.yml`, `.github/workflows/codeql.yml`):
- `ruff check .` (lint)
- `mypy src/chitra` (`strict = true` in `pyproject.toml`)
- `pytest --cov=chitra --cov-report=term-missing`, matrixed across Python 3.12 and 3.13
- CodeQL Python analysis on push/PR/weekly cron — **currently not delivering results** (see §3)

Nothing in the CI workflows assumes private-repo trust boundaries in an unsafe way (no hardcoded internal hostnames, no privileged secrets referenced) — `docs/DESIGN.md` states the extraction process specifically stripped internal deployment specifics (hostnames, internal service names) before this became its own repo. That part of the "make this safe to make public" work already happened.

**What should change before flipping private → public, in priority order:**
1. **Fix the CodeQL permission failure** (§3) — a public repo with a code-scanning badge that silently produces no alerts is worse than no badge at all; this should be fixed regardless of the public/private decision, but it's more visible and more likely to be checked by an external contributor once public.
2. **Decide on required PR review** — GitHub's own branch-protection guidance and general 2026 best-practice writeups treat "require at least one approving review before merge to the default branch" as a baseline recommendation for reducing the risk of an unreviewed push introducing a secret or vulnerable dependency (see Sources). chitra's current auto-merge-on-green-CI posture is a deliberate fleet-wide convention, not an accident, but it's worth an explicit operator decision on whether it stays that way once external contributors can open PRs, since "no required review" reads differently on a public repo than on an internal one.
3. **`enforce_admins`** is currently off, meaning the repo owner can bypass branch protection. Low-risk on a private single-maintainer repo; worth a conscious decision once public.
4. Nothing about GHAS-tier features (secret scanning, push protection, Dependabot) needs to change for the public/private flip — those are already fully enabled and, per GitHub's model, remain enabled on public repos at no cost (GHAS features are free for public repos and already configured here as if the repo were being held to public-repo security standards, which is a sound "build once, don't need to upgrade later" choice).

---

## 5. Open questions / what still needs an operator decision

**(a) The LICENSE question — open, do not re-relitigate here.** `main` currently carries Apache-2.0 (`LICENSE`, `NOTICE`, `pyproject.toml`), applied as a stated *default* because "no historical project precedent existed" (per the commit message and `docs/DESIGN.md`/`NOTICE` annotations). That premise has since been challenged: **PR #4** (`fix/license-cc-by-nc-sa`, opened 2026-07-09, currently open) argues the sibling repo `lean-wintermute/polyphony` has carried `CC BY-NC-SA 4.0` (© Trey Herr) since 2025-11-16 and proposes using that precedent verbatim for chitra. The PR explicitly flags, and does **not** resolve, the tension this creates: CC BY-NC-SA is a non-commercial, share-alike license, which is in tension with any future plan to make the repo fully public under permissive terms (a NC license restricts commercial reuse in a way Apache-2.0/MIT do not, and is unusual for developer tooling specifically because it blocks downstream commercial packaging). This is squarely an operator call — flagging that it exists and is unresolved, not adjudicating it here.

**(b) Whether/when to flip the repo from private to public.** No target date exists in any repo artifact. `README.md`'s license section explicitly hedges ("This is a default applied absent any historical project precedent — it has not yet been confirmed by the operator and may change"), which reads as evidence the repo was extracted and hardened in anticipation of a public release, not that a specific release date has been set. §4 above lists what should be fixed first (CodeQL permissions at minimum) regardless of when this happens.

**(c) `CODE_OF_CONDUCT.md`** — not currently present anywhere in the repo (checked `.github/` and repo root). Brief web research (see Sources) confirms this is commonly treated as optional for small/single-maintainer projects but as a near-universal expectation once a project is public and accepting outside contributions — GitHub's own contribution-health guidance and multiple 2026 write-ups frame it as standard infrastructure alongside `CONTRIBUTING.md` and issue templates once external participation is invited, with GitHub providing ready-made templates (e.g., Contributor Covenant) rather than expecting projects to draft one from scratch. Given chitra already has `CONTRIBUTING.md` and an issue template, adding a standard template `CODE_OF_CONDUCT.md` before or at the public flip (not necessarily now, while private) would be consistent with that norm and is a small, low-cost addition.

**(d) PyPI publishing timeline.** No PyPI project has been registered and no publish workflow exists. `docs/DESIGN.md` treats this as intentionally deferred until there's a real external consumer who needs name-based discovery or version-range dependency resolution rather than a pinned git tag — the `hatchling`/`pyproject.toml` groundwork is already in place to make this "a small, mechanical step" whenever that need arises. No evidence in the repo suggests this is scheduled; it reads as consciously not-yet rather than blocked or forgotten.

---

## Sources (brief confirmatory web research, §4 and §5(c))

- GitHub Docs, ["Adding a code of conduct to your project"](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/adding-a-code-of-conduct-to-your-project) — code of conduct treated as standard project infrastructure once a project accepts outside participation; GitHub provides templates rather than expecting from-scratch drafting.
- GitHub Docs, ["Managing a branch protection rule"](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/managing-a-branch-protection-rule) and DEV Community, ["Best Practices for Branch Protection"](https://dev.to/n3wt0n/best-practices-for-branch-protection-2pe3) — at least one required approving review before merge to the default branch is treated as a baseline recommendation, particularly to reduce risk of an unreviewed push introducing a secret or vulnerable dependency.

These are general-practice references, not institutional mandates specific to chitra or Polyphony — cited to ground the recommendations in §4/§5(c), not to assert a binding external requirement.
