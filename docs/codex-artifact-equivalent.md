# Does Codex have an equivalent to Claude Code Artifacts?

Research note for Asana task "Chitra - find codex equivalent for artifacts"
(gid 1215732216143585). Answers: is there something a `codex-cli`-routed
`RouteEntry` (see `src/chitra/routing_config.py`) can produce that is
comparable to a Claude Code Artifact, and if so, how should chitra's
dispatch layer know about it?

## What Claude Artifacts actually do

A Claude Code session can publish a self-contained HTML/React/Markdown
page, get back a preview URL, and redeploy the same URL in place as the
session continues. Sharing is private by default, with an explicit
publish step to org or public. No hosting infrastructure is required from
the caller — the harness does it inline, mid-session.

## The closest OpenAI equivalent: Codex Sites

OpenAI shipped **Codex Sites** in preview on 2026-06-02 (Business/Enterprise
ChatGPT workspaces). Invoked as an `@Sites` plugin, it has Codex build a
site/app, "save" a build (linked to a git commit, reviewable), then
"deploy" it to a live URL on request — a two-step publish gate that
Artifacts doesn't have. Output is Cloudflare Worker–compatible ES modules
(full apps, not just static HTML/Markdown).

Where it falls short of Artifacts, for chitra's purposes:

- **Not reachable from Codex CLI.** Per OpenAI's own docs, Sites has no
  CLI management surface — creation, save, and deploy all happen through
  the ChatGPT web or desktop app. `codex-cli` (the harness chitra actually
  dispatches to per `RouteEntry.harness`) can only edit/test a project
  locally before a human publishes it through the app. There is currently
  no documented API endpoint a third-party orchestrator can call to
  create or deploy a Site programmatically.
- **Workspace-locked.** Sharing is admins-only, whole-workspace, or named
  users/groups — always gated on ChatGPT workspace membership. No public
  link, no custom domain. OpenAI's own guidance: "not suitable for
  customer-facing applications."

## Conclusion

**Partial equivalent, not a real one for chitra's dispatch path.** Codex
Sites is a genuine, named, shipped feature that covers similar ground
(shareable, hosted, previewable output), but it sits behind the ChatGPT
web/desktop app, not the `codex-cli` harness chitra actually invokes. A
`DispatchOrder` routed to `harness: codex-cli` today has no mechanical way
to end up with an Artifact-equivalent URL — that gap is a Codex CLI/API
limitation, not something chitra can route around.

## If/when this becomes actionable

Consistent with chitra's existing determinism invariant (routing config is
mechanical, operator-populated, never chitra's judgment call — see
`routing_config.py`'s module docstring), the natural hook would be an
**optional, operator-declared field on `RouteEntry`**, e.g.:

```python
class RouteEntry(BaseModel):
    model: str
    harness: str
    zdr: bool = False
    artifact_capable: bool = False  # operator-asserted, not inferred
```

An operator running Claude-routed design/review tasks would set
`artifact_capable: true` on those routes today. There is nothing to set it
`true` for on any `codex-cli` route right now, because no CLI/API path
exists for Codex Sites as of this writing (2026-07-24). Revisit if OpenAI
ships CLI/API access to Sites.

This doc is a research stub, not a proposal to merge — no code change is
included.
