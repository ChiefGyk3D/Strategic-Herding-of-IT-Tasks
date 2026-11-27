# Queue Implementation Notes (issue 17)

**SELF-REVIEW:** like `docs/queue-spec.md`, this is the implementer's own account. A human peer review is still required. The spec is the authority; this file says what was built, how it maps to the spec, what was left out and what still needs a human decision.

## What exists

| Module | Content |
|---|---|
| `shit/dependencies.py` | `dependency-graph` validation, cycle detection (iterative), missing-node detection, add and remove edge, readiness helpers. Pure functions. |
| `shit/queue_records.py` | `queue-entry` and `lease` validation, the transition table, per-kind field rules, configuration ranges and `replay`, which rebuilds every task's queue state from the log and rejects impossible sequences. |
| `shit/queue.py` | Storage and operations: `enqueue`, `add_edge`, `remove_edge`, `claim`, `start`, `renew`, `release`, `submit`, `fail`, `hold`, `reopen`, `complete`, `reclaim`, `status`, `render_view`. |
| `shit/project.py` | Two confined helpers: `list_dir` and `create_exclusive` (create-if-absent by temp file and hard link, never follows a symlink). |
| `shit/task.py` | `_transition_locked` split out of `transition` so a queue commit can run a lifecycle step under the lock it already holds; `create_task` refuses an id that appears in the event log (Q-RES-01). |

Files under `.shit`: `queue.jsonl` with the sidecar `queue.chain`, `dependencies.json`, `leases/<task-id>.json`, `queue-pending.json` (the commit point, present only during a mutation) and the generated `QUEUE.md`.

Every operation takes the store lock, finishes an interrupted commit, loads and verifies everything (chain, replay, graph, lifecycle agreement), runs maintenance (orphan leases, missing lease files, expiry, waiting to ready) and then does its own step. A queue state that cannot be verified is exit code 8 and nothing is written.

## Decisions recorded by the owner (2026-11-27)

- OQ-2: queue facts stay in `queue.jsonl`; the event log stays at version 1. Implemented as specified.
- OQ-6: an `apply` edge opens when the prerequisite's `apply_finished` is recorded. Implemented in the claim check: a claim of step `apply` is refused (exit code 4) while any `apply` prerequisite has no `apply_finished` entry. Nothing in issue 17 writes `apply_started` or `apply_finished` (apply slots belong to the apply-serialization work), so such a claim stays refused until that work lands. That fails safe.
- OQ-3: bounded wrapper-only retry is allowed in principle. The lifecycle amendment is drafted in `docs/lifecycle.md` ("Proposed amendment", not in force) and needs review. The retry itself is not implemented: it needs the amendment first, and the current code keeps `running` plus expired lease ending in `failed` (Q-LEASE-06) and `failed` to `ready` human-only (Q-RET-02). Stub: the transition table in `queue_records.ALLOWED` and the `expired` handling in `queue._expire` are the two places to change.

## Open questions still undecided, and how each touches the code

| Question | Effect on this implementation |
|---|---|
| OQ-1 exit code for a refused lease | Code 12 and the name `lease_refused` are used as the spec proposes, in one constant (`errors.LEASE_REFUSED`) and one row in `docs/exit-codes.md` marked PROPOSED. If the number changes, change that constant, the row and the tests that assert 12. |
| OQ-4 `running` entered by hand | `start` records `open` to `running` with actor `wrapper` when the worker asks, exactly as the spec's reading. If the lifecycle text changes, only `queue.start` changes. |
| OQ-5 graph size | One `dependencies.json` limited to 65,536 bytes. Over the limit `dump_graph` fails with exit code 7 (tested). Sharding would change `_load_graph` and the two write sites. |
| OQ-7 `base_sha256` source | Not implemented (belongs to apply serialization). No field exists for it. |
| OQ-8 lease secrecy | Bearer token in a file; compared in constant time. The chain proves every lease field, so a hand-edited lease file (for example a longer expiry) is exit code 8. Per-worker credentials would change `_check_presented`. |
| OQ-9 priority | No `priority` field exists. Nothing in issue 17 orders by priority (that is apply admission, Q-SER-04). |

