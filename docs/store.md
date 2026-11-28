# SQLite Store Foundation (issue 59, epic 57)

**SELF-REVIEW:** the implementer's own account. A human peer review is still required.

`shit/store.py` is the base of the SQLite store that is replacing the file-based queue (`docs/queue.md`). The queue operations are ported in a delimited section ("Queue operations" below), and the file queue remains available during the migration. `shit/store.py` is the only module that imports `sqlite3`.

## Selecting a backend

The validated project configuration selects one backend for command-line task, record, dependency and queue operations:

```toml
[store]
backend = "sqlite"
busy_timeout_ms = 60000
```

`backend` accepts only `"sqlite"` and defaults to it. The removed `"files"` value is a configuration error. Command handlers bind `store_tasks` and `store_queue`; there is no backend selection path.

`shit init --backend sqlite` creates the database only when validated configuration already records `backend = "sqlite"`; an explicit option that disagrees with configuration is exit code 2. If any file-protocol-only artifact exists and no SQLite database exists, SQLite initialization is refused with exit code 9 instead of creating two active stores. That catalog covers the event and queue chains, dependency shards and the refused legacy dependency file, leases, pending commits, the queue anchor and the generated view. Body directories, the lock, the SQLite database and its audit export are classified separately because SQLite operations create or use them too; they are not evidence of file-protocol state. `shit check` uses the same file-only catalog to report mixed state beside a database. A `[store] backend = "sqlite"` configuration also makes plain `shit init` create it. The `check` and `audit` commands are registered in the same command-line parser as the task commands.

## Migration and rollback

Phase 2 is complete: SQLite is the only backend. `shit init` and `shit check` refuse leftover file-protocol state when there is no database. There is no one-way import tool, so start a new store and retain the old state separately if needed.

The supported rollback is restoring a coordinated backup of `store.sqlite3` and the record-body directories. The tooling verifies a restored database and audit export, but does not create, select, or reconcile backups. Stop writers before restoring, then run `shit check` and `shit audit verify --database`.

**Scope of the protection.** Like the queue, the store protects against crashes, races and mistakes of cooperating processes of the same operating-system user. A hostile process of that user can open the database directly and bypass every control here. The checks below catch accidents and a casual swap; they are not a boundary against that user.

## Opening the store

`store.init_store(project, adopt_empty=False)` creates the database once. `store.connect(project, busy_timeout_ms=None)` is the only way to open it. Both refuse, in this order:

1. The floor: SQLite older than 3.37 (STRICT tables) or Python older than 3.11 is exit code 8.
2. The `.shit` directory: it must be owned by the effective user with no group, other or special bits, and the path SQLite will open must still be the directory the project holds a descriptor on (checked before and after the open; a renamed and replaced directory refuses with exit code 5).
3. The file, by `lstat` before opening: a regular file, not a symlink, link count 1, owned by the effective user, and a mode that is exactly within owner read and write (the whole mode is compared, so setuid, setgid, sticky and owner-execute bits refuse as well as group and other bits; exit code 5; a missing file is 9). The `-wal`, `-shm` and `-journal` files get the same check when they exist.
4. After opening, the device and inode of every descriptor the process holds on that path (read through `/proc/self/fd` on Linux) must be exactly the file that was checked (exit code 5). Without `/proc` the file is compared again by path, which is weaker.
5. The header and schema: `application_id` and `user_version` must match, and the schema fingerprint must match (exit code 8). These reads do not prove the file is intact: a database whose pages are damaged but whose header, schema text and the pages read at open are fine opens normally. `verify(conn, deep=True)` runs `PRAGMA integrity_check`, which reads every page.

**Sidecar files.** After the open, each existing `-wal`, `-shm` and `-journal` is checked again with `lstat`, and must be the same file (device and inode) as in a snapshot taken just before the open. A change is retried once with a fresh snapshot (another process closing the last connection legitimately removes and recreates them) and refused with exit code 5 the second time. A sidecar that existed before the open and is gone afterwards is treated the same way as a replaced one. A sidecar that sqlite creates itself is checked for type, link count, owner and mode.

**Residual race window.** Python's `sqlite3` cannot pass `O_NOFOLLOW` to the open. Between the `lstat` and the open, a process that can write the `.shit` directory can replace the file. The inode comparison in step 4 detects that after the fact and the connection is closed, but sqlite has by then opened the replacement and read its header. Only a process of the same user can use this window, which is outside the scope above. The same window exists for `-wal` and `-shm`, and a replacement made after the post-open check is not seen by either check (a descriptor sqlite already holds keeps pointing at the old file). The pre-created file in a 0700 directory is what keeps other users out.

Every connection sets, and reads back: `foreign_keys=ON`, `journal_mode=WAL` (refused unless the answer is `wal`), `synchronous=FULL`, `busy_timeout`, `trusted_schema=OFF`, `recursive_triggers=ON` (so that `INSERT OR REPLACE` fires the delete triggers), the defensive flag (`SQLITE_DBCONFIG_DEFENSIVE`, where Python 3.12 or later has it), and `isolation_level=None`, so Python never opens a transaction on its own.

## Creation and modes

