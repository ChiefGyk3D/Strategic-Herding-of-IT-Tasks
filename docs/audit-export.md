# Audit export and offline verifier (issue 63, epic 57)

**SELF-REVIEW:** the implementer's own account. A human peer review is still required.

The SQLite store (`docs/store.md`) is the authority. The audit export is a second copy of its two hash chains, the event log and the queue log, written to a separate file after each write commits. It has two jobs:

- **Detect a whole-store restore.** A database that is older than its export, or whose chain differs from the export's, is refused before a write extends it. This is the mitigation that `docs/store.md` named for the restore residual risk.
- **Verify offline.** `shit audit verify` reads the export alone and checks the chains, every entry hash and the replay rules, without the database.

**Scope of the protection.** As for the store and the queue: crashes, races, mistakes and an older copy of the database file restored by itself. A process of the same user that can write `.shit/` can rewrite the database and the export together, or restore the whole directory, and nothing here detects that (see "Residual risks"). The export is not signed and not authenticated.

## File

`.shit/audit-export.txt`, ASCII, LF, mode 0600 (a mode with a bit outside 0600, a symlink, a hard link, a file of another owner or a non-regular file is refused with exit code 5). It is rewritten whole by temporary file, `fsync`, rename and a directory `fsync` (`Project.write_atomic`), so a reader sees the old file or the new one. Its name is `audit-export.txt` and its location is fixed (a configurable location, for example another filesystem, is deferred).

```
SHIT-AUDIT-EXPORT 1
E <seq> <hash> <event line>
...
Q <seq> <hash> <queue entry line>
...
TIP events <seq> <hash> queue <seq> <hash> sha256 <hex>
```

- `E` lines are the event log, oldest first; `Q` lines the queue log, oldest first; every `E` line comes before every `Q` line. `<seq>` is the row's sequence number (1, 2, 3, no gaps) and `<hash>` its chain hash.
- `<event line>` and `<queue entry line>` are the exact compact JSON lines each hash was computed over: `hash = sha256(previous hash, newline, line)`, the first previous hash being 64 zeros. A line holds no newline (JSON escapes it), so the record is one line. The same functions that wrote the lines read them back (`records.load_event`, `queue_records.load_entry`).
- The last line is the footer: the tip (sequence number and hash) of each chain, and the SHA-256 of every byte before the footer. An empty chain has tip `0` and the genesis hash.
- The same database state always gives the same bytes (nothing in the file depends on time or the process).

**Why the database can reproduce the line.** The queue's key order is not a column. Each operation builds its entry in a fixed order, and `store._LINE_ORDER` writes that order down per kind. The export rebuilds each line from the row, recomputes the hash and compares it with the stored one; a difference is exit code 8 at export time. An entry kind without an order (`dispatch_failed`, `orphan_removed`: nothing writes them yet), an unnamed column or a changed field order therefore stops the export instead of writing a line nobody can check. `tests/test_audit_export.py` runs every operation that writes an entry and exports the result.

## Where the tip is stored and when it is compared

The tip is the footer of the export file itself, outside the database file. Readers may obtain it from a bounded read of the last 1,024 bytes. Before a writer accepts an export as current, it reads the complete size-bounded file, verifies the footer body digest and both chains, and compares the verified tips with the database. An export is written after its commit, so a database read later always holds everything the export holds.

| State | Meaning | Reader (`get_task`, `show`, `entry_for_key`) | Writer (every operation that writes) | `shit check` |
|---|---|---|---|---|
| `current` | export tips are the database tips | proceeds | proceeds | ok |
| `behind` | the export is a prefix of the database: a commit whose export was not made (a crash, or a failed write) | proceeds | **repairs it first** (writes the export), then writes | warning line, exit 0 |
| `none` | no export and no entries | proceeds | writes the first (empty) export, then writes | ok |
| `missing` | no export file but the database has entries | proceeds (no proof of a restore) | **refuses, exit code 8** | failure, exit 8 |
| `database_older` | the database has fewer entries than its export | **refuses, exit code 8** | **refuses, exit code 8** | failure, exit 8 |
| `diverged` | the database has a different hash where the export has one | **refuses, exit code 8** | **refuses, exit code 8** | failure, exit 8 |

A footer that cannot be parsed is exit code 8 for readers and writers alike; an unsafe file (mode, link, type) is exit code 5; a file over `MAX_EXPORT_BYTES` (128 MiB) is exit code 7. Nothing is repaired by `shit check`, and nothing replaces an export that is `missing`, `database_older`, `diverged` or unreadable except the operator: `shit audit export --accept-database` is "the database in front of me is the truth, replace the export". The old export is not kept; copy it first if it is evidence.

This wiring is in the adapters (`store_tasks._connect`, `store_queue._run`), which are the path the CLI will use (C7). `store.connect` itself does not check the export, so code that opens the database directly and writes through the `store.*` helpers (tests, repair tools) writes no export; the next adapter writer finds the export `behind`, or, with no export at all, `missing`.

## Writing

Each adapter write is: open, then `audit_export.sync` (a writer makes the export current first, or refuses), then the `store` write helper (one transaction, unchanged), then `audit_export.after_commit`. `sync` takes the database's write lock (`BEGIN IMMEDIATE`, nothing written) while it reads both chains in one snapshot and replaces the file, so two writers cannot make the file go back to an older state. The export is written after the commit, never inside it, so the one-transaction rule of the helpers is untouched.

