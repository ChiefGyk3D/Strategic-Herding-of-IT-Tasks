# Bounded work

Every configured scan and confined directory listing has a ceiling, and a ceiling that trips fails closed with exit code 7 (`input_limit`). The skipped work is never reported as clean. These bounds apply to record bodies and repository files; queue state is read from SQLite.

| Bound | Where | Value | On trip |
|---|---|---|---|
| Directory width | privacy scan, `MAX_DIR_ENTRIES` | 5,000 names | that directory is not scanned, exit 7 |
| Cumulative bytes | privacy scan, `MAX_TOTAL_BYTES` | 256 MiB of file sizes | later files are not read, exit 7 |
| Scan deadline | privacy scan, `MAX_SECONDS` | 120 s wall clock | the rest of the walk is skipped, exit 7 |
| File size, file count, depth | privacy scan | 4 MiB, 20,000, 32 | exit 7 (unchanged) |
| Directory width | store listings (`Project.list_dir`, `publication_pairs`, the `check` task listing), `project.MAX_DIR_ENTRIES` | 10,000 names | exit 7 |
| Lock hold | `Project.locked(hold=...)`, `project.MAX_HOLD_SECONDS` | 60 s | every confined store operation after the deadline raises exit 7 |

## How the listings stay bounded

A listing uses `os.scandir` on the directory descriptor and stops at the first name past the limit, so a hostile wide directory costs the limit plus one entry, not its full size. Hits found before a bound trips are still reported, and the exit code is the highest-priority one (confinement, then input limit, then privacy hit).

## The lock deadline

While the store lock is held, each confined operation (`read`, `write_atomic`, listings, and so on) checks a monotonic deadline set when the lock was taken. Past it, the operation raises exit code 7 and the caller leaves the lock. A commit stopped this way is in the same state as a crash between two writes, which queue recovery already proves and repairs (`docs/queue-spec.md`, Q-REC-02). The deadline never extends: leaving the lock clears it.

The deadline is checked only at store operations. A pure in-memory loop that never touches the store is not interrupted; those loops are bounded by the record and task caps in `docs/queue.md`.

## Not covered

A file that grows between its size check and its read is still limited to `MAX_FILE_BYTES` by the read itself and by the scan deadline, but its growth is not added to the cumulative total.
