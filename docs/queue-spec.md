# Queue, Dependency, Lease and Parallelism Specification

**SELF-REVIEW:** This is an initial design self-review. A human peer review is still required before implementation (issues 17 and 18) or reliance on these rules.

This document specifies the dependency graph, queue states, leases, atomic claims, apply serialization, retry and hold rules, crash recovery and parallelism metrics for the `shit` wrapper. It builds on `docs/lifecycle.md`, `docs/schemas.md`, `docs/exit-codes.md` and `docs/threat-model.md`. It specifies behavior only; no implementation exists.

Rule IDs have the form `Q-<AREA>-<NN>`. Each rule is one testable statement. Hostile cases map to threat-model rows `TM-15` through `TM-24` in `docs/threat-model.md`. Words such as "must" are requirements on the implementation.

## Principles

1. Only genuine input dependencies and conflicting application steps block work. Drafts and reviews never wait on apply-only dependencies.
2. The queue is plain files under the confined `.shit` root. No network, daemon, database or clock service is needed or assumed.
3. The wrapper is the only writer of queue state. A worker, human or machine, asks the wrapper to claim, start, renew, release or finish. Workers never write queue files.
4. Any AI tool, script or human can be a worker. A worker is a label plus capability tags. Nothing in the queue names a model, vendor, account or host.
5. Unknown or unverifiable queue state fails closed with exit code 8, as `docs/threat-model.md` requires for hooks.
6. `QUEUE.md` is a generated view. It is never read as input.

## Files

| Path under `.shit` | Content | Writer |
|---|---|---|
| `deps/<task-id>.json` | One `dependency-shard` record per dependent task (below). Present only while the task has edges. | wrapper, under lock |
| `queue-anchor.json` | The entry count, chain tip and per-task generation high-water marks, advanced with every commit and checked on every load (Q-RES-02). | wrapper, under lock |
| `queue.jsonl` | Append-only JSON Lines `queue-entry` records, hash-chained like the event log. | wrapper, under lock |
| `leases/<task-id>.json` | One `lease` record. Present only while a lease is live or being reclaimed. | wrapper, under lock |
| `apply/<sha256-of-target>.json` | One `apply-slot` record. Present only while an apply step holds that target. | wrapper, under lock |
| `QUEUE.md` | Generated human-readable view. | wrapper, derived |

All files follow the common rules in `docs/schemas.md` (UTF-8, duplicate-key rejection, unknown-key rejection, 65,536-byte limit, depth 8, `Z` timestamps, slugs). The new record types below start at `schema_version` 1. They do not change any existing record, so no existing schema version changes (see open question OQ-2 for the event log).

### dependency-shard record

The graph is sharded per dependent task (owner decision on OQ-5, 2026-11-27). `schema` `dependency-shard`, `schema_version` 1, `task_id` (slug, the dependent; it must equal the file name), `needs`: array of objects with `needs_id` (slug, the prerequisite) and `type` (`draft`, `apply`, `review` or `decision`). Each edge lives in exactly one shard. A shard holds at most 200 `needs` (`MAX_NEEDS_PER_TASK`, far below the 65,536-byte record limit) and at most 2,000 tasks are queued or have shards (`MAX_TASKS`); a breach is exit code 7. A change to the graph writes every affected shard and one `graph_changed` queue entry that records the SHA-256 of each written shard (a deleted shard records 64 zeros), so the chain proves the graph. A shard file with no chain entry, a missing or edited shard, a record id that differs from its file name, an oversize shard, a leftover single-file `dependencies.json`, a cycle or a dangling edge makes every queue operation exit code 8.

### queue-entry record

Common fields as in `docs/schemas.md` (`schema` `queue-entry`, `schema_version`, `id`, `task_id`, `timestamp`, `actor`), plus `kind` (see Q-LOG-01), `state_after` (a queue state), optional `lease_id`, `generation`, `worker`, `worker_kind` (required on `claimed`), `step`, `hold_kind`, `not_before`, `attempt`, `message` (at most 500 characters, fixed or sanitized text), and a chain field computed as for the event log. Fields not allowed for a given `kind` are rejected. `claimed` and `renewed` entries also carry `expires_at` so the chain proves the lease file in full (Q-LEASE-10); this field is part of the record as built. A `graph_changed` entry carries no `state_after` but a `shards` array of `task_id` and `sha256` pairs, sorted by task id, and its `task_id` is the first of them.

### lease record

