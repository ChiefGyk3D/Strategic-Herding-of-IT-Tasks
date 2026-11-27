# SQLite Store Foundation (issue 59, epic 57)

**SELF-REVIEW:** the implementer's own account. A human peer review is still required.

`shit/store.py` is the base of the SQLite store that is replacing the file-based queue (`docs/queue.md`). It holds no queue behavior yet. Operations are ported in later issues, and the file queue stays until they land. `shit/store.py` is the only module that imports `sqlite3`.

**Scope of the protection.** Like the queue, the store protects against crashes, races and mistakes of cooperating processes of the same operating-system user. A hostile process of that user can open the database directly and bypass every control here. The checks below catch accidents and a casual swap; they are not a boundary against that user.

## Opening the store

`store.init_store(project)` creates the database once. `store.connect(project, busy_timeout_ms=None)` is the only way to open it. Both refuse, in this order:

1. The floor: SQLite older than 3.37 (STRICT tables) or Python older than 3.11 is exit code 8.
2. The `.shit` directory: it must be owned by the effective user with no group or other bits.
3. The file, by `lstat` before opening: a regular file, not a symlink, link count 1, owned by the effective user, mode no wider than 0600 (exit code 5; a missing file is 9). The `-wal` and `-shm` files get the same check when they exist.
4. After opening, the device and inode of every descriptor the process holds on that path (read through `/proc/self/fd` on Linux) must be exactly the file that was checked (exit code 5). Without `/proc` the file is compared again by path, which is weaker.
5. The header and schema: `application_id` and `user_version` must match, and the schema fingerprint must match (exit code 8).

**Residual race window.** Python's `sqlite3` cannot pass `O_NOFOLLOW` to the open. Between the `lstat` and the open, a process that can write the `.shit` directory can replace the file. The inode comparison in step 4 detects that after the fact and the connection is closed, but sqlite has by then opened the replacement and read its header. Only a process of the same user can use this window, which is outside the scope above. The pre-created file in a 0700 directory is what keeps other users out.

Every connection sets, and reads back: `foreign_keys=ON`, `journal_mode=WAL` (refused unless the answer is `wal`), `synchronous=FULL`, `busy_timeout`, `trusted_schema=OFF`, the defensive flag (`SQLITE_DBCONFIG_DEFENSIVE`, where Python 3.12 or later has it), and `isolation_level=None`, so Python never opens a transaction on its own.

## Creation and modes

`init` runs under umask 077 (restored afterwards; the umask is process-wide, so call it from single-threaded setup), creates `store.sqlite3` with `O_EXCL` and mode 0600, and installs the schema in one `BEGIN IMMEDIATE` transaction, only into a brand-new empty database. SQLite gives `-wal` and `-shm` the mode of the main file, so all three are 0600. An existing store is verified, never rewritten.

## Writing

`store.write_transaction(conn)` is the only way to write. It runs `BEGIN IMMEDIATE` (never a deferred read that later upgrades, which fails with an unretried busy-snapshot error), commits on success, and rolls back on any exception, including `KeyboardInterrupt`. An SQLite error raised inside is mapped to a `ShitError` after the rollback. SQL text must be a literal with `?` parameters. `tests/test_store.py` checks every module under `shit/` for SQL built by f-string, `%`, `.format` or `+`; only the schema builder, which uses fixed vocabulary from this code, is exempt.

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

The `CHECK` lists for states, kinds, steps, worker kinds, hold kinds, dependency types, event kinds and legal lifecycle pairs are generated from `shit/lifecycle.py`, `shit/queue_records.py`, `shit/dependencies.py` and `shit/records.py`, so the database cannot drift from the code. The triggers raise `RAISE(ABORT, 'E<n>')` with only a token; `store.TOKEN_EXIT` maps each token to an exit code (all are 8 today).

**Schema fingerprint.** At every open, SHA-256 over the rows `(type, name, tbl_name, sql)` of `sqlite_master`, in a fixed order, is compared with the value the DDL in this code produces (computed on a scratch in-memory database). A dropped trigger, a trigger recreated with a different body, an altered or extra table, view or index, all refuse with exit code 8. A hand edit that keeps the exact text is not detected, and neither is a change made by a process that then restores it before the next open.

## Exit codes

| Condition | Code |
|---|---:|
| Lock wait ran out (`SQLITE_BUSY`, `SQLITE_LOCKED`, any extended code) | 13 `store busy` (new, `docs/exit-codes.md`) |
| Symlink, hard link, wrong owner or mode, wrong file type, inode mismatch | 5 |
| Store file missing | 9 |
| Version floor, corrupt or unreadable database, read-only directory, cannot open, disk or I/O error, wrong application id or version, fingerprint mismatch, append-only or no-delete trigger | 8 |
| Constraint violation (`CHECK`, `NOT NULL`, `UNIQUE`, primary key) | 3 |
| Foreign key violation | 9 |
| Bad argument to `connect` | 2 |
| Any other SQLite error | 1 |

Messages are the fixed text of `docs/exit-codes.md`. They never carry a path or SQL text.

## Not built yet

Queue operations, the event-log and task ports, the hash-chained export, apply-slot logic, and removal of the file queue.
