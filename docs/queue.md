# Queue Implementation Notes (issue 17)

**SELF-REVIEW:** like `docs/queue-spec.md`, this is the implementer's own account. A human peer review is still required. The spec is the authority; this file says what was built, how it maps to the spec, what was left out and what still needs a human decision.

**Scope of the protection (read first).** The queue, leases and recovery in issue 17 protect against crashes, races and mistakes of cooperating workers that run as the same operating-system user. They do not protect against a hostile process of that user (it can read every lease id, plant or rewrite any file under `.shit`, and ignore the advisory lock), nor against a rewrite or restore of the whole store. Multi-tenant or adversarial use needs an operating-system identity boundary, a credential service that holds the secrets outside the workers' reach (issue #48). Every "proves", "refuses" and "fails closed" below is meant inside that scope.

## What exists

| Module | Content |
|---|---|
| `shit/dependencies.py` | Shard validation and bytes, in-memory graph validation, cycle detection (iterative), missing-node detection, add (single and batch) and remove edge, readiness helpers. Pure functions. |
| `shit/queue_records.py` | `queue-entry` and `lease` validation, the transition table, per-kind field rules, configuration ranges and `replay`, which rebuilds every task's queue state from the log and rejects impossible sequences. |
| `shit/queue.py` | Storage and operations: `enqueue`, `add_edge`, `add_edges`, `remove_edge`, `claim`, `start`, `renew`, `release`, `submit`, `fail`, `hold`, `reopen`, `complete`, `reclaim`, `status`, `render_view`. |
| `shit/store.py` (queue section), `shit/store_queue.py` | The same operations on the SQLite store (issue 61, C4): one write transaction each, the adapter with the names of `queue.py`. The file queue stays until C7. See `docs/store.md`, "Queue operations". |
| `shit/project.py` | Two confined helpers: `list_dir` and `create_exclusive` (create-if-absent by temp file and hard link, never follows a symlink). |
| `shit/task.py` | `_transition_locked` split out of `transition` so a queue commit can run a lifecycle step under the lock it already holds; `create_task` refuses an id that appears in the event log (Q-RES-01). |

Files under `.shit`: `queue.jsonl` with the sidecar `queue.chain`, `deps/<task-id>.json` (one dependency shard per dependent task), `leases/<task-id>.json`, `queue-pending.json` (the commit point, present only during a mutation) and the generated `QUEUE.md`.

Every operation takes the store lock, refuses with exit code 8 if a `queue-pending.json` exists (only `queue.recover` finishes a queue commit; a task-only `pending.json` is finished after the planner proved the queue), loads and verifies everything (chain, replay, graph, lifecycle agreement), runs maintenance (orphan leases, missing lease files, expiry, waiting to ready) and then does its own step. A queue state that the recovery planner cannot verify is exit code 8 and nothing is written before the planner's proof (Q-REC-02); maintenance steps after it are separate committed steps, and an orphan lease file is deleted only after its `orphan_removed` entry has committed, and the entry is not repeated after a crash between the two.

## Decisions recorded by the owner (2026-11-27)

- OQ-1: exit code 12 `lease_refused` is approved (`errors.LEASE_REFUSED`, `docs/exit-codes.md`).
- OQ-2: queue facts stay in `queue.jsonl`; the event log stays at version 1. Implemented as specified.
- OQ-3: a bounded wrapper-only automatic retry is allowed in principle. The lifecycle amendment is drafted in `docs/lifecycle.md` ("Proposed amendment", not in force) and needs review. The retry is not implemented: it needs the amendment first, and the code keeps a running task with an expired lease ending in `failed` (Q-LEASE-06) and `failed` to `ready` human-only (Q-RET-02). Stub points: `queue_records.ALLOWED` and `queue._expire`.
- OQ-4: kept as built. `start` records open to running with actor `wrapper` when the worker asks, including a by-hand start by a human worker.
- OQ-5: sharded per task now. See "Dependency shards" below.
- OQ-6: an `apply` edge opens after the prerequisite's `apply_finished` is recorded. A claim of step `apply` is refused (exit code 4) while any `apply` prerequisite has no `apply_finished` entry. Nothing in issue 17 writes `apply_started` or `apply_finished`, so such a claim stays refused until the apply-slot work lands. That fails safe.
- OQ-7: the wrapper computes `base_sha256` at claim time of the draft step. **STUB, issue 18 (apply slots):** nothing is computed or stored yet; a marked comment in `queue.claim` shows where. Not part of issue 17.
- OQ-8: per-worker credentials are wanted. Not built here; designed in `docs/design/lease-credentials.md` for a separate implementation issue. Until then the lease id is a bearer token (TM-17 residual risk).
- OQ-9: no priority field. Apply admission will order by `(created_at, id)` only (issue 18); the spec is updated.