| Field | Type | Rules |
|---|---|---|
| `schema`, `schema_version` | | `lease`, `1` |
| `task_id` | slug | The leased task. |
| `step` | string | `draft`, `apply` or `review`. |
| `worker` | slug | Worker label chosen by the caller, for provenance only. |
| `worker_kind` | string | `ai`, `script` or `human`. Provenance only; it grants nothing. |
| `lease_id` | string | 32 lowercase hex digits from the operating system random source, generated by the wrapper. |
| `generation` | integer | Fencing token. Per task, starts at 1, increases by exactly 1 for every new lease on that task, never reused. |
| `claimed_at`, `expires_at` | timestamps | Set by the wrapper clock. `expires_at - claimed_at` is at most `lease_max_seconds`. |
| `renewals` | integer | At most `lease_max_renewals`. |

### apply-slot record

`schema` `apply-slot`, `schema_version` 1, `target` (repository-relative path, same rules as the evidence `path`), `task_id`, `lease_id`, `generation`, `acquired_at`.

### Configuration values

All are configuration, never hard-coded in logic. Limits are enforced by the loader.

| Key | Meaning | Allowed range | Suggested default |
|---|---|---|---|
| `lease_ttl_seconds` | Initial and renewal length of a lease. | 60 to 86,400 | 900 |
| `lease_max_seconds` | Longest total lease including renewals. | `lease_ttl_seconds` to 604,800 | 14,400 |
| `lease_max_renewals` | Renewals per lease. | 0 to 1,000 | 100 |
| `max_attempts` | Work attempts before a human decision is needed (Q-RET-03). | 1 to 20 | 3 |
| `max_dispatch_failures` | Consecutive dispatch failures before hold (Q-RET-05). | 1 to 20 | 3 |
| `dispatch_backoff_seconds` | Base delay after a dispatch failure, doubled each time, capped at 3,600. | 1 to 3,600 | 30 |

## Dependency types

A task depends on a prerequisite through a typed edge. The type says which milestone of the prerequisite the dependent needs.

| Type | The dependent needs | Gate it closes | Blocks drafts and reviews of the dependent |
|---|---|---|---|
| `draft` | The prerequisite's output, meaning a recorded report with outcome `success`. | The dependent stays `waiting`. | Yes, the dependent cannot start. This is a genuine input dependency. |
| `review` | A latest review of the prerequisite with verdict `pass`, recorded by a human actor. | The dependent stays `waiting`. | Yes. |
| `decision` | An accepted decision by a human on the prerequisite task, not superseded. | The dependent stays `waiting`. | Yes. |
| `apply` | The prerequisite's apply step finished first, because both change the same thing. | Only the dependent's `apply` step. | No. The dependent may draft and be reviewed while the prerequisite is unapplied. |

Rules:

- **Q-DEP-01.** A task is `waiting` while any edge of type `draft`, `review` or `decision` is unmet. The task enters `ready` in the same locked operation that satisfies the last unmet edge. Test: build a three-task chain, finish prerequisites in order, assert each dependent becomes `ready` only then.
- **Q-DEP-02.** An `apply` edge never holds a task in `waiting`. A task whose only unmet edges are `apply` edges is `ready` and may claim `draft` and `review` steps. Test: a task with one unmet `apply` edge is `ready`, can claim step `draft`, and is refused step `apply` (Q-SER-03).
- **Q-DEP-03.** The graph must be acyclic over all edge types combined. A self edge is a cycle. Adding an edge that closes a cycle is refused with exit code 3 and changes nothing. Test: self edge, two-node cycle, and a long-chain cycle all refused; graph bytes unchanged.
- **Q-DEP-04.** Cycle and missing-node checks also run on every load. A loaded graph containing a cycle or a dangling edge makes every queue operation return exit code 8 (fail closed) and writes nothing. Test: forge a cycle into the shards with a matching chain entry; claim returns 8.
- **Q-DEP-05.** An edge naming a task that has no task record is refused on add with exit code 9. Test: add an edge to an unknown id.
- **Q-DEP-06.** Edge endpoints must be existing task ids, validated as slugs. Duplicate edges (same `task_id`, `needs_id`, `type`) are rejected with exit code 3. Two edges with different types between the same pair are allowed. Test: duplicate rejected; mixed types accepted.
- **Q-DEP-07.** Edges are added or removed only by the `human` or `wrapper` actor. Removing an edge from a task that already has a live lease is refused with exit code 4. A `model` actor may propose an edge as a decision record but cannot change the graph. Test: model actor add refused with exit code 10.
- **Q-DEP-08.** Readiness is a pure function of the graph, the task records, the lifecycle states, the recorded reports, reviews and decisions, and the leases. It is recomputed from files at each operation, never cached across operations. Test: delete every derived file including `QUEUE.md`; readiness is unchanged.
- **Q-DEP-09.** A prerequisite that leaves its satisfied milestone (for example a review verdict superseded by a later non-pass human review, or a task reopened) re-closes the gate. A dependent that is `waiting` or `ready` and holds no lease moves back to `waiting`. A dependent already holding a lease keeps running; its next submit for review is refused with exit code 9 until the gate is satisfied again. Test: supersede a pass review; dependent returns to `waiting`. Accepted limit for this release: there is no queue-log kind for `ready` to `waiting`, so only `waiting` to `ready` is written. The stored state can lag the effective one in that direction; readiness is recomputed from files on every operation (Q-DEP-08), so a claim of such a task is refused and `status` reports `waiting`.