`init` runs under umask 077 (restored afterwards; the umask is process-wide, so call it from single-threaded setup), creates `store.sqlite3` with `O_EXCL` and mode 0600, and installs the schema in one `BEGIN IMMEDIATE` transaction, only into a brand-new empty database. SQLite gives `-wal` and `-shm` the mode of the main file, so all three are 0600. A complete existing store is verified and never rewritten. A file that already exists and that this call did not create is refused (exit code 8) unless it is a complete store, so an empty or foreign file is never adopted by accident. `adopt_empty=True` is the explicit recovery for a crash between creating the file and installing the schema: it initializes only a brand-new empty database and still refuses anything with content. The schema install is one transaction. The arguments are validated before any file is created, and if anything fails after this call created the file, the file (and any SQLite side files its failed open left) is removed by name inside the held directory, but only while the name is still the file this call created; a file this call did not create is never removed. The check is by device and inode, so if another process deletes the created file and a new file is given the same inode number, the new file looks like the created one; that needs a same-user process and is within the residual window above. `adopt_empty` therefore matters only after a hard crash (power loss, kill) between creating the file and installing the schema.

## Writing

`store.write_transaction(conn)` is the only way to write. It runs `BEGIN IMMEDIATE` (never a deferred read that later upgrades, which fails with an unretried busy-snapshot error), commits on success, and rolls back on any exception, including `KeyboardInterrupt`. An SQLite error raised inside is mapped to a `ShitError` after the rollback.

If the rollback itself fails, is interrupted, or the connection is still inside a transaction afterwards, the connection is closed (so it cannot be reused) and exit code 8 is raised with the fixed message.

**An error at COMMIT.** If `COMMIT` raises while the connection is still in its transaction, the commit did not happen: it is rolled back and the error maps normally (a busy store is exit code 13) and the connection stays usable. If `COMMIT` raises and the connection is no longer in a transaction (or cannot say), nobody can tell whether the write reached storage (an I/O error can follow a completed commit). The connection is closed and `store.AmbiguousCommit` is raised (a `ShitError`, exit code 8, the usual fixed message, with no chained cause, so no SQL text or path travels with it). The caller must open a new connection and re-read before it repeats anything that is not idempotent. The later queue operations need idempotency keys or a reconciling read for this case.

`tests/test_store_lint.py` reads every module under `shit/` and enforces, by syntax tree:

- no SQL text built by f-string, `%`, `.format` or `+`; only the named schema builders, which use fixed vocabulary from this code and validate every word, are exempt;
- only `shit/store.py` imports `sqlite3`, and nothing outside it reaches the module (`store.sqlite3`), imports `builtins`, `importlib`, `operator`, `ctypes` or `inspect`, or uses `eval`, `exec`, `compile`, `vars`, `__import__` or attribute-reaching dunders (`__getattribute__`, `__dict__` and similar);
- no module but the store names `execute`, `executemany`, `executescript`, `cursor`, `commit`, `rollback` or another connection method, in any form: a call, an alias (`run = conn.execute`), `getattr` and the other reflection helpers, or a string key;
- inside the store: every database method is a direct call in one of the exact functions listed for it in the test (a later change adds a function to that list on purpose), `sqlite3.connect` is a direct call in the two functions that open a database, with the argument each is allowed (`_uri(path)` and `':memory:'`), there is no `executescript` and no cursor, the SQL passed to `execute` is a literal, or the loop variable of `for ... in schema_statements()` in the two schema consumers, `schema_statements()` is called only there, a plain read is `SELECT` or one of an exact list of `PRAGMA` reads without value or argument list (so `PRAGMA foreign_keys(OFF)` is a write), and any other statement runs inside `with write_transaction(...)`, except in the functions that own their transaction (`_open_once`, `_install_schema`, `write_transaction`, `_fail_transaction`, `expected_fingerprint`).

Authority is by lexical identity, not spelling: a privileged function (one on an allowlist, a connector, a transaction owner, a schema consumer, a builder, or `write_transaction`) must be a unique, top-level definition; a nested function, a lambda, a duplicate definition, a parameter, assignment, import or global that reuses such a name is a finding, and a `write_transaction` that a local name shadows does not count as the guard. The lint is static and reads source only. The schema builders are exempt from the text-building rule as whole functions, so their review is a human one; the lint cannot prove a value interpolated there is safe, only that the builders feed nothing but `schema_statements()` into the two consumers. A determined change to the lint itself is stopped by review, not by the lint. It catches the practical ways the code could reach the database around `connect` and `write_transaction`; it is not a proof that no way exists.

## Lock wait

The wait for another writer is `[store] busy_timeout_ms` in `config.toml` (0 to 600000, default 60000; `templates/config.toml`). The caller passes `cfg["store"]["busy_timeout_ms"]` to `connect`. A wait that runs out is exit code 13.

## Schema

`user_version` 1, `application_id` 0x53484954. All tables are `STRICT`.

| Table | Content |
|---|---|
| `meta` | Key and value pairs for the store itself. |
| `tasks` | One row per task: state, owner, timestamps, blocked fields. Rows are never deleted. |
| `task_history` | Lifecycle transitions; the pair must be in the legal table of `docs/lifecycle.md`. Append-only. |
| `events` | The event log with its hash chain columns. Append-only. |
| `records` | Index of record files by kind, id, task, SHA-256 and size. Bodies stay files. |
| `queue_entries` | The queue log with its hash chain columns. Append-only. |
| `leases` | At most one live lease per task. |
| `edges` | Dependency edges, no self edge, one row per dependent, prerequisite and type. |
| `apply_slots` | One slot per apply target. |

