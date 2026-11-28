# Queue Implementation Notes

The queue is implemented by the SQLite store described in [store.md](store.md). This document summarizes the current implementation; [queue-spec.md](queue-spec.md) is the behavioral specification.

## Persistence

`store.sqlite3` is the only supported queue backend. Queue entries, dependency edges, leases, and apply slots are database rows. Each mutation uses one `BEGIN IMMEDIATE` transaction, so its queue, lifecycle, lease, graph, and audit-chain effects commit together or roll back together.

Record bodies remain confined files whose digest and size are indexed by the database. `QUEUE.md`, when produced, is a generated view and is never input.

The former multi-file queue protocol was removed. Its artifacts, including queue logs, chain files, dependency and lease directories, anchors, generated queue views, and pending-commit files, are not read, written, recovered, or reconciled. Their presence makes ordinary store commands refuse with exit code 9; a new store is required. `shit init` and `shit check` provide their own explanatory refusal.

## Modules

| Module | Current responsibility |
|---|---|
| `shit/store.py` | Schema, transactions, queue and dependency operations, leases, apply slots, invariant checks, and audit-chain reads. |
| `shit/store_queue.py` | Project-level queue adapter, audit-export guard, record-body milestone checks, and bounded ambiguous-commit retry. |
| `shit/queue.py` | Import-compatible names forwarding to `store_queue`. |
| `shit/queue_records.py` | Pure validation and replay of queue entries. |
| `shit/dependencies.py` | Pure graph validation and canonical dependency-shard bytes used in graph-change audit entries. |

## Graph, leases, and apply slots

Dependencies are rows in `edges`; a graph mutation and its `graph_changed` queue entry share one transaction. The entry carries canonical shard digests as audit evidence, but no dependency-shard file is created.

Live leases are rows in `leases`. Claims use monotonically increasing generation numbers as fencing tokens. Renewal bounds, expiry, release, submission, failure, and holds are checked against the queue chain and current lifecycle state in the same transaction.

Apply coordination uses rows in `apply_slots`. Canonical lexical target keys conflict when equal or when one is an ancestor of the other. Apply-start and apply-finish queue entries are ordinary database queue entries; no apply-marker file is created.

## Crash and concurrency behavior

SQLite is the commit point. A crash before `COMMIT` leaves the transaction unapplied; a successful commit contains all of the operation's database effects. The separate audit export is refreshed after commits and guards later writes against a restored or divergent database.

`shit check` uses one connection and one read transaction for task states, state-dependent body checks, store invariants, queue reporting, and audit comparison. It reports the event and queue sequence numbers of that snapshot once.

The concurrency harness races cooperating processes, kills writers at deterministic fault stages, and compares accepted operations with the pure model. The threat boundary remains same-user cooperation; a process with direct write access to the database can bypass the wrapper.

## Deferred work

- Capability tags and per-resource capacity.
- Dispatcher-produced backoff and consecutive-failure holds.
- Apply priority and `base_sha256`.
- Parallelism metrics and a queue command-line front end.

## Configuration

`queue_records.make_config` validates lease TTL, maximum lifetime, renewal, attempt, dispatch-failure, and backoff values. Queue operations accept the validated configuration; no separate queue-state backend or file loader exists.