## Queue states

The queue state is stored in `queue.jsonl` and must agree with the task's lifecycle state. `shit check` verifies the agreement.

| Queue state | Meaning | Lifecycle state | Lease |
|---|---|---|---|
| `waiting` | At least one blocking edge is unmet. | `open` | none |
| `ready` | All blocking edges met, no lease, `not_before` not in the future. | `open` | none |
| `dispatched` | Claimed under a lease; not yet started (for example waiting for execution capacity). | `open` | live |
| `running` | Work started. | `running` | live |
| `review` | Work reported; awaiting review, approval or closure. | `review` or `approved` | none |
| `hold-owner` | Waiting on a human. | `blocked` | none |
| `hold-ci` | Waiting on an automated check named in `blocking_item`. | `blocked` | none |
| `failed` | An attempt failed; a human decides what next. | `failed` | none |
| `done` | Complete. Terminal. | `closed` | none |

- **Q-ST-01.** Every task has exactly one queue state at any time, equal to the `state_after` of its latest queue entry. Test: replay of `queue.jsonl` yields one state per task and `check` compares it to the lifecycle mapping above.
- **Q-ST-02.** `hold-owner` and `hold-ci` both require `blocked_reason` and `blocking_item` on the task, as the lifecycle requires, and a `hold_kind` on the queue entry. Test: a hold entry without those fields is rejected with exit code 3.
- **Q-ST-03.** Only transitions in the table below are legal. Every other pair, including repeating a state, is refused with exit code 4 without modifying the queue, leases or event log. Test: enumerate all 81 pairs; exactly the listed ones succeed in a suitable fixture.
- **Q-ST-04.** A queue transition that implies a lifecycle transition performs both in one locked operation using the existing pending-record commit point, with the actor rules of `docs/lifecycle.md`. If the lifecycle transition is illegal, the queue transition is illegal. Test: inject a failure between the two; recovery leaves both or neither.
- **Q-ST-05.** `done` is terminal and the task id is never reusable (Q-RES-01).

### Allowed queue transitions

| From | To | Trigger | Actor | Lifecycle effect |
|---|---|---|---|---|
| waiting | ready | Last blocking edge satisfied (Q-DEP-01). | wrapper | none |
| ready | waiting | A blocking edge is added or re-closes (Q-DEP-09). | wrapper | none |
| ready | dispatched | Atomic claim (Q-CLAIM-01). | wrapper on behalf of any worker | none |
| dispatched | ready | Release, lease expiry, or dispatch failure with backoff (Q-LEASE-05, Q-RET-04). | wrapper | none |
| dispatched | running | Worker starts the step (Q-LEASE-01). | wrapper | open to running |
| dispatched | hold-owner, hold-ci | A blocker is identified before start. | human or wrapper | open to blocked |
| dispatched | failed | Launch failed and the failure is not retryable (Q-RET-05). | wrapper | open to failed |
| running | review | Worker submits a report. | wrapper | running to review |
| running | hold-owner, hold-ci | A blocker is identified. | wrapper | running to blocked |
| running | failed | Worker reports failure, or lease expired (Q-LEASE-06). | wrapper | running to failed |
| review | ready | Human requests changes. | human | review to open |
| review | hold-owner, hold-ci | Review finds a blocker. | human or wrapper | review to blocked |
| review | failed | Review or its verification fails. | human or wrapper | review to failed |
| review | done | Human approval, then closure under the `close` rules. | human enters approved; human or wrapper closes | review to approved to closed |
| hold-owner, hold-ci | ready | Blocker resolved (Q-RET-07). | human | blocked to open |
| hold-owner, hold-ci | running | Wrapper resumes after the blocker is resolved. Accepted limit for this release: not implemented, because a running task needs a live lease and a resume has no claim. A hold leaves only through a human reopen to `ready`. | wrapper | blocked to running |
| hold-owner, hold-ci | failed | Blocker became a failure. | human or wrapper | blocked to failed |
| failed | ready | Reopen for another attempt (Q-RET-03). | human only | failed to open |

