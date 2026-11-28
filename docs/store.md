# SQLite Store Foundation (issue 59, epic 57)

**SELF-REVIEW:** the implementer's own account. A human peer review is still required.

`shit/store.py` is the base of the SQLite store that is replacing the file-based queue (`docs/queue.md`). It holds no queue behavior yet. Operations are ported in later issues, and the file queue stays until they land. `shit/store.py` is the only module that imports `sqlite3`.

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

`init` runs under umask 077 (restored afterwards; the umask is process-wide, so call it from single-threaded setup), creates `store.sqlite3` with `O_EXCL` and mode 0600, and installs the schema in one `BEGIN IMMEDIATE` transaction, only into a brand-new empty database. SQLite gives `-wal` and `-shm` the mode of the main file, so all three are 0600. A complete existing store is verified and never rewritten. A file that already exists and that this call did not create is refused (exit code 8) unless it is a complete store, so an empty or foreign file is never adopted by accident. `adopt_empty=True` is the explicit recovery for a crash between creating the file and installing the schema: it initializes only a brand-new empty database and still refuses anything with content. The schema install is one transaction. The arguments are validated before any file is created, and if anything fails after this call created the file, the file (and any SQLite side files its failed open left) is removed by name inside the held directory, but only while the name is still the file this call created; a file this call did not create is never removed. `adopt_empty` therefore matters only after a hard crash (power loss, kill) between creating the file and installing the schema.

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
| `edges` | Dependency edges, no self edge. |
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

- A complete older or replaced database that has the right schema, application id and version opens normally, and its hash chains roll back with it consistently, so a whole-store restore is undetectable. The fingerprint authenticates the shape of the schema, not the identity or freshness of the data. This is the same whole-store restore limit as the file queue (`docs/threat-model.md`). A later mitigation is to keep the tip of the exported audit chain outside the database and compare it at open; that is not built.
- A damaged database page that the open-time reads do not touch is not found at open (see step 5 above).
- A process of the same user can open the file with any SQLite client and bypass every control above, including the triggers (it can also drop and recreate them; the next open of the store then refuses on the fingerprint).
- `PRAGMA journal_mode=WAL` writes the database header outside an explicit transaction, because SQLite does not allow the switch inside one. It runs only after the header checks pass.
- Rows are checked for what the database can express. Control and bidirectional characters in messages are still the job of the record validators (`shit/_text.py`) before a write.

## Not built yet

Queue operations, the event-log and task ports, the hash-chained export, apply-slot logic, and removal of the file queue.