## Spec gaps found while implementing (not decided here)

These are for a human to settle; the code takes the least risky reading and says so.

1. **No log kind for ready to waiting.** Q-DEP-09 and the transition table allow `ready` to `waiting`, but Q-LOG-01 has no kind for it. The code does not persist that step. A stored `ready` task whose gate re-closes is reported as `waiting` by `status` and refused by `claim` (the effective state is recomputed from files every time, Q-DEP-08). Only waiting to ready is written (`ready` entry). This keeps Q-ST-01 true for the stored state and makes the stored state lag the effective one in that one direction.
2. **Release from `running`.** The table has no `running` to `ready`, and the lifecycle forbids `running` to `open`, yet Q-MET-01 lists `released` as a way out of `running` and Q-HET-02 lists release in a human worker's path. `release` is accepted only while `dispatched`; from `running` it is exit code 4.
3. **Review step while a lease is live.** The worked example has a third worker claim a `review` step of a task that is already `running` under another lease, but Q-CLAIM-04 allows one live lease per task. The code follows Q-CLAIM-04: a second claim is exit code 4.
4. **Lease expiry is not in the queue-entry field list.** The spec's entry fields have no expiry, so a lease file's `expires_at` could not be checked against the chain. `claimed` and `renewed` entries carry an extra optional `expires_at` so the chain proves the lease file in full (Q-LEASE-10). This adds a field to a record type that has no earlier version.
5. **`orphan_removed` for a task that is not in the queue.** The entry would have no queue state to carry, so the file is removed without an entry.
6. **Hold to running.** The table lists `hold-*` to `running` (wrapper resumes). A running task needs a live lease and a resume has no claim, so it is not implemented; a hold leaves only through a human `reopen`.
7. **Whole-queue deletion.** Q-RES-02 asks for detection of a restored older copy. A queue prefix that disagrees with the lifecycle state, a truncated or edited chain and a broken file are all exit code 8 (tested). Deleting `queue.jsonl` and `queue.chain` together leaves nothing to compare, so a task that was never enqueued in that copy is just not queued. Detecting that needs the signing the threat model already lists as a residual risk.

## Deferred to later work (named in the spec, not part of issue 17)

- Capability tags (Q-CLAIM-06) and execution capacity by resource class (Q-CLAIM-04 second half): the claim request has no tag field and nothing counts leases per worker. Issue 18 owns routing and capacity.
- `dispatch_failed` backoff and the consecutive-failure hold (Q-RET-04, Q-RET-05): the entry kind and `not_before` are validated and honored on claim (Q-RET-06), but nothing writes them. Issue 18's dispatcher does.
- Apply slots, `base_sha256`, `apply_started` and `apply_finished` writers (Q-SER-01, 02, 04 to 07): the kinds are accepted by validation; nothing produces them.
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
| TM-18 | `tests/test_dependencies.py`, `test_cycles_unknown_and_duplicate`, `test_hand_edited_cycle_fails_closed`, `test_model_actor_cannot_change_the_graph` |
| TM-19 | `test_complete_requires_the_close_rules_and_is_terminal`, `test_closed_id_not_reusable`, `test_restored_older_queue_fails_closed` |
| TM-20 | `test_stale_generation_submit_refused` |
| TM-22 | `test_queue_md_is_a_view_only`, `test_readiness_is_recomputed_from_files_after_deleting_every_derived_file` |
| TM-23 | `test_renewal_bounds_enforced`, `test_total_length_bound` |
| TM-24 | `test_queue_crash_matrix_recovers_consistently`, `test_crash_inside_the_nested_lifecycle_transition_recovers`, `test_orphan_lease_removed` |

TM-21 (apply serialization) is not covered here.