A task re-entering `ready` from `review`, a hold or `failed` is re-evaluated against Q-DEP-01 in the same operation and lands in `waiting` if an edge is unmet.

## Leases and atomic claims

A worker claims a step of a task for a bounded time. The claim creates a lease. The lease is the only thing that lets a worker move the task forward.

- **Q-CLAIM-01.** A claim is performed under the store's exclusive lock (the same advisory lock the task commands use). Inside the lock the wrapper re-reads the graph, task, queue and leases, verifies the task is `ready` for the requested step, and only then writes. A claim made on stale information finds the task not `ready` and is refused with exit code 4. Test: two processes claim the same task simultaneously over 200 trials; exactly one succeeds each time.
- **Q-CLAIM-02.** The lease file is created with exclusive create semantics (`O_CREAT|O_EXCL`, no symlink follow, relative to the confined directory descriptor). If the lock were bypassed, a second creator still fails. Test: pre-create the lease file; the claim is refused and the queue is unchanged.
- **Q-CLAIM-03.** A claim writes, in order and as one recoverable operation: the pending commit record, the lease file, the queue entry (`claimed`), the chain entry, the queue anchor (Q-RES-02), and then removes the pending record. Each apply step is idempotent (Q-REC-01). Test: kill after each step; recovery finishes or rolls forward to a single consistent state.
- **Q-CLAIM-04.** One task has at most one live lease. One worker label may hold several leases, up to the configured capacity for its resource class; the wrapper counts leases from files, not from worker statements. Test: claiming past capacity leaves the task `ready`, and the pass records the capacity shortfall (issue 18 records such a claim as `dispatched`, never falsely `ready`).
- **Q-CLAIM-05.** The wrapper generates `lease_id` and `generation`. A claim request supplies only task id, step and worker label and kind. A request that supplies a lease id, generation, time or state is rejected with exit code 2. Test: request with an extra field is refused.
- **Q-CLAIM-06.** A claim of a step needs the worker's capability tags to include every tag the task requires. Tags are strings from configuration. A worker with no tags can claim a task that requires none. Test: manual worker claims an untagged task; refused for a tagged one.
- **Q-LEASE-01.** Every later operation by the worker (`start`, `renew`, `release`, `submit`, `fail`, `hold`) must present `lease_id` and `generation`. The wrapper compares both against the lease file with a constant-time comparison. A mismatch, a missing lease or an expired lease is refused with exit code 12 `lease_refused` (approved, OQ-1) and nothing is written. Test: wrong id, wrong generation, expired lease and no lease each refused.
- **Q-LEASE-02.** `generation` increases by one on every new lease for the task and is also recorded on every queue entry. An operation presenting a generation lower than the task's latest is refused (stale worker). Test: worker A's lease expires, worker B claims (generation 2), worker A submits with generation 1; refused and B's lease intact.
- **Q-LEASE-03.** Leases expire at `expires_at` by the wrapper's clock. The initial length is `lease_ttl_seconds`. `renew` adds `lease_ttl_seconds`, bounded so total lease length never exceeds `lease_max_seconds` and renewals never exceed `lease_max_renewals`. A renewal past a bound is refused with exit code 12. Test: renew until each bound.
- **Q-LEASE-04.** Workers never supply times. All timestamps come from the wrapper clock. Test: a request carrying a time field is rejected (Q-CLAIM-05).
- **Q-LEASE-05.** Expiry is applied lazily, under the lock, at the start of every queue operation (not by a background process). An expired lease on a `dispatched` task is removed and the task returns to `ready` with an `expired` entry. Test: advance a fake clock; the next operation reclaims.
- **Q-LEASE-06.** An expired lease on a `running` task is removed, an `expired` entry is written, and the task moves to `failed` with actor `wrapper` and reason `lease expired`, because the lifecycle forbids `running` to `open`. A human reopens it (Q-RET-03). Test: expiry of a running lease ends in `failed`, never `ready`.
- **Q-LEASE-07.** A lease expiry also releases every apply slot held under that lease (Q-SER-05). Test: expire a running apply step; the slot file is gone.
- **Q-LEASE-08.** Clock safety: the wrapper keeps the newest timestamp it has recorded in `queue.jsonl`. If the current clock reads earlier than that timestamp, the wrapper does no expiry and no claim, and returns exit code 8 until the clock is no earlier. Test: set the fake clock back by one second; claim and expiry both refused with exit code 8.
- **Q-LEASE-09.** Early expiry caused by a forward clock jump is safe because fencing (Q-LEASE-02) and apply slots (Q-SER-04) hold regardless of time. It may cause duplicate work but never a double apply. Test: jump the clock forward past the TTL while the worker is alive; the worker's next operation is refused, and a second claim does not produce two applies.
- **Q-LEASE-10.** A lease file is trusted only if it matches the latest `claimed` or `renewed` queue entry for the task (same `lease_id` and `generation`). A lease file with no matching entry is an orphan and is handled by recovery (Q-REC-03). Test: hand-written lease file does not grant any operation.
- **Q-LEASE-11.** Lease files and queue files are opened under the confinement rules of `docs/threat-model.md` (realpath containment, no symlink, no extra hard link, regular files only, restrictive mode). Violations return exit code 5.

