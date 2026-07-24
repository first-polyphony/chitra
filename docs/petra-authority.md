# Chitra and Petra workload-authority contract

Status: **dark launch / observe-only**. This document describes the v1
contract implemented by `chitra.ownership_provider` and `chitra.petra`. It is
not authorization to hold, stop, resume, signal, throttle, renice, inject into,
or otherwise control an existing live session.

## Authority split

- **Chitra ownership provider** is read-only. It answers whether one exact,
  caller-supplied `host:lane:instance` reference belongs to a canonical managed
  lane. It never discovers sessions, scans processes, derives tmux targets, or
  enrolls existing work.
- **Petra** is the only eventual authority that can make a live-workload
  decision. In the current release it only validates and durably records
  advisory pressure observations. It has no executor and no control adapter.
- **Watchtower** may provide pressure evidence only. It cannot select an
  action, and an absent/unowned/unknown Chitra result never grants it control
  over a workload.

## Chitra promotion and apply-back

Petra is also the sole eventual authority for any Chitra-originated path that
could turn a lesson into a code change, pull request, merge, or deployment.
The current `petra.pressure-observation.v1` contract is **not** such authority:
it records advisory workload pressure only and cannot be repurposed as a
general approval token.

Accordingly, Chitra apply-back is disabled in this release. Its scheduler is
observe-only, its dispatch and applied-result record paths fail closed, and
the merge queue must reject `chitra/applyback-*` branches. No timer, green CI
result, queue label, prior digest, or operator-free workflow is a substitute
for Petra authority.

A future Petra executor must issue a fresh, signed, single-use proof that
binds the exact lesson identity, immutable commit SHA, PR number and head SHA,
target environment, bounded operation, expiry, and deployment verification
requirements. Each executor stage must independently verify that proof at the
point of effect. That is a separate protocol and canary, not an extension of
the advisory-observation API.

## Chitra ownership proof

The provider accepts one bounded Unix-socket query with an exact host, boot,
request, and session identity. It becomes authoritative only when it can read:

1. a bounded, regular, non-group/world-writable `goals.json`;
2. a complete, digest-bound `goals.managed.json` marker for the current host
   and boot with a fresh manager heartbeat; and
3. a durable per-boot generation fence that rejects a lower generation or a
   changed snapshot at the same generation.

Malformed, stale, partial, owner-mismatched, oversized, or rollback state is
returned as non-authoritative `unknown`. The service uses a dedicated `chitra`
identity and verifies its state-file owner in production. Petra verifies the
provider's Unix peer identity before accepting a proof.

## Petra advisory record

For an owned lane, Petra accepts only `petra.pressure-observation.v1`. Its
payload binds the Watchtower observation to the original Chitra query ID,
provider instance, exact lane, lane generation, canonical ownership generation,
host, boot, expiry, and a bounded evidence digest. It contains no requested
operation field.

Petra immediately makes a fresh Chitra query and revalidates the observation
after that round trip. It then records the observation and exactly one outbox
row in a `FULL`-synchronous SQLite transaction. Duplicate event IDs are
idempotent only when their canonical observation digest matches; a conflicting
duplicate or a full ledger fails closed. The current fixed capacity is 10,000
events and is never silently pruned.

`health.json` is published atomically by the static `petra` identity. It is a
readiness signal for the separate bridge, not permission for Ring-0 to wait on
Petra or Chitra.

## Deployment boundary

The example units use dedicated static users, private state directories, a
`0077` umask for Petra ledger files, and restricted Unix-socket groups. They
are deliberately examples only: do not enable them against a legacy or inferred
session population. A future executor requires a separate reviewed protocol,
new-lane-only canary, explicit checkpoint/quiescence evidence, and an operator
approved rollout.
