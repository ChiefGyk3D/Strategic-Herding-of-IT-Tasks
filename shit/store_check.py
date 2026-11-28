"""The queue and store section of `shit check`: read-only, from the database store (issue 63; docs/audit-export.md).

Reports what is pending, which leases are live or past their expiry, the invariants of `store.verify` (deep) and
`store.verify_tasks`, and how the audit export compares with the database. It never writes a row, a file or an export
(opening the database may create its `-wal` and `-shm` files, as any open does). A project with no store file has
nothing to report. Output is fixed text and names of invariants (fixed identifiers of this code), never record content.
"""

from datetime import UTC, datetime

from . import audit_export, file_backend_state, store, store_tasks
from .errors import FAILED_CLOSED, ShitError

_LIVE = ("dispatched", "running")
_NOW = "%Y-%m-%dT%H:%M:%SZ"
_FAILURES = {
    audit_export.MISSING: "audit export is missing but the database has entries",
    audit_export.OLDER: "database is older than its audit export (restored or truncated)",
    audit_export.DIVERGED: "database differs from its audit export (restored or edited)",
}


def _count(items):
    return ", ".join(f"{name} {number}" for name, number in sorted(items.items()))


def _queue_lines(conn, now):
    snapshot = store.queue_snapshot(conn)
    states, live, due = {}, 0, 0
    for item in snapshot.values():
        states[item["state"]] = states.get(item["state"], 0) + 1
        if item["lease"] is not None:
            live += 1
            if item["stored_state"] in _LIVE and item["lease"]["expires_at"] <= now:
                due += 1
    pending = sum(states.get(name, 0) for name in ("waiting", "ready"))
    lines = [f"store: queue {len(snapshot)} task{'s' if len(snapshot) != 1 else ''}"
             + (f" ({_count(states)})" if states else "")]
    lines.append(f"store: pending {pending}, leases {live} live, {due} past expiry")
    return lines


def diagnose(project, now=None):
    """([(exit code, text)], [summary lines], [warning lines]); all empty when the project has no store file."""
    findings, lines, warnings = [], [], []
    if not project.exists(store.DB_NAME):
        return findings, lines, warnings
    now = now or datetime.now(UTC).strftime(_NOW)
    try:
        footer = audit_export.read_footer(project)  # before the snapshot: an export never gets ahead of its database
        exported = audit_export.read_export(project) if footer is not None else None
        conn = store.connect(project)
    except ShitError as exc:
        return [(exc.code, "cannot be opened or its audit export cannot be read")], lines, warnings
    try:
        with store.read_snapshot(conn):
            if file_backend_state.present(project):
                findings.append((FAILED_CLOSED, "file-backend state is present beside the database"))
            for name in store.verify(conn, deep=True) + store.verify_tasks(conn):
                findings.append((FAILED_CLOSED, f"invariant {name} does not hold"))
            for name in store_tasks.verify_bodies(project, conn, include_orphans=True):
                findings.append((FAILED_CLOSED, f"record {name} does not verify"))
            try:
                lines.extend(_queue_lines(conn, now))
            except ShitError:
                findings.append((FAILED_CLOSED, "queue state does not verify"))
            found = audit_export.compare(footer, conn)
    except ShitError as exc:
        findings.append((exc.code, "cannot be read"))
        return findings, lines, warnings
    finally:
        conn.close()
    if found.state in _FAILURES:
        findings.append((FAILED_CLOSED, _FAILURES[found.state]))
    elif found.state == audit_export.BEHIND:
        warnings.append(f"warning: store: audit export is {found.events_behind} event(s) and {found.queue_behind} "
                        "queue entry(ies) behind the database; the next write repairs it")
    elif found.state == audit_export.NONE:
        lines.append("store: audit export not written yet (no entries)")
    if exported is not None:
        try:
            verified = audit_export.verify_bytes(exported)
        except ShitError as exc:
            findings.append((exc.code, "audit export does not verify"))
        else:
            if (verified.events, verified.queue) != (footer.events, footer.queue):
                findings.append((FAILED_CLOSED, "audit export changed while it was read"))
            elif found.state in (audit_export.CURRENT, audit_export.BEHIND):
                lines.append(f"store: audit export verified (events {verified.events.seq}, queue {verified.queue.seq})")
    return findings, lines, warnings
