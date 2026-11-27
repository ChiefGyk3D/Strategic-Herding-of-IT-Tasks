# SQLite Store Foundation (issue 59, epic 57)

**SELF-REVIEW:** the implementer's own account. A human peer review is still required.

`shit/store.py` is the base of the SQLite store that is replacing the file-based queue (`docs/queue.md`). It holds no queue behavior yet. Operations are ported in later issues, and the file queue stays until they land. `shit/store.py` is the only module that imports `sqlite3`.

**Scope of the protection.** Like the queue, the store protects against crashes, races and mistakes of cooperating processes of the same operating-system user. A hostile process of that user can open the database directly and bypass every control here. The checks below catch accidents and a casual swap; they are not a boundary against that user.

## Opening the store

`store.init_store(project)` creates the database once. `store.connect(project, busy_timeout_ms=None)` is the only way to open it. Both refuse, in this order:

1. The floor: SQLite older than 3.37 (STRICT tables) or Python older than 3.11 is exit code 8.
2. The `.shit` directory: it must be owned by the effective user with no group or other bits.
3. The file, by `lstat` before opening: a regular file, not a symlink, link count 1, owned by the effective user, and a mode that is exactly within owner read and write (the whole mode is compared, so setuid, setgid, sticky and owner-execute bits refuse as well as group and other bits; exit code 5; a missing file is 9). The `-wal` and `-shm` files get the same check when they exist, and the `.shit` directory must be within owner-only access with no special bits.
4. After opening, the device and inode of every descriptor the process holds on that path (read through `/proc/self/fd` on Linux) must be exactly the file that was checked (exit code 5). Without `/proc` the file is compared again by path, which is weaker.
5. The header and schema: `application_id` and `user_version` must match, and the schema fingerprint must match (exit code 8).

**Sidecar files.** After the open, each existing `-wal` and `-shm` is checked again with `lstat`, and must be the same file (device and inode) as in a snapshot taken just before the open. A change is retried once with a fresh snapshot (another process closing the last connection legitimately removes and recreates them) and refused with exit code 5 the second time. A sidecar that sqlite creates itself is checked for type, link count, owner and mode.

**Residual race window.** Python's `sqlite3` cannot pass `O_NOFOLLOW` to the open. Between the `lstat` and the open, a process that can write the `.shit` directory can replace the file. The inode comparison in step 4 detects that after the fact and the connection is closed, but sqlite has by then opened the replacement and read its header. Only a process of the same user can use this window, which is outside the scope above. The same window exists for `-wal` and `-shm`, and a replacement made after the post-open check is not seen by either check (a descriptor sqlite already holds keeps pointing at the old file). The pre-created file in a 0700 directory is what keeps other users out.

Every connection sets, and reads back: `foreign_keys=ON`, `journal_mode=WAL` (refused unless the answer is `wal`), `synchronous=FULL`, `busy_timeout`, `trusted_schema=OFF`, the defensive flag (`SQLITE_DBCONFIG_DEFENSIVE`, where Python 3.12 or later has it), and `isolation_level=None`, so Python never opens a transaction on its own.

## Creation and modes

`init` runs under umask 077 (restored afterwards; the umask is process-wide, so call it from single-threaded setup), creates `store.sqlite3` with `O_EXCL` and mode 0600, and installs the schema in one `BEGIN IMMEDIATE` transaction, only into a brand-new empty database. SQLite gives `-wal` and `-shm` the mode of the main file, so all three are 0600. An existing store is verified, never rewritten.

## Writing

`store.write_transaction(conn)` is the only way to write. It runs `BEGIN IMMEDIATE` (never a deferred read that later upgrades, which fails with an unretried busy-snapshot error), commits on success, and rolls back on any exception, including `KeyboardInterrupt`. An SQLite error raised inside is mapped to a `ShitError` after the rollback.

If the rollback itself fails, or the connection is still inside a transaction afterwards, the connection is closed (so it cannot be reused) and exit code 8 is raised with the fixed message. A failed commit whose rollback succeeds maps normally, for example to exit code 13, and leaves the connection usable.

`tests/test_store_lint.py` reads every module under `shit/` and enforces, by syntax tree:

- no SQL text built by f-string, `%`, `.format` or `+`; only the named schema builders, which use fixed vocabulary from this code and validate every word, are exempt;
- only `shit/store.py` imports `sqlite3`, and no module imports `importlib` or calls `__import__`;
- no module but the store calls `execute`, `executemany`, `executescript`, `cursor`, `commit`, `rollback` or another connection method;
- inside the store: no `executescript`, no cursor, the SQL passed to `execute` is a literal (or one of three named loop variables over fixed statements), and any statement that is not a plain read runs inside `with write_transaction(...)`, except in the four functions that own their transaction (`_open_once`, `_install_schema`, `write_transaction`, `expected_fingerprint`).