Honest limit: `lease_id` is a bearer token stored in a file. It stops a worker that does not hold it, a stale worker, and a hand-forged file. It does not stop another process of the same operating-system user that can read the file. See TM-17 residual risk.

## Serialization of apply-only steps

An apply step is the step that changes shared state outside `.shit` (for example a file in the repository that other tasks also change). A task declares the repository-relative `targets` its apply step will change in its brief scope. Draft and review steps never take apply slots.

- **Q-SER-01.** Two apply steps whose target sets overlap never hold slots at the same time. Apply steps with disjoint targets may run in parallel. Test: three tasks, two sharing a target; the sharing pair never overlaps in `apply_started`/`apply_finished` intervals while the third overlaps freely.
- **Q-SER-02.** A task starting its `apply` step acquires a slot for every target, all or nothing, in one locked operation, in sorted target order. If any slot is held, none is taken and the step is refused with exit code 4 and stays queued. Test: a task needing targets X and Y while X is held takes no slot on Y.
- **Q-SER-03.** An `apply` edge from D to P keeps D's `apply` step refused until P's `apply_finished` entry exists. It does not affect D's draft or review steps (Q-DEP-02). Test: see the worked example.
- **Q-SER-04.** Without an `apply` edge, overlapping apply steps are admitted in order of `(task created_at, task id)` among tasks whose apply step is requested. There is no priority field (OQ-9, decided). The slot file carries the holder's `lease_id` and `generation`; releasing or writing under a slot with a mismatching pair is refused with exit code 12. Test: tasks apply in creation order.
- **Q-SER-05.** A slot is released by `apply_finished`, by `release`, by lease expiry or by a hold or failure. A held or failed task keeps no slot. Test: each path removes the slot.
- **Q-SER-06.** Before applying, the worker's step must present the sha256 of each target as it read it while drafting (`base_sha256`). If the target's current digest differs at apply start, the apply step is refused with exit code 9 and the task moves to `hold-owner` with a fixed reason, because the draft was made against a changed base. Test: change a target after drafting; apply is refused and the target is untouched.
- **Q-SER-07.** Target paths follow the evidence path rules: repository-relative, no empty, `.` or `..` component, never under `.shit`, reached without a symlink. A target outside those rules is rejected with exit code 5. Test: `../x`, absolute path and `.shit/tasks/a.json` all refused.

### Worked example

Tasks `fix-timeout` (A) and `rename-timeout-key` (B) both change `config/app.toml`. The graph holds one edge: B needs A with type `apply`. A has no edges.

| Step | Event | A | B |
|---|---|---|---|
| 1 | Graph loaded. B has only an `apply` edge. | ready | ready (Q-DEP-02) |
| 2 | Worker 1 claims A step `draft`; worker 2 (a human working by hand) claims B step `draft`. Both run in parallel. | running | running |
| 3 | Worker 3 asks to claim B step `review` while worker 2's lease on B is live. Refused with exit code 4: one live lease per task (Q-CLAIM-04), whatever the step. B's draft is reviewed after worker 2 submits it (B is in `review`), and that review does not wait on A's apply, because reviews never wait on apply-only edges. | running | running, claim refused |
| 4 | Worker 2 asks to start B step `apply`. A's `apply_finished` does not exist. | running | refused (Q-SER-03), exit code 4, stays queued |
| 5 | A starts `apply`: slot for `config/app.toml` acquired (Q-SER-02). | applying | queued |
| 6 | A records `apply_finished`; slot released. A submits for review. | review | now eligible |
| 7 | B starts `apply`. Slot acquired. Worker presents `base_sha256`; A's change altered the file, so the digest differs (Q-SER-06). | review | refused, exit code 9, `hold-owner` |
| 8 | A human reconciles B's draft against A's change and reopens B. B drafts again, applies with the new base. | review | ready, then applies |

