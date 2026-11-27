# Design Note: Per-Worker Lease Credentials

**Status:** design only, for an implementation issue to be filed. SELF-REVIEW: written by the implementer; a human review is required before building. Owner decision on 2026-11-27 (docs/queue-spec.md, OQ-8): per-worker credentials are wanted now. Nothing in this note is implemented.

## Problem

A lease is proved today by a `lease_id` (128 random bits) plus a generation. Both sit in `.shit/leases/<task-id>.json`, mode 0600. Any process running as the same operating-system user can read that file and present the id, so it can renew, submit, fail or release a lease it does not hold (TM-17). The chain already stops a worker that has no id, a stale generation, and a hand-forged lease file. It does not stop a same-user process that reads the file.

## Limits (read first)

The queue of issue 17 protects against crashes, races and cooperating workers of one operating-system user, not against a hostile process of that user or a rewrite or restore of the whole store. Multi-tenant or adversarial use needs an operating-system identity boundary and a credential service (issue #48); this note does not provide that.

Per-worker credentials do not stop a hostile process running as the same operating-system user. Memory, environment variables and mode 0600 files are all readable by that user. Without a broker running under a different OS identity (authenticated IPC with peer credentials), or a distinct OS identity per worker, this design only protects against passive disclosure of the `.shit` store (a copied file, a screenshot, a logged listing). The claims in the sections below are read in that narrower sense.

Unresolved items from the Daybreak round 1 review:

- Same-user hostile workers: a worker that can read another worker's secret can release or submit its lease.
- Standard input is already the adapter prompt transport, so it cannot also carry the secret.
- "Never printed" conflicts with the paste-in adapter showing the secret to a human once.
- A renewal can commit before the caller receives the rotated secret, losing the lease.
- A claim cannot deliver the secret exactly once if the response is lost.
- Default-off migration allows a downgrade unless the mode is persisted and proven by the chain.
- A human `reclaim` and `reopen` rely on the unauthenticated `actor == "human"` label; authenticated human-control operations are needed.
- The presented lease id is compared in constant time but is not first constrained to the canonical length and encoding.

## Goal and non-goals

Goal: a worker proves it is the worker the claim was issued to, using a secret that is not stored in any file the wrapper reads back, so reading `.shit` is not enough to act as that worker. This is protection from passive disclosure of the store, not from a hostile process of the same OS user (see Limits).

Non-goals: defending against a process that can read the claiming worker's memory or environment, against root, or against the owner's account being compromised. Authentication of humans (actor strings stay provenance labels). Network use: there is none.

## Proposed design

1. **Secret issued at claim.** `claim` returns, once, a `lease_secret` (32 bytes from the OS random source, hex encoded, separate from `lease_id`) together with the lease. The wrapper stores only `credential_sha256 = SHA-256(lease_secret)` in the lease record and in the `claimed` queue entry, so the chain proves it. The secret itself is never written to `.shit`, a log, an event, `QUEUE.md`, an error or a message.
2. **Presented on every later call.** `start`, `renew`, `release`, `submit`, `fail` and `hold` take `lease_id`, `generation` and `lease_secret`. The wrapper hashes the presented secret and compares it with the stored digest using `hmac.compare_digest`. Any mismatch is exit code 12 with nothing written, the same as today. The comparison order does not leak which part failed.
3. **Handling by the caller.** The secret travels in the worker's own memory, an environment variable or a file the worker owns with mode 0600, or standard input. It is never accepted as a command-line argument (visible in the process table) and never printed by the wrapper. A command-line front end must read it from a descriptor or the environment.
4. **Rotation.** Every new lease (re-claim after release, expiry or reopen) draws a new secret, so an old secret never works on a new generation. `renew` may also rotate: it returns a new secret and records the new digest in the `renewed` entry, which shortens the useful life of a leaked secret to one renewal interval. Rotation on renew is optional and decided at implementation time.
5. **Hash choice.** The secret has 256 bits of entropy, so a plain SHA-256 digest is not brute-forceable; a slow password hash would add cost and no security.
6. **Record changes.** `lease` and the `claimed` and `renewed` entries gain `credential_sha256` (64 hex characters). That is a new `schema_version` for `lease` and `queue-entry` only if this ships after the version 1 records are released; the queue is private and pre-release, so it can be added to version 1 now. If it ships later, follow "Schema version changes" in `docs/schemas.md`.
7. **Hostile-input tests to write first.** Wrong secret, secret of an older generation, the lease id alone, an empty secret, a secret with trailing whitespace, a secret supplied as an extra claim field (exit code 2), the secret absent from every file under `.shit` after a full run (a byte search), and absent from every exception text and log line. Each needs a revert proof.

## What it defends against on one machine

- A process that only reads `.shit` (the lease file, the chain, `QUEUE.md`, a pending record) learns the id and the digest but cannot act as the worker. A same-user process that also reads the worker's secret still can (see Limits).
- A leaked or logged lease id, for example from a screenshot or a shared task listing.
- A stale worker whose lease was reclaimed: it holds an old secret that no longer matches.

## What it does not defend against

- A same-user process that can read the worker's environment, memory or the file where the worker keeps its secret. On one machine, one account, that is the same trust boundary; the note moves the secret out of the shared store but cannot make it secret from the account itself.
- A process that can modify the wrapper code or the queue files and rechain them (already a stated residual risk).
- Replay of a captured call on the same machine within the lease lifetime, if the secret was captured in transit through a non-private channel such as a command line.
- Loss of the secret: the worker loses its lease and must wait for expiry or a human reclaim (Q-REC-05). That is intended.

## Migration from the bearer lease id

1. Ship the credential as optional behind a configuration flag, default off, with both paths tested: a lease with no `credential_sha256` behaves as today.
2. Turn it on per queue by configuration; new leases then carry a digest and every operation on them requires the secret.
3. Live leases at the switch keep their old form until they end; no lease is converted in place, because the wrapper cannot invent a secret for a worker that already holds the lease.
4. After one release, make the credential required and reject a lease without a digest as exit code 8.
5. Adapters (Claude, Codex, Copilot, paste-in) pass the secret through the channel in point 3. The paste-in adapter shows it to the human once, with a warning that it will not be shown again.

## Open points for the reviewer

- Rotate on renew, or only on re-claim?
- Should a human `reclaim` and a human `reopen` need the secret? Proposed: no, they act with actor `human` and the chain records them.
- Where does a long-running worker keep the secret between calls on one machine, so that other tools of the same user cannot read it? Answer depends on the adapter; the implementation issue should pick one default per adapter.