`after_commit` is best effort: if the export cannot be written (the disk, a busy store, a size limit), the committed write stands and is not reported as an error, because the database is the authority and a missed export is detected and repaired at the next write. The cost is that a persistent export failure is visible only as `behind` in `shit check` and as the next writer's refusal (a size or disk error raised by its own repair). An interrupt is never swallowed.

**Crashes** (`tests/test_audit_export.py`, a child process killed with SIGKILL):

| Killed | State afterwards | Next open |
|---|---|---|
| after the commit, before the export is written | the write is in the database; the old export is intact | `behind`: readers proceed, the next writer repairs |
| inside the file replacement, before the rename | the old export is intact; a `.tmp-` file is left in `.shit/` | `behind`, as above (the leftover is ignored) |
| after the rename | current | current |

**Cost.** The file is rewritten whole, so a write costs time and I/O proportional to the log. The 128 MiB cap bounds it. An incremental, append-only export is deferred.

## Offline verifier

`audit_export.verify_bytes(data)` takes the bytes of an export and returns the tips and the body digest, or raises. It checks, in this order of work: size, ASCII, the magic line, the footer and the body digest it records, then for each chain the sequence numbers, the recomputed hash of every line, the record itself (validated by the code that wrote it, and re-dumped to the same bytes), the id that follows the sequence (`event-0001`, `queue-0001`), and the replay rules: `EventLog.verify_lines` (unique ids, timestamps never going back, every transition continuing the task's history) for events and `queue_records.replay` (the whole queue state machine, one task at a time) for the queue. It also requires every task named by the queue chain to have a no-later creation event in the event chain, and reconciles queue-implied lifecycle effects with matching task, transition, actor and timestamp in event-chain order. A lifecycle transition for a queued task that is implied by queue state must have the matching queue effect in the same order; the independent human `review` to `approved` step is allowed. Finally the footer must name the tips the body has. A failure is exit code 8; over the size limit is 7. Nothing in the output names a record, a path or the failing line.

The export is a plain file, so a copy kept off the machine verifies by itself: no database and no repository are needed (the module imports the store, so the Python `sqlite3` module must be importable, but it opens no database).

## Commands

`python3 -m shit.audit` (the shared entry point calls `audit.register(subparsers)`, as for `check`):

| Command | Effect | Exit codes |
|---|---|---|
| `audit export [--repo PATH] [--accept-database]` | make the export equal to the database: writes if it is `behind` or `none`, reports `already current`; refuses (8) `missing`, `database_older`, `diverged` or an unreadable export unless `--accept-database` | 0, 2, 5, 7, 8, 9 (no store) |
| `audit verify [--repo PATH]` | verify the project's export offline | 0, 2, 5, 7, 8, 9 (no export) |
| `audit verify --file PATH` | verify any copy of an export; no repository needed | 0, 2, 5, 7, 8, 9 (no file) |
| `audit verify [--file PATH] --database` | also compare the export with the database, read-only; a database ahead of the export is reported and is exit 0; `database_older`, `diverged` and `missing` are exit 8 | as above |

Output is fixed text: `audit-export: ok (events N, queue M)`, then `tip events N <hash>` and `tip queue M <hash>` (the tips are hashes, safe to copy to a second place, which is what makes a later restore of the whole directory detectable), then, with `--database`, one `database: ...` line.

## `shit check`

`shit check` adds a store section when `.shit/store.sqlite3` exists (nothing when it does not). It is read-only: it writes no row, no export and no file (opening the database may create its `-wal` and `-shm` files, as any open does; a test compares every table, the export's bytes and mtime and the files). It reports:

- `store: queue N tasks (state count, ...)`, `store: pending P, leases L live, E past expiry` (`P` is the waiting and ready tasks; a lease is past expiry when its task is `dispatched` or `running` and the lease's expiry is not after the clock; nothing is expired by the check);
- every invariant that `store.verify(conn, deep=True)` (including `PRAGMA integrity_check`) and `store.verify_tasks` name, as `store: invariant <name> does not hold` (exit 8);
- missing, unreadable, multiply linked or digest-mismatched record bodies, reported by fixed problem names without body content (the store finding is exit 8; the check's existing priority may select exit 3 or 5 when its file-backend scan reports the same damaged path);
- the export: `store: audit export verified (events N, queue M)` (the whole file is verified, not only its footer), a warning when it is `behind`, and a failure for `missing`, `database_older`, `diverged`, a damaged or unreadable export (8 or 5), or a database that cannot be opened (the exit code of the refusal; a busy store is 13).

The exit code is the check's usual priority (`docs/exit-codes.md`).

## Residual risks

- A process of the same user that can write `.shit/` can restore the database and the export together (or the whole directory) and nothing detects it; the same process can edit the export and re-chain it, because the export is not signed. The mitigation is to copy the printed tips (or the export) somewhere a restore does not reach, and compare them with `audit verify --file ... --database`. An external anchor (a configurable second location, signing) is deferred.
- Truncating the tail of the export and writing a matching footer verifies on its own; it is found by the comparison with the database (`database_older` cannot happen for a shorter export; the export would then be `behind` and the next writer would repair it), so a shortened export hides nothing from the database, but it does not prove the database was not also rolled back. Only a copy kept elsewhere does.
- A readers' open does not refuse a missing export (it is not proof). A writer does.
- The `behind` state is not an error and `after_commit` hides export failures from the caller (see "Writing").
- The export is rewritten whole on every write (cost grows with the log) and a `.tmp-` file left by a crash in the file replacement is not swept.
- `store._LINE_ORDER` must be extended in the same change that adds a queue entry kind or reorders its fields; the export refuses until then.
- The export's location and name are fixed.