## Dependency shards (OQ-5)

The graph is stored as one `dependency-shard` record per dependent task, `deps/<task-id>.json`, listing the tasks it needs. Each edge lives in exactly one shard.

- Caps: 200 `needs` per task (`dependencies.MAX_NEEDS_PER_TASK`, about 12 KB at the cap, far under the 65,536-byte record limit) and 2,000 queued tasks and shards in total (`dependencies.MAX_TASKS`). A breach is exit code 7 (adding an edge, or enqueueing past the total). Both are constants in one module; raising them is a code change plus a test.
- Chain proof: every graph change appends one `graph_changed` entry carrying the SHA-256 of each shard it wrote (64 zeros for a deleted one). The loader requires the shard files to equal the chain's set exactly and each file's digest to match.
- Fail closed (exit code 8, nothing written): a shard deleted, edited or added without a chain entry; a record whose `task_id` differs from its file name (two shards disagreeing about an edge); an oversize or malformed shard even when its digest was rechained; a cycle, self edge or dangling edge; a stray file name; a leftover `dependencies.json` from the single-file layout (never released, so there is nothing to migrate).
- Atomicity: the commit point `queue-pending.json` lists every shard to write or delete with its exact content and the entry that records their digests. Recovery proves the whole commit read-only first, nested task commit included (Q-REC-02), and only then applies it idempotently; a refusal in the proof writes nothing and keeps the record. An unrecoverable pending record has no API to discard it: the operator procedure is in Q-REC-02. `add_edges` changes several shards in one commit or none.
- A shard is written with an atomic replace. Edge add and remove stay human or wrapper only, and a task with a live lease cannot lose an edge.

## Accepted gaps (owner accepted these on 2026-11-27)

The spec text now matches the code.

1. There is no log kind for ready to waiting. Only waiting to ready is written. The effective state is recomputed from files on every operation, so a claim of a re-closed task is refused and `status` reports `waiting`.
2. Release from `running` is exit code 4 (the table has no `running` to `ready`). The spec's worked example is corrected: a second claim on a task whose lease is live, whatever the step, is refused (Q-CLAIM-04).
3. `claimed` and `renewed` entries carry an optional `expires_at`, so the chain proves the lease file in full (Q-LEASE-10).
4. `orphan_removed` is written only for a task that is in the queue.
5. A hold leaves only through a human `reopen`. The table's hold to running (wrapper resume) is not implemented.
6. Deleting `queue.jsonl`, `queue.chain` and the shards together is undetectable without signing. It is listed under residual risks in `docs/threat-model.md`.

## Hardening notes (Daybreak round 1)

- The store lock is taken on a descriptor of the `.shit` directory, not on the `lock` file path. Replacing the file does not admit a second holder. Anyone who can move or replace the `.shit` directory itself can still redirect a process to a different store; that needs a different OS identity (see the threat model).
- An exclusive publication that stopped between its link and its unlink leaves a `.tmp-` name and its destination sharing one inode. The next operation removes exactly that pair; any other extra link is still refused (exit code 5).
- Replay enforces global ceilings on every lease whatever the configuration: a TTL of at most 86,400 seconds from the entry's own time, an absolute lifetime of at most 604,800 seconds, at most 1,000 renewals, and no renewal at or after the lease's expiry. `apply_started` and `apply_finished` are accepted only as one ordered pair under an `apply`-step lease, once per attempt. Apply claims still refuse while an apply prerequisite has not finished.
- Every mutation checks the clock against the queue tip and the event-log tip, and `EventLog.prepare` validates the whole sequence with the candidate, so time never goes backwards in either chain.
- An append that would cross the 64 MiB log cap is refused with exit code 7 before anything is written. The check for a lifecycle event assumes the largest record, so it refuses slightly early.
- The task cap (2,000) counts queued ids and shard owners together. An empty persisted shard is refused.
- Maintenance removes every temporary leftover in one pass, and readiness reads an index of edges by task.
- Entry points refuse on sight: any locked entry point (queue operation, task, evidence, report, review, decision, yardstick) that finds `queue-pending.json` returns exit code 8 and writes nothing, whether the record is genuine or planted. Only `queue.recover(project)` plans and applies it, after proving it read-only (Q-REC-02). Task and artifact writers prove the whole queue state before they commit, even with nothing pending; readers do so only when a pending task record forces a write. `shit check` reports the queue state and any pending record read-only (exit code 8).
- Bounded work (M4, issue 54): privacy-scan directory width and cumulative byte budgets, a scan deadline, `scandir`-based bounded listings and an operation deadline while holding the lock are done. See [bounded-work.md](bounded-work.md).