The rules are generated from `shit/lifecycle.py`, `shit/queue_records.py`, `shit/dependencies.py` and `shit/records.py`, so the database cannot drift from the code:

- Vocabulary lists (states, kinds, steps, worker kinds, hold kinds, dependency types, event kinds) and the legal lifecycle pairs, with the actors allowed for each pair (a model cannot approve), on `task_history` and on `events` of kind `state_transition` alike.
- No text column may contain a NUL byte (SQLite's `length` and pattern checks stop at one). Timestamps also refuse year 0000 and hour 24, as the Python validators do. `command_failed` events accept any stable exit code (`errors.STABLE_EXIT_CODES`, 1 to 13), the same set the event validator uses.
- Timestamps must be real calendar times in the fixed `Z` form, not only the right shape. Event, history and queue messages are non-empty and at most 500 characters.
- Per queue-entry kind: the allowed actors and `state_after`, the columns the kind requires and the columns it forbids (from `queue_records.KINDS`), a `held` entry's state matching its hold kind, `held` and `failed` carrying a lease id and generation together, and a claim lifetime of at most the global ceiling.
- Triggers on `queue_entries` (before insert): a task is enqueued once and before anything else; the state change must be in the allowed table (`ALLOWED`, plus the re-entry pairs for a reopen, plus the two expiry pairs) and each kind must start from its own state; renewal and apply markers never change state; every lease-bearing entry names the task's latest claim; a claim's generation is the previous one plus one; a start's attempt is the previous one plus one; a start or renewal uses the claim's worker and step; a renewal comes before the previous expiry, extends it, stays within the time-to-live and total-lifetime ceilings and the renewal count; apply markers need an `apply` claim and the order none, started, finished; a reopen or done never happens while a lease is live; a claim needs no lease row and not before a `dispatch_failed` entry's `not_before`. The lease row is bound to the claim entry when it is inserted, so every entry that matches the claim also matches the row; for a renewal the row's expiry and renewal count must be either the entry's or the previous ones, so the row and the entry may be written in either order. A test replays random accepted sequences through `queue_records.replay` and requires that the database never accepts what replay rejects.
- Triggers on `events` and `queue_entries`: the new row's `prev_hash` is the previous row's `hash` (64 zeros for the first), the timestamp never goes back, and the sequence number is the previous maximum plus one (the first is 1) however it was supplied, so a record cannot be spliced behind the tip. A queue entry's id is `queue-` and its sequence number (four digits at least), as `queue_records.replay` requires. `graph_changed` shards must be a sorted JSON array of at most 32 objects with exactly a task id and a SHA-256 digest, the first task being the entry's. The hash itself is computed by the later writer code, not checked here. With `recursive_triggers` on, `INSERT OR REPLACE` cannot rewrite or delete a protected row: the implicit delete fires the delete trigger.
- Triggers on `leases` and `apply_slots`: a lease row equals the latest claim entry of the task (so write the claim entry first, then the lease); an update is exactly one renewal and changes no identity field; a lease with an apply slot cannot be deleted (delete the slot first); an apply slot needs a matching `apply`-step lease and is never edited.

Where a row breaks both a `CHECK` and a trigger, SQLite runs the before-triggers first, so the exit code is the trigger's.

## Invariants across statements

A trigger sees one statement. These invariants span several statements of one transaction, so the database cannot refuse a commit that leaves them broken (a crash or a writer that commits halfway can). They are the contract of the later write helpers, which must do each operation in **one** `write_transaction` in the order below and must call `store.verify(conn)` after every operation in their tests (and `verify(conn, deep=True)` in a periodic check). `verify` is read-only and returns the names of the invariants that do not hold. An SQLite failure while it reads (corrupt page, I/O error, busy, a closed connection) raises the same fixed-message `ShitError` as the rest of the module, never a raw SQLite exception:

| Invariant | Required order inside the transaction | `verify` name |
|---|---|---|
| A live queue state (`dispatched`, `running`) has a lease row, and a lease row exists only for one | claim entry first, then the lease row; delete the lease row when the claim ends | `claim_without_lease`, `lease_without_live_claim` |
| The lease row equals the latest claim entry | the row is bound to the claim when inserted (a trigger) | `lease_differs_from_claim` |
| The lease expiry and renewal count equal the log's latest expiry and renewed-entry count | a renewal is the `renewed` entry and the row update in the same transaction, either first; at most one row-first renewal may be outstanding | `lease_expiry_differs_from_log` |
| An apply slot hangs on a live `apply` lease of the same claim | take the slot after the lease row; delete the slot before the lease row | `slot_without_live_apply_lease` |
| The task row's lifecycle state is one the queue state allows | update the task row in the same transaction as the queue entry | `task_state_differs_from_queue` |
| Hash chains are contiguous and ordered | the writer computes the hash and links it | `events_chain_broken`, `queue_chain_broken` |
| Foreign keys hold | | `foreign_key_violation` |

Not enforced by the database at all, and left to the C3 and C4 write helpers: that `started` and `renewed` entries only happen while a physical lease row exists, the dependency graph rules (cycles, missing nodes), the digest of `graph_changed` shards against the shard records, the entry hash value, and idempotency of an operation whose commit was ambiguous.

The append-only and no-delete triggers raise `RAISE(ABORT, 'E<n>')` with only a token; `store.TOKEN_EXIT` maps each token to an exit code (8 for append-only and chain breaks, 4 for an illegal queue transition, 12 for lease and apply-slot refusals, 3 for a repeated enqueue or a wrong attempt number). Trigger conditions are written in this code from a small fixed alphabet; the builder refuses anything else.

**Schema fingerprint.** At every open, SHA-256 over the rows `(type, name, tbl_name, sql)` of `sqlite_master`, in a fixed order, is compared with `store.PINNED_FINGERPRINT`, a reviewed literal. A test regenerates the value from the DDL on a scratch in-memory database and fails until the literal is updated, so a DDL change is a deliberate edit of both. A dropped trigger, a trigger recreated with a different body, an altered or extra table, view or index, all refuse with exit code 8. A hand edit that keeps the exact text is not detected, and neither is a change made by a process that then restores it before the next open. The fingerprint is a schema check, not an integrity check.

**Portability.** The fingerprint hashes the raw `sqlite_master.sql` text, including the objects SQLite creates itself. The pinned value was generated and tested on SQLite 3.46; support starts at 3.37, and this has been verified only on the SQLite versions the project's CI runs. If the value differs on another version, every open of a store refuses with exit code 8 (a refusal, never a silent pass). The continuous integration runs on several Python versions, each with its own SQLite build, and is the portability check: a fingerprint mismatch on any CI job is a finding to report, not to work around. The `graph_changed` shard trigger uses SQLite's JSON functions (the default from 3.38; usual in builds before that). Every open first runs a fixed read-only probe of `json_valid`, `json_type`, `json_array_length`, `json_extract` and `json_each`; a runtime without them is refused with exit code 8 (the unsupported-runtime code) before any schema is installed, instead of failing later with an internal error.

## Exit codes

| Condition | Code |
|---|---:|
| Lock wait ran out (`SQLITE_BUSY`, `SQLITE_LOCKED`, any extended code) | 13 `store busy` (new, `docs/exit-codes.md`) |
| Symlink, hard link, wrong owner or mode, wrong file type, inode mismatch | 5 |
| Store file missing | 9 |
| Version floor, a database SQLite reports as corrupt or unreadable, read-only directory, cannot open, disk or I/O error, wrong application id or version, fingerprint mismatch, a file that exists but is not a complete store (when not adopting), append-only or no-delete trigger, hash-chain, sequence or time break, a rollback that failed, an ambiguous commit, a runtime without the JSON functions | 8 |
| Illegal queue state change (trigger) | 4 |
| Lease, claim generation or apply-slot refusal (trigger) | 12 |
| Constraint violation (`CHECK`, `NOT NULL`, `UNIQUE`, primary key) | 3 |
| Foreign key violation | 9 |
| Bad argument to `connect` | 2 |
| Any other SQLite error | 1 |

Messages are the fixed text of `docs/exit-codes.md`. They never carry a path or SQL text, and the errors raised by this module do not chain the original exception.

## Residual risks

- A complete older or replaced database that has the right schema, application id and version opens normally, and its hash chains roll back with it consistently. The fingerprint authenticates the shape of the schema, not the identity or freshness of the data. This is the same whole-store restore limit as the file queue (`docs/threat-model.md`). **Mitigated by the audit export** (issue 63, `docs/audit-export.md`): the adapters keep the tip of each hash chain in a separate file after each commit and refuse, with exit code 8, a database that is older than that export or whose chain differs from it, before a write extends it (`shit check` reports the same). A restore of the database together with the export, or of the whole `.shit/` directory, is still not detected, and `store.connect` itself does not compare (a caller that bypasses the adapters is not protected).
- A damaged database page that the open-time reads do not touch is not found at open (see step 5 above).
- A process of the same user can open the file with any SQLite client and bypass every control above, including the triggers (it can also drop and recreate them; the next open of the store then refuses on the fingerprint).
- `PRAGMA journal_mode=WAL` writes the database header outside an explicit transaction, because SQLite does not allow the switch inside one. It runs only after the header checks pass.
- Rows are checked for what the database can express. Control and bidirectional characters in messages are still the job of the record validators (`shit/_text.py`) before a write.

## Tasks, events and the records index (issue 61)

`store.py` holds the task and record write helpers. `shit/store_tasks.py` supplies the task and record operations. Nothing reads or writes the removed `events.jsonl`, `events.chain`, or `pending.json` protocol.

| Helper | One `write_transaction` writes, in this order |
|---|---|
| `create_task` | the task row, the `task` index row (digest of the body file), the `task_created` event |
| `transition_task` | the task row (state, time, blocked fields), the `task_history` row, the `state_transition` event |
| `add_record` | the index row, then the event (`handoff` has no event kind, so only the row) |

Every check and every read happens after `BEGIN IMMEDIATE`: the task exists, a closed task takes no record, a duplicate is refused (exit code 2), the transition is legal for the actor, a model or wrapper cannot approve (10), time never goes back against the task or the event log tip (3). A `precheck` or `check` callback runs inside the transaction and may raise, so `close_task` decides on the same data it commits. Each event is the file protocol's: same fields in the same order, id `event-NNNN` from the sequence number, hash `sha256(previous hash, newline, line)`, so the two logs of the same script are equal byte for byte (a test runs both).

**Bodies.** A record body (task, evidence, report, review, decision) stays a file at its old path under `.shit/`. It is written to its final name before the transaction, by temp file and hard link, so it appears whole or not at all and nothing on disk is overwritten. No file lock is taken (the store's own transaction is the isolation, and the file protocol's lock discipline belongs to the file protocol). The one thing a lock would have done is clean up a `.tmp-` name that a crash left beside a finished body in the microseconds between the link and the removal: every read of that body then refuses (exit code 5, `body_unreadable` in `verify_bodies`) and that record id is unusable until the extra name is removed by hand; another id works. After the commit the adapter reads the file again and compares its SHA-256 and size with the index row; a difference is exit code 3 and the row stays (the index is the record of what was committed). Every read of a body checks the digest first. A task body is the task as created (title, summary); the state, owner, times and history come from the database, and `get_task` joins them and validates the result with the old validator.

**Crashes.** A crash before the transaction leaves a body file with no index row. A retry adopts it only when its complete bytes have the same digest and size as the content submitted by that retry; different content is refused with exit code 3 and the orphan is left untouched. A crash inside the transaction leaves nothing (SQLite rolls it back). A crash after the commit and before the check leaves a complete record. An orphan body is never used by a reader, because every read goes through the index, and `shit check` reports it as `body_orphan`.

**Reads.** One statement can be read alone. An operation that reads several rows (`get_task` reads the row and the history) runs in `store.read_snapshot`, a deferred `BEGIN` that ends in `ROLLBACK`, so every read sees one commit. It is one of the functions that own their transaction in the lint.

**Checks.** The tests call `store.verify(conn)` and `store.verify_tasks(conn)` after every operation. `verify_tasks` adds what `verify` leaves to the writers: `event_hash_wrong`, `event_invalid`, `task_differs_from_history` (state and time against the last history row, and the history chain from `open`), `history_differs_from_events`, `task_without_created_event`, `task_without_body_record`, `record_without_event`, `event_without_record`, `record_digest_differs_from_event`. `store_tasks.verify_bodies(project, conn)` reads indexed files and can perform a bounded, lstat-first directory scan: `body_missing`, `body_unreadable`, `body_differs_from_index`, `body_orphan`, and `body_unexpected_name`.

**Lint.** The new functions are on the allowlist of `tests/test_store_lint.py` on purpose, each with `execute` only: the reads, `read_snapshot` (a transaction owner) and the three write helpers. A test proves each write helper holds exactly one `write_transaction` with all its writes inside, that the read helpers hold no write, and that being on the list does not allow a write outside a transaction, text-built SQL or a look-alike `read_snapshot`.

`transition_task` refuses a change the queue state does not allow. A body file that no index row names is left in place; there is no sweep. `precheck` receives the task row, not the body (no title or summary).

## Dependency graph (issue 62)

`store.add_edge`, `store.add_edges`, `store.remove_edge` and the reads `store.list_edges`, `store.needs_of` and `store.prerequisite_closure` are the database version of `shit/dependencies.py` (which stays until the file queue is removed). An edge `(task_id, needs_id, type)` means `task_id` needs `needs_id`. Each add (one edge or a batch) and each removal is one `write_transaction`, so a refusal leaves nothing behind. The reads are single `SELECT` statements.

The order of the checks, and the exit codes, are those of `dependencies.add_edges`: each edge's shape (two slugs and one of the four types, else 3) and then its tasks (an unknown task is 9), in the order given; then duplicates (3); then the caps (7): 200 edges per dependent task and 2,000 dependent tasks with edges in all (`dependencies.MAX_NEEDS_PER_TASK`, `MAX_TASKS`), counting a self edge as the file version does; then a self edge or a cycle (3). The cycle check runs inside the transaction: with the new edges inserted, a recursive query (`UNION`, so a diamond or a long chain terminates and is walked once) asks whether any new edge's prerequisite reaches its dependent; if so the transaction is rolled back. `remove_edge` refuses a bad shape (3) and an edge that is not there, matched on all three columns (9), and then, as `queue.remove_edge` does, a dependent that holds a live lease row (4). No SQL text is built from an argument: every statement is a literal with `?` parameters, and each function that runs one is on the exact allowlist of `tests/test_store_lint.py`.

`edges` now has the primary key `(task_id, needs_id, type)`, as the file graph allows the same pair with more than one type (a `draft` and an `apply` edge between the same two tasks). The pinned schema fingerprint changed with it.

What the database does not do: the caps are enforced by the helpers only (a hand written `INSERT` can exceed them; the schema keeps the foreign keys, the type list and the no-self-edge check), and a cycle can likewise be written by hand. `store.verify` does not look for cycles. The queue population cap (queued tasks and shard owners together stay within 2,000, `queue._check_population`) belongs to the enqueue operation, not to the graph helpers. The `graph_changed` queue entry that the file queue writes for each change is the job of the queue operations (C3, C4): this module changes the table only. `tests/test_store_graph.py` replays random operation sequences, including hostile and oversized ids, through both implementations and requires the same exit code, the same edges and the same closure after every operation.

## Queue operations (issue 61, C4)

The queue helpers in `shit/store.py` each use one `write_transaction`; `shit/store_queue.py` is their project-level adapter. Nothing reads or writes the removed `queue.jsonl`, `queue.chain`, `leases/`, `deps/`, `queue-anchor.json`, or `queue-pending.json` protocol; the SQLite transaction is the commit point.

**Queue behavior.** After BEGIN IMMEDIATE every operation checks the clock against the queue tip and the event tip (exit code 8), performs maintenance (below), applies its own checks (the presented lease: exit code 12; a state the transition table forbids: 4; an unmet edge at submit: 9; and so on), then writes. Every candidate entry is also run through `queue_records.replay` with the entries of its task before it is written, so the database never holds a log replay would refuse. `tests/test_store_queue.py` covers the low-level operations and invariants, `tests/test_store_queue_adapter.py` covers the project-level adapter and record-dependent rules, and `tests/test_store_queue_safety.py` covers transaction fault points and races.

**Maintenance in the same transaction.** Every operation first expires every lease that is due (or has no row) and promotes every waiting task whose edges are met, as `queue._maintain` does, inside its own transaction. An expired `running` lease writes the `expired` entry and the `running` to `failed` lifecycle step; a `dispatched` one returns to `ready`. If the operation is then refused, the whole transaction rolls back, maintenance included (the file queue kept it): the next operation redoes it, so the log differs only by when the expiry was written, never by what it says. `queue_maintain` is the operation that applies maintenance alone. Whether an edge's milestone is reached (a success report, a pass review by a human, an accepted human decision) needs the record bodies, which are files: the caller passes `met(edge)`, and `store_queue.py` builds it from `store_tasks` (each body checked against its index digest). With no `met`, only an `apply` edge counts as met.

| Operation | One `write_transaction` writes, in this order |
|---|---|
| `queue_claim` | the `claimed` entry, then the lease row (the generation is the previous one plus one; the lease id is generated here, never supplied) |
| `queue_start` | the `started` entry, the task row (open to running), its history row and the `state_transition` event |
| `queue_renew` | the `renewed` entry, then the update of the lease row's expiry and renewal count |
| `queue_release` | the `released` entry, the apply slots of the task (none for a `dispatched` lease), the lease row |
| `queue_submit`, `queue_fail`, `queue_hold` | the entry, the task row, history row and event (running to review, to failed, to blocked with its reason and item), the apply slots, the lease row |
| `queue_reclaim`, expiry | the `expired` entry, the lifecycle step when the lease was `running` (it ends `failed`), the apply slots, the lease row |
| `queue_reopen`, `queue_complete` | the entry, the task row, history row and event (review, a hold or failed to open; approved to closed) |
| `queue_apply_start` | the `apply_started` entry, then one apply slot per canonical target key (all or none: an equal, ancestor or descendant held target is exit code 4 and nothing is written) |
| `queue_apply_finish` | the `apply_finished` entry, then the slots released |
| `queue_add_edges`, `queue_remove_edge` | the edge rows, then one `graph_changed` entry carrying the SHA-256 of each changed dependent's shard (the bytes `dependencies.dump_shard` would write; 64 zeros for a dependent left with none) |
| `queue_enqueue`, promotion | the `enqueued` or `ready` entry |

The order is the one of "Invariants across statements": the entry before the lease row, the slot after the lease row and deleted before it, the renewal's entry and row in one transaction, the task row's lifecycle state with the entry (`task_state_differs_from_queue` cannot occur through these helpers). `store._fault(stage)` is called after each write (as in C3); `tests/test_store_queue_safety.py` kills a child process at every numbered stage of every operation and requires the database unchanged and consistent each time, and the next write accepted.

**A queued task changes lifecycle state only through its queue operation.** `transition_task` now reads the task's latest queue state after BEGIN IMMEDIATE and refuses (exit code 4) any change to a state that queue state does not allow (`queue_records.LIFECYCLE`): a task with a live lease (`dispatched`, `running`) cannot be moved by hand at all, and neither can a task in `ready`, a hold, `failed` or `done` leave its lifecycle state. `review` to `approved` by a human is the one move that stays free, and `queue_complete` closes it. The check and the entry that depends on it are serialized by the same write lock. A task that was never enqueued is not restricted.

**Ambiguous commit.** `AmbiguousCommit` means the write may or may not be durable. Every operation takes an optional `key` (a slug); the entry the operation writes carries it in its message (`Queue entry recorded; key <key>.`). A repeat with the same key and the same task and kind finds that entry inside its transaction and returns the first answer (the lease record of a claim or renewal as that entry left it; nothing for the others) without writing; a key that names a different task or kind is exit code 3. If the commit did not land, the repeat writes once. The reconciling read is `store.queue_entry_for_key(conn, key)`. `store_queue.py` generates a key per call and repeats up to three times on a new connection before raising. A call made without a key is not idempotent: repeating it after an ambiguous commit finds the task already moved and is refused (exit code 4), which is safe but gives no answer. A key is not a secret and is not authenticated: any process of the same user can write one.

**Lint.** `tests/test_store_lint.py` lists each new function on the allowlist on purpose. The functions that write without opening a transaction (`_append_entry`, `_lifecycle_step`, `_insert_lease`, `_renew_lease_row`, `_drop_lease`, `_take_slot`, `_release_slots`, and the callers `_expire`, `_maintain`, `_begin`) are on a second list, `IN_TRANSACTION`: every call to one must sit lexically inside `with write_transaction(...)` (the real helper, not a shadowing look-alike) or inside another of them, a reference without a call (an alias, a callback) is a finding, each is a unique top-level definition, and each refuses at run time (exit code 1) when the connection is not in a transaction. Being on the list lets a function write outside a lexical `with`; it still has to be on the exact `ALLOWED_METHODS` list to run a statement, and the statement is still a literal. Every public operation holds exactly one `write_transaction`.

**Not done here.** `dispatch_failed` and its backoff (Q-RET-04, Q-RET-05; `queue.py` writes none either), an unleased `fail` or `hold` by a human (the table allows it; `queue.py` has none), capability tags and capacity (Q-CLAIM-04 second half, Q-CLAIM-06), `base_sha256` and apply priority (Q-SER-04 and Q-SER-06), the `QUEUE.md` view, parallelism metrics, `orphan_removed` (a database has no stray lease file), and removal of the file queue. Apply target keys are lexical: absolute paths, empty, dot and parent components, control and format characters, backslashes and drive prefixes are refused; accepted components are NFC-normalized and case-folded. Equal keys and ancestor/descendant keys overlap. Before a slot is taken, every existing component is walked from the repository directory descriptor without following links: symlinks, non-directories in directory positions, special final entries and multiply linked final files are exit code 5; a non-existing tail is allowed. The slot is advisory coordination between cooperating workers. Validation is point-in-time at slot acquisition, not a retained descriptor or other binding to the later filesystem write. A same-user process can replace a checked component after validation and before or during that later write; this documented race is outside the slot's protection. A key is looked up by scanning the log for its message (no index): the log is small; an index is a later schema change.

## Concurrency and crash harness (issue 65, C8)

Test infrastructure for the queue operations above. It lives in `tests/` and changes nothing in `shit/`. The C3 and C4 tests kill one child at each numbered fault point and race two claimants; this harness adds many processes, kills at random times, a model to compare with, and proof that it notices breakage.

| File | What it is |
|---|---|
| `tests/store_worker.py` | One worker process: random queue operations on a shared database, a journal of every won claim and every refusal a healthy run never gives, a snapshot check of every invariant every few operations |
| `tests/store_harness.py` | The drivers (`run_workers`, `kill_rounds`, `stage_sweep`), `check_invariants`, the mutations and `mutated_root` |
| `tests/store_model.py` | A pure model of the queue lifecycle and the generator of operation sequences |
| `tests/test_store_harness.py` | The tests that use all of it, and the negative cases |

**Run it.** `python3 -m unittest discover -s tests -p test_store_harness.py` (the default size takes about 30 seconds and is part of the full discover). Long mode, which is opt-in and takes about ten minutes: `SHIT_HARNESS_LONG=1 python3 -m unittest discover -s tests -p test_store_harness.py`. Real databases in temporary directories; nothing about SQLite is mocked.

| Variable | Default (long) | Meaning |
|---|---|---|
| `SHIT_HARNESS_SEED` | 1729 (random) | Seed of every random choice. Default mutation and harness runs are repeatable; long mode chooses a random seed. A failure prints it; set it to repeat the operation stream of each worker and the kill schedule |
| `SHIT_HARNESS_WORKERS` | 3 (6) | Worker processes |
| `SHIT_HARNESS_OPS` | 80 (1500) | Operations per worker in the concurrency run (it also stops at `SHIT_HARNESS_SECONDS`) |
| `SHIT_HARNESS_SECONDS` | 20 (120) | Time limit of the concurrency run |
| `SHIT_HARNESS_ROUNDS` | 4 (25) | Rounds of the kill driver |
| `SHIT_HARNESS_ROUND_SECONDS` | 1.5 (4) | Length of one kill round; the kills fall inside it |
| `SHIT_HARNESS_SCALE` | 30 | Logical seconds per real second. At 30 the shortest lease (60 logical seconds) lasts two real seconds, so expiry and reclaim happen during a run |
| `SHIT_HARNESS_BUSY_MS` | 30000 | Busy timeout the workers pass to `connect` |
| `SHIT_HARNESS_MODEL_SEEDS` | 3 (40) | Number of generated sequences (seeds 1 to N) compared with the model |
| `SHIT_HARNESS_MODEL_LENGTH` | 120 (300) | Operations per generated sequence |
| `SHIT_HARNESS_LONG` | unset | `1` selects the long defaults above |

**What it does.**

1. *Concurrency run.* The workers perform random operations (new tasks, enqueue, claim, start, renew, release, submit, fail, hold, reopen, approve, complete, reclaim, maintain, apply start and finish, edges) on one database, mostly the operation a task's state invites and sometimes another, with valid, stale and invented leases. They share a logical clock (real elapsed time times `SHIT_HARNESS_SCALE`), so leases expire. After it, nothing may have been busy (exit code 13 with a 30 second timeout means a lock is not taken as the design says), no worker may have died with an exception that is not a `ShitError`, and no snapshot check may have found anything.
2. *Kill driver.* Rounds of workers; this process sends SIGKILL to random workers at random times. A kill is not aimed at COMMIT, but with `synchronous=FULL` most of the time of an operation is the commit, so a uniform random time lands inside it often. Odd rounds also make each worker kill itself at a random fault point of the operation it is running. After every kill, `verify` and `verify_tasks` run on one read snapshot while the survivors keep writing; after every round the database is checked at rest, and a write must go through at once (a dead process holds no lock). `stage_sweep` is the deterministic part: a claim, a renewal and a submit, each killed at every fault point it reaches, with the invariants checked after each.
3. *Lifecycle model.* A pure state machine written from `docs/queue-spec.md` (not from the store) says, for each generated operation, whether the store must accept it, with which exit code, and what every task, lease and apply slot must look like afterwards, including lazy expiry (a refused operation keeps no expiry, because it rolls back) and the attempt limit. After every operation the store must agree, and at the end the model must agree with `queue_records.replay` of the log.
4. *Mutation checks.* A copy of the package is made in a temporary directory, one text of `shit/store.py` is replaced (it must match exactly once), and the named detector must report the expected fixed guard signal shown below. A control copy with no change must report nothing. The transaction-mode and timeout mutations use explicit barriers tied to the affected connection and operation; the other mutation detectors exercise generated or killed operations and therefore remain partly probabilistic. A signal from those randomized detectors identifies an observed invariant failure, not a causal attribution to one schedule step.

| Mutation | Noticed by |
|---|---|
| `BEGIN IMMEDIATE` replaced by a deferred `BEGIN` | a barrier commits a competing writer after the mutated transaction's read, then observes that transaction's busy-snapshot refusal (13) |
| the busy timeout ignored (0) | a barrier holds the write lock and observes that the mutated operation refuses (13) before the configured timeout while the control waits and succeeds |
| the lease row never written after a claim | the concurrency run: the snapshot check finds `claim_without_lease` |
| the lease row deleted before its apply slots (the order the trigger forbids) | the model: an accepted submit or hold is refused |
| a COMMIT between the claim's entry and its lease row | the sweep: a kill after the entry leaves `claim_without_lease` |

**Invariants checked** (`check_invariants`): `verify` (deep) and `verify_tasks` are empty; `queue_records.replay` accepts the whole log; the queue hash chain is contiguous (numbers, previous hashes) and every queue-entry hash is recomputed from its canonical line; one winner per claim (the generations of a task are 1, 2, 3 ..., no lease id repeats, every claim a worker was told it won is in the log exactly once, no two workers were told the same generation); a lease row exists exactly when the replayed log says a lease is live, with the same lease id.

**What it proves, and its limits.**

- It shows that the invariants hold across random interleavings and random kills of the operations as implemented, and that the checks notice the listed breakages. It does not prove the absence of a bug: a random run covers what it happens to reach, and the default is small on purpose.
- The interleaving of processes is the operating system's. A seed repeats the operations of each worker and the kill schedule, not the order in which the processes meet.
- A SIGKILL ends a process; the operating system, and the file system, stay up. Power loss, a torn sector, a lost fsync or a full disk are not simulated, so what `synchronous=FULL` buys is trusted here, not tested.
- A kill by a timer lands near COMMIT often but not exactly there; the deterministic sweep covers the fault points between statements, and SQLite's own atomic commit covers the rest.
- Workers and the checks use one database file on one local file system. Network and shared file systems are not covered.
- The queue hash chain's digests are recomputed from the canonical stored-entry representation; the events chain's digests are recomputed by `verify_tasks`.
- The model covers operations with no dependency edges and no decisions or records. Edge and record-dependent behavior is covered by `tests/test_store_queue.py`, `tests/test_store_queue_adapter.py`, and the concurrency harness; there is no claim of differential parity with the removed file queue.
- A worker that sees exit code 8 (a stale clock reading against a newer entry, or an ambiguous commit) opens a new connection and goes on: with a shared logical clock a few operations are refused this way in every run, which is the designed fail-closed answer, not a finding.

## Audit export (issue 63, C6)

`shit/audit_export.py` writes `.shit/audit-export.txt`, a deterministic text copy of both hash chains (every line the hash was computed over, the chain hashes and the tips), atomically with mode 0600, after each adapter write commits. The adapters make it current before they write, refuse a restored database and refuse to write without an export; `shit audit verify` checks an export offline; `shit check` reports the queue and the comparison read-only. The store side is a delimited section at the end of `shit/store.py` ("C6"): `audit_tips`, `audit_hash_at` and `audit_chains` are reads only, on the lint allowlist one by one (`tests/test_store_lint.py`, `AuditExportLintTests`), and `audit_chains` rebuilds each line and recomputes its hash, so an entry that cannot be reproduced stops the export with exit code 8. Format, states, crash behavior and limits: `docs/audit-export.md`.

## Not built yet

The CLI switch and removal of the file queue (C7).