If the file was not actually affected (for example the edit touched a different part and the owner confirms), the human records a decision and reopens; the spec does not auto-merge. Without the edge, step 4 would be refused only if both ran apply at the same moment; the edge adds the ordering guarantee.

## Retry and hold rules

- **Q-RET-01.** An attempt is one transition into `running`. The attempt number is carried on queue entries and equals the count of `started` entries for the task. Test: three starts yield attempt 3.
- **Q-RET-02.** Retry is never automatic. After a worker or lease failure the task is `failed` and stays so until a human acts. The wrapper never performs `failed` to `ready`. Test: no code path moves `failed` to `ready` with actor `wrapper` or `model` (exit code 4).
- **Q-RET-03.** Reopening a task whose attempt count is at least `max_attempts` requires an accepted human decision on the task made after the last failure; otherwise exit code 10. Test: fourth reopen without a new decision refused.
- **Q-RET-04.** A dispatch failure (the claim succeeded but the step could not be started, for example a tool is unavailable, exit code 11) is recorded as `dispatch_failed` with its fixed reason. The lease is removed, the task returns to `ready` with `not_before = now + dispatch_backoff_seconds * 2^(n-1)`, capped at 3,600 seconds, where `n` is the number of consecutive dispatch failures. A dispatch failure is not a work attempt. Test: a failed launch records the reason and backoff.
- **Q-RET-05.** After `max_dispatch_failures` consecutive dispatch failures the task moves to `hold-owner` with a fixed reason. A successful `started` resets the count. Test: third failure holds.
- **Q-RET-06.** A task whose `not_before` lies in the future is not claimable and is counted as `deferred`, not `ready-not-running` (Q-MET-03). Test: claim before `not_before` refused with exit code 4.
- **Q-RET-07.** A hold releases the lease and all apply slots. A hold never expires on its own and never unblocks dependents. `hold-ci` leaves only when a check result for the named `blocking_item` is recorded by the wrapper as passing, or a human resolves it. `hold-owner` leaves only by a human. A hold changes kind only by going through `ready` (the lifecycle forbids `blocked` to `blocked`). Test: a dependent of a held task stays `waiting`.

## Crash recovery