## Still open

| Question | Effect on this implementation |
|---|---|
| OQ-10 shared filesystems | Locking relies on advisory `flock` on a local filesystem. Unsupported on network or synced folders until tested. |

## Deferred to later work (named in the spec, not part of issue 17)

- Capability tags (Q-CLAIM-06) and execution capacity by resource class (Q-CLAIM-04 second half): the claim request has no tag field and nothing counts leases per worker. Issue 18 owns routing and capacity.
- `dispatch_failed` backoff and the consecutive-failure hold (Q-RET-04, Q-RET-05): the entry kind and `not_before` are validated and honored on claim (Q-RET-06), but nothing writes them. Issue 18's dispatcher does.
- The file backend does not produce apply markers. The SQLite backend exposes apply start and finish and serializes lexical canonical target keys, including ancestor/descendant overlap. Apply priority and `base_sha256` remain unimplemented there, so Q-SER-04 and Q-SER-06 are not claimed.
- Parallelism metrics (Q-MET-01 to Q-MET-06).
- A command-line front end for the queue. The library API is complete for a human worker; a CLI wrapper is not written.
- A task-level `shit check` comparison of queue and lifecycle: the queue verifies the agreement itself on every operation.

## Configuration

`queue_records.make_config` validates `lease_ttl_seconds`, `lease_max_seconds`, `lease_max_renewals`, `max_attempts`, `max_dispatch_failures` and `dispatch_backoff_seconds` against the spec's ranges and rejects unknown keys. Operations take an optional `config` dict. No file loader exists yet.

## Test map (threat-model rows)

| Row | Tests (`tests/test_queue.py` unless noted) |
|---|---|
| TM-15 | `test_simultaneous_claims_single_winner` (set `SHIT_RACE_TRIALS=200` for the spec's full count), `test_simultaneous_claims_from_two_processes`, `test_precreated_lease_refuses_claim` |
| TM-16 | `test_clock_backward_fails_closed`, `test_forward_clock_jump_does_not_double_claim`, `test_claim_rejects_extra_fields_and_worker_supplied_values` |
| TM-17 | `test_forged_lease_refused`, `test_hand_written_lease_file_grants_nothing`, `test_tampered_live_lease_file_fails_closed` |
| TM-18 | `tests/test_dependencies.py`, `test_cycles_unknown_and_duplicate`, `test_chain_proven_cycle_fails_closed`, `test_shard_deleted_fails_closed`, `test_shard_edited_fails_closed`, `test_shard_added_without_a_chain_entry`, `test_two_shards_disagreeing_on_one_edge_fail_closed`, `test_model_actor_cannot_change_the_graph` |
| TM-19 | `test_complete_requires_the_close_rules_and_is_terminal`, `test_closed_id_not_reusable`, `test_restored_older_queue_fails_closed` |
| TM-20 | `test_stale_generation_submit_refused` |
| TM-22 | `test_queue_md_is_a_view_only`, `test_readiness_is_recomputed_from_files_after_deleting_every_derived_file` |
| TM-23 | `test_renewal_bounds_enforced`, `test_total_length_bound` |
| TM-24 | `test_queue_crash_matrix_recovers_consistently`, `test_crash_inside_the_nested_lifecycle_transition_recovers`, `test_orphan_lease_removed`, `test_multi_shard_graph_change_recovers_from_a_crash_at_every_stage` |

TM-21 (apply serialization) is not covered here.
