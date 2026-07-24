# Security Policy

## Scope

chitra is a deterministic relay/dedup toolset intended to run as a `systemd`-supervised service on a host you control, delivering text into local or ssh-reachable `tmux` sessions and reading/writing a small set of local files (a JSON order/result queue, log files, a signing key). It assumes:

- The host it runs on is trusted (not shared with untrusted, mutually-adversarial users).
- The `tmux` sessions it targets belong to the same trust boundary as the daemon itself.
- It is not designed for, and should not be deployed in, a multi-tenant environment where the daemon's operator and the session owner are adversaries.

If your use case involves any of the above, treat that as out of scope and file an issue to discuss before deploying.

## Reporting a Vulnerability

Please report security issues via [GitHub's private vulnerability reporting](https://github.com/ReticleWorks/chitra/security/advisories/new) rather than a public issue.

This is a small, best-effort maintained project — there is no dedicated security team and no SLA. We'll acknowledge reports within a week where possible and fix what's actionable, but we can't promise a fixed response time or CVE issuance.

## Supported Versions

Only the latest released version is supported. Given the project's 0.x maturity (see `docs/DESIGN.md`), there is no backport policy yet.