- **Q-REC-01.** Every queue mutation uses the commit-point pattern of `shit/task.py`: validate, write `pending.json` (the commit point), apply lease, queue entry, chain entry and any lifecycle record idempotently, remove `pending.json`. Test: inject a crash after each stage and run any locked operation; the end state equals an uninterrupted run.
- **Q-REC-02.** The first thing every locked queue operation does is recover, through one planner and one write path for both subsystems. The planner only reads. It covers a pending task commit (`pending.json`), a pending queue commit (`queue-pending.json`) with the task commit nested in it, and, when no queue commit is pending, the whole queue state (event log, chain, replay, anchor, graph, lifecycle agreement) checked against the task image a pending task commit would write. For a pending queue commit it parses the record from its own bytes (a size-limit failure counts as a rejection, exit code 8), applies a torn tail virtually, and proves the anchor (count and tip at the pre-commit prefix or at this exact commit, and per-task generation marks equal to the replayed state at that point), the chain tail, every lease, lifecycle and shard effect against the replay of the verified prefix plus the entry, the lifecycle step against the task file and the verified event log (time order, legal transition), and that the lease path is free or holds this commit's lease. A lease to remove must be absent or exactly the lease the replay says is live. The nested task record is proven completely: the event prefix and chain, the candidate as the legal next event with the expected id, and the pending task image as exactly what that event does to the task file. Writes start only after the planner passed, in this order: the task commit, the queue files' torn-tail rewrites, the store directories and the removal of a leftover publication temp name, then the queue commit (lease, shards, queue and chain appends, anchor, removal of the pending record). A refusal in the planner returns exit code 8, writes nothing and keeps every pending record; a pending record is never deleted automatically. Every locked entry point of the task, evidence, report, review, decision and queue commands (`get_task`, `create_task`, `transition`, `show`, the four `add_*` commands, and so on) runs this same planner first, and writes nothing before it passed; a task entry point does so only when a pending record exists, so an unrelated queue fault does not block ordinary reads. A provable pending record is therefore recovered by whichever command comes first, and an entry point refuses (exit code 8) only when the plan cannot be proven. A test enumerates every locked block in the package and fails if one writes before calling the planner. Trade-off: recovery on entry means any command can complete a pending commit, in return for a stray provable record never blocking writers; a record that cannot be proven still blocks every writer until an operator acts. There is no automatic or API way to discard a pending record. The operator procedure for one that cannot be recovered: stop all writers; keep a copy of `queue-pending.json` (and `pending.json` if present) outside `.shit` as evidence; read the record and the files it names to see which steps of the interrupted operation had already been applied before the crash; remove or repair the cause the refusal points to, and only if the record itself is invalid or superseded move it out of `.shit`; then run any queue operation, which verifies the whole state again. What is lost by discarding a record: the operation it described is never applied. Effects the crashed process had already written before its crash (a lease file, a lifecycle step) are not undone; a lease file with no entry is removed as an orphan, and a lifecycle step with no queue entry makes the queue refuse (exit code 8) until a human reconciles the task. A hostile or buggy process of the same OS user can plant such a record at will, so it can block every writer; an operator is the accepted recovery control, and only authentication or OS separation would prevent the planting. Orphan lease maintenance commits its `orphan_removed` entry first and deletes the file afterwards, so a refused commit leaves the file; a crash between the two does not append a second entry (an entry that immediately precedes for the same task and state is not repeated), so the log grows by at most one entry per orphan file. Not covered: a process that ignores the advisory lock can swap a file between a check and the write that follows it; a crash or OS error during the write phase is finished by the next operation; a coordinated rewrite of every store file is the unsigned-history residual; every queue operation verifies the event log more than once (planner, then the operation context), which is not one snapshot. Tests: `PendingProof`, `RecoveryWrites` (a recording of every write against the planner's proof point).
- **Q-REC-03.** After pending recovery the wrapper reconciles: (a) a lease file with no matching `claimed` or `renewed` entry is deleted and an `orphan_removed` entry is written; (b) a `claimed` entry whose lease file is missing is treated as expired; (c) a lease or apply slot belonging to a task in `waiting`, `ready`, `review`, a hold, `failed` or `done` is deleted; (d) an apply slot whose `lease_id` does not match a live lease is deleted. Test: construct each inconsistency; reconciliation fixes exactly it. Accepted limit: an `orphan_removed` entry is written only for a task that is in the queue; for any other task the file is removed and nothing is written, because an entry needs a queue state to carry.
- **Q-REC-04.** A broken queue chain, an unparseable queue entry or a queue entry that contradicts the lifecycle mapping makes every queue operation return exit code 8. The wrapper never repairs history. Test: truncate a middle line; all operations return 8.
- **Q-REC-05.** After a crash of a worker (not of the wrapper), nothing happens until the lease expires (Q-LEASE-05, Q-LEASE-06). A worker may release explicitly; a human may force expiry of a lease by recording the reclaim, which is treated as expiry. Test: forced reclaim writes the same entries as natural expiry.
- **Q-REC-06.** `QUEUE.md` is regenerated from files after every successful mutation and by an explicit command. Deleting it changes no state and is not an error. Editing it changes no state. Test: delete and edit it; every query is unchanged.
- **Q-RES-01.** A task id that has ever appeared in `queue.jsonl`, the event log or a task file is never accepted for a new task. A `done` task cannot transition. A new task needs a new id. Test: create a task with a closed task's id; refused with exit code 3.
- **Q-RES-02.** Restoring an older copy of `.shit` over a newer one is detected by `queue-anchor.json`, an in-store consistency check: it records the entry count, the chain tip and each task's generation high-water mark, is advanced with every commit, and is compared with the queue files on every load. An older queue prefix, a missing anchor over a non-empty queue, or an anchor that disagrees with the queue is exit code 8. The anchor is not an external or monotonic reference: queue facts are not recorded in the event log, and anyone who restores the queue files, the chain, the shards and the anchor together (or deletes all of them) cannot be detected without signing, which the threat model lists under residual risks. Test: `TipAnchor`.

## Event kinds in the queue log

- **Q-LOG-01.** `kind` is one of `enqueued`, `ready`, `claimed`, `started`, `renewed`, `released`, `expired`, `dispatch_failed`, `apply_started`, `apply_finished`, `submitted`, `held`, `failed`, `reopened`, `done`, `orphan_removed`. Any other kind is rejected with exit code 3. `graph_changed` is also accepted: it records a change to the dependency shards (see "dependency-shard record").
- **Q-LOG-02.** Every lifecycle transition continues to be recorded in the existing event log by the existing mechanism. The queue log adds queue facts only and is the source for metrics.

## Heterogeneous workers

- **Q-HET-01.** The queue stores no model name, vendor, account or host. The only worker facts stored are a slug label, a kind (`ai`, `script`, `human`) and capability tags drawn from configuration. Test: a scan of every queue record schema finds no free-text field for those.
- **Q-HET-02.** A `human` worker with no AI tool can complete the full path: claim, start, renew, release, report, hold and submit, through plain wrapper commands. No step requires a subprocess adapter, a network call or an AI tool. Test: run a task end to end with only human-kind calls.
- **Q-HET-03.** Actor labels follow who performed the action: a person at the keyboard is `human`, a subprocess the wrapper started is `wrapper`, an AI-originated proposal is `model`. Worker kind never grants authority; approval still requires actor `human` (TM-10). Test: an `ai` worker kind cannot approve.
- **Q-HET-04.** Routing from tags to workers is configuration and outside this specification except that the wrapper refuses a claim that does not satisfy Q-CLAIM-06. Local-only or no-local-model operation changes nothing in the queue.

## Parallelism metrics

All metrics are computed by replaying the verified queue chain (Q-REC-04) for a time window `[t0, t1]`. They are never stored counters. A replay of the same log and window always yields the same numbers. Metrics report counts and task ids only, never message text.

- **Q-MET-01.** `running(t)` is the number of tasks whose latest entry at or before `t` has `state_after` equal to `running`. A running interval starts at a `started` entry and ends at the next entry for that task that leaves `running` (`submitted`, `held`, `failed`, `expired`, `released`). An interval still open at `t1` ends at `t1`.
- **Q-MET-02.** Derived from `running(t)`: `peak_running` is the maximum over the window, and `average_running` is the sum of interval lengths in seconds divided by `t1 - t0`. Test: a fixture with known intervals yields exact values.
- **Q-MET-03.** `ready-not-running(t)` is the number of tasks in state `ready` at `t` whose `not_before` is not in the future. Tasks in `dispatched` are claimed work waiting for capacity and are reported separately as `dispatched(t)`. Tasks held back by `not_before` are `deferred(t)`. Tasks in `waiting` and holds are never counted. Test: twelve ready tasks, one dispatch pass: twelve attempts, `ready-not-running=0` at the end of the pass.
- **Q-MET-04.** `dispatch_attempts` is the number of `claimed` entries in the window. `dispatch_failures` is the number of `dispatch_failed` entries plus the number of `expired` entries whose lease never reached `started`. `dispatch_successes` is the number of `claimed` entries followed by `started` under the same `lease_id`. Invariant at any `t1`: `dispatch_attempts = dispatch_successes + dispatch_failures + dispatched(t1) + released-before-start`. Test: the invariant holds on every fixture, including crash fixtures.
- **Q-MET-05.** Metrics for a window with a missing, broken or reordered chain are not produced; the command returns exit code 8. A metric is never estimated from a partial log.
- **Q-MET-06.** A metrics report prints fixed field names and integers or decimals only. It contains no task titles, summaries or messages. Test: sentinel text in a title never appears in output (compare TM-08).

## Open questions

Decided items carry the owner's decision date. OQ-10 is still open.

- **OQ-1: lease refusal exit code. DECIDED 2026-11-27.** Exit code 12 with the name `lease_refused`, recorded in `docs/exit-codes.md`.
- **OQ-2: event-log version. DECIDED 2026-11-27.** Queue facts stay in `queue.jsonl`; the event log stays at version 1, no migration.
- **OQ-3: automatic retry. DECIDED in principle 2026-11-27.** A bounded wrapper-only automatic retry is allowed (a small configured limit, then a human decides). It needs a lifecycle amendment; the draft is in `docs/lifecycle.md` ("Proposed amendment", not in force) and awaits review. Until then no automatic retry (Q-RET-02) and an expired running lease ends in `failed` (Q-LEASE-06).
- **OQ-4: by-hand `running`. DECIDED 2026-11-27.** The wrapper records open to running with actor `wrapper` when the worker asks to start, including a by-hand start. The lifecycle wording stands as built.
- **OQ-5: graph size. DECIDED 2026-11-27.** Shard per task now; see "dependency-shard record".
- **OQ-6: apply gate milestone. DECIDED 2026-11-27.** An `apply` edge opens when the prerequisite's `apply_finished` is recorded.
- **OQ-7: `base_sha256` source. DECIDED 2026-11-27.** The wrapper computes it at claim time of the draft step. The work belongs to issue 18 (apply slots); `docs/queue.md` holds the stub.
- **OQ-8: lease secrecy. DECIDED 2026-11-27.** Per-worker credentials are wanted. This is a design change larger than a patch: see `docs/design/lease-credentials.md`. Until it is implemented the lease id is a bearer token and TM-17 residual risk stands.
- **OQ-9: priority. DECIDED 2026-11-27.** No priority field. Apply admission is ordered by `(created_at, id)` only.
- **OQ-10: shared filesystems.** Locking relies on advisory `flock` on a local filesystem. Behavior on network or synced folders is not specified and is unsupported until tested.