The lint is static. It does not stop a determined change to the lint itself; review does.

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

- Vocabulary lists (states, kinds, steps, worker kinds, hold kinds, dependency types, event kinds) and the legal lifecycle pairs, with the actors allowed for each pair (a model cannot approve).
- Timestamps must be real calendar times in the fixed `Z` form, not only the right shape. Event, history and queue messages are non-empty and at most 500 characters.
- Per queue-entry kind: the allowed actors and `state_after`, the columns the kind requires and the columns it forbids (from `queue_records.KINDS`), a `held` entry's state matching its hold kind, `held` and `failed` carrying a lease id and generation together, and a claim lifetime of at most the global ceiling.
- Triggers on `queue_entries` (before insert): a task is enqueued once and before anything else; the state change must be in the allowed table (`ALLOWED`, plus the re-entry pairs for a reopen, plus the two expiry pairs) and each kind must start from its own state; renewal and apply markers never change state; every lease-bearing entry names the task's latest claim; a claim's generation is the previous one plus one; a start's attempt is the previous one plus one.
- Triggers on `events` and `queue_entries`: the new row's `prev_hash` is the previous row's `hash` (64 zeros for the first) and the timestamp never goes back. The hash itself is computed by the later writer code, not checked here.
- Triggers on `leases` and `apply_slots`: a lease row equals the latest claim entry of the task (so write the claim entry first, then the lease); an update is exactly one renewal and changes no identity field; a lease with an apply slot cannot be deleted (delete the slot first); an apply slot needs a matching `apply`-step lease and is never edited.

Where a row breaks both a `CHECK` and a trigger, SQLite runs the trigger first, so the exit code is the trigger's.

The append-only and no-delete triggers raise `RAISE(ABORT, 'E<n>')` with only a token; `store.TOKEN_EXIT` maps each token to an exit code (8 for append-only and chain breaks, 4 for an illegal queue transition, 12 for lease and apply-slot refusals, 3 for a repeated enqueue or a wrong attempt number). Trigger conditions are written in this code from a small fixed alphabet; the builder refuses anything else.

**Schema fingerprint.** At every open, SHA-256 over the rows `(type, name, tbl_name, sql)` of `sqlite_master`, in a fixed order, is compared with the value the DDL in this code produces (computed on a scratch in-memory database). A dropped trigger, a trigger recreated with a different body, an altered or extra table, view or index, all refuse with exit code 8. A hand edit that keeps the exact text is not detected, and neither is a change made by a process that then restores it before the next open.

## Exit codes

| Condition | Code |
|---|---:|
| Lock wait ran out (`SQLITE_BUSY`, `SQLITE_LOCKED`, any extended code) | 13 `store busy` (new, `docs/exit-codes.md`) |
| Symlink, hard link, wrong owner or mode, wrong file type, inode mismatch | 5 |
| Store file missing | 9 |
| Version floor, corrupt or unreadable database, read-only directory, cannot open, disk or I/O error, wrong application id or version, fingerprint mismatch, append-only or no-delete trigger, hash-chain or time break, a rollback that failed | 8 |
| Illegal queue state change (trigger) | 4 |
| Lease, claim generation or apply-slot refusal (trigger) | 12 |
| Constraint violation (`CHECK`, `NOT NULL`, `UNIQUE`, primary key) | 3 |
| Foreign key violation | 9 |
| Bad argument to `connect` | 2 |
| Any other SQLite error | 1 |

Messages are the fixed text of `docs/exit-codes.md`. They never carry a path or SQL text.

## Residual risks

- A complete older or replaced database that has the right schema, application id and version opens normally. The fingerprint authenticates the shape of the schema, not the identity or freshness of the data. This is the same whole-store restore limit as the file queue (`docs/threat-model.md`). A later mitigation is to keep the tip of the exported audit chain outside the database and compare it at open; that is not built.
- A process of the same user can open the file with any SQLite client and bypass every control above, including the triggers (it can also drop and recreate them; the next open of the store then refuses on the fingerprint).
- `PRAGMA journal_mode=WAL` writes the database header outside an explicit transaction, because SQLite does not allow the switch inside one. It runs only after the header checks pass.
- Rows are checked for what the database can express. Control and bidirectional characters in messages are still the job of the record validators (`shit/_text.py`) before a write.

## Not built yet

Queue operations, the event-log and task ports, the hash-chained export, apply-slot logic, and removal of the file queue.
