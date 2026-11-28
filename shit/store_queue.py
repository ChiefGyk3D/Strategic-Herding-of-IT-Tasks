"""Queue operations on the database store, with the names and arguments of `shit/queue.py` (issue 61, C4).

This is the adapter layer for the port from the file queue: a caller that used `queue.enqueue`, `queue.claim` and the
rest finds the same name and arguments here and gets the same results and exit codes. The file queue is left in place
until the CLI moves over (C7); nothing here touches `queue.jsonl`, `queue.chain`, `leases/`, `deps/` or the anchor.

Each call opens the store, runs one `store.queue_*` helper (one write transaction) and closes the connection. The
helpers need two things only the files can answer, so this module supplies them as callbacks that run inside the
transaction: whether a prerequisite's milestone is reached (a report, a review or a decision), and, for `reopen` and
`complete`, the decision and closing rules. Both read record bodies through `store_tasks`, which checks every body
against its index digest.

**Ambiguous commit.** Every write carries a fresh idempotency key. If COMMIT fails in a way that leaves it unknown
whether the write landed (`store.AmbiguousCommit`; the connection is closed), the call opens a new connection and
repeats the same operation with the same key: a repeat finds the entry the first attempt wrote and returns its answer
without writing again, and a repeat of a commit that did not land writes once. After `_ATTEMPTS` the error is raised
to the caller, who can ask `store.queue_entry_for_key` (the reconciling read) with a new connection.
"""

import secrets

from . import audit_export, commands, store, store_tasks
from . import task as task_mod

_ATTEMPTS = 3
_now = commands._now


def _meter(project, conn):
    """`met(edge)`: is the prerequisite's milestone reached, read from verified records (as `queue._meter`)."""
    cache = {}

    def loaded(kind, task_id):
        key = (kind, task_id)
        if key not in cache:
            cache[key] = [store_tasks._load(project, conn, kind, rid, task_id)
                          for rid in store.record_ids(conn, kind, task_id)]
        return cache[key]

    def met(edge):
        pre, kind = edge["needs_id"], edge["type"]
        if kind == "draft":
            return any(r["outcome"] == "success" for r in loaded("report", pre))
        if kind == "review":
            reviews = loaded("review", pre)
            return bool(reviews) and reviews[-1]["verdict"] == "pass" and reviews[-1]["actor"] == "human"
        if kind == "decision":
            decisions = loaded("decision", pre)
            superseded = {d["supersedes_id"] for d in decisions if "supersedes_id" in d}
            effective = [d for d in decisions if d["id"] not in superseded and d["status"] in ("accepted", "rejected")]
            return bool(effective) and effective[-1]["status"] == "accepted" and effective[-1]["actor"] == "human"
        return True

    return met


def _decided_after(project, conn, task_id):
    """`decided(stamp)`: an accepted, effective human decision was recorded after `stamp` (Q-RET-03)."""
    def decided(stamp):
        decisions = [store_tasks._load(project, conn, "decision", rid, task_id)
                     for rid in store.record_ids(conn, "decision", task_id)]
        superseded = {d["supersedes_id"] for d in decisions if "supersedes_id" in d}
        return any(d["status"] == "accepted" and d["actor"] == "human" and d["id"] not in superseded
                   and (stamp is None or d["created_at"] > stamp) for d in decisions)

    return decided


def _run(project, call, now, config, busy_timeout_ms):
    """Run `call(conn, now, key, met)` on a fresh connection; repeat it with the same key after an ambiguous commit."""
    now = now or _now()
    key = secrets.token_hex(16)

    def run():
        for attempt in range(_ATTEMPTS):
            conn = store_tasks._connect(project, True, busy_timeout_ms, write=True)
            try:
                result = call(conn, now, key, _meter(project, conn))
                audit_export.after_commit(project, conn)
                return result
            except store.AmbiguousCommit:
                if attempt == _ATTEMPTS - 1:
                    raise
            finally:
                conn.close()

    return task_mod._guarded(run)


def enqueue(project, task_id, actor, *, now=None, config=None, busy_timeout_ms=None):
    return _run(project, lambda conn, at, key, met: store.queue_enqueue(conn, task_id, actor, at, config=config,
                                                                         met=met, key=key),
                now, config, busy_timeout_ms)


def claim(project, request, *, now=None, config=None, busy_timeout_ms=None):
    return _run(project, lambda conn, at, key, met: store.queue_claim(conn, request, at, config=config, met=met,
                                                                       key=key), now, config, busy_timeout_ms)


def start(project, presented, *, now=None, config=None, busy_timeout_ms=None):
    return _run(project, lambda conn, at, key, met: store.queue_start(conn, presented, at, config=config, met=met,
                                                                       key=key), now, config, busy_timeout_ms)


def renew(project, presented, *, now=None, config=None, busy_timeout_ms=None):
    return _run(project, lambda conn, at, key, met: store.queue_renew(conn, presented, at, config=config, met=met,
                                                                       key=key), now, config, busy_timeout_ms)


def release(project, presented, *, now=None, config=None, busy_timeout_ms=None):
    return _run(project, lambda conn, at, key, met: store.queue_release(conn, presented, at, config=config, met=met,
                                                                         key=key), now, config, busy_timeout_ms)


def submit(project, presented, *, now=None, config=None, busy_timeout_ms=None):
    return _run(project, lambda conn, at, key, met: store.queue_submit(conn, presented, at, config=config, met=met,
                                                                        key=key), now, config, busy_timeout_ms)


def fail(project, presented, *, now=None, config=None, busy_timeout_ms=None):
    return _run(project, lambda conn, at, key, met: store.queue_fail(conn, presented, at, config=config, met=met,
                                                                      key=key), now, config, busy_timeout_ms)


def hold(project, presented, hold_kind, blocked_reason, blocking_item, *, now=None, config=None,
         busy_timeout_ms=None):
    return _run(project, lambda conn, at, key, met: store.queue_hold(conn, presented, hold_kind, blocked_reason,
                                                                      blocking_item, at, config=config, met=met,
                                                                      key=key), now, config, busy_timeout_ms)


def reopen(project, task_id, actor, reason, *, now=None, config=None, busy_timeout_ms=None):
    return _run(project, lambda conn, at, key, met: store.queue_reopen(
        conn, task_id, actor, reason, at, config=config, met=met, key=key,
        decided=_decided_after(project, conn, task_id)), now, config, busy_timeout_ms)


def complete(project, task_id, actor, reason, *, now=None, config=None, busy_timeout_ms=None):
    """As `queue.complete`: the `close` rules of `store_tasks.close_task` are proven inside the same transaction."""
    return _run(project, lambda conn, at, key, met: store.queue_complete(
        conn, task_id, actor, reason, at, config=config, met=met, key=key,
        check=lambda row: store_tasks._check_closable(project, conn, task_id)), now, config, busy_timeout_ms)


def reclaim(project, task_id, actor, *, now=None, config=None, busy_timeout_ms=None):
    return _run(project, lambda conn, at, key, met: store.queue_reclaim(conn, task_id, actor, at, config=config,
                                                                         met=met, key=key),
                now, config, busy_timeout_ms)


def apply_start(project, presented, targets, *, now=None, config=None, busy_timeout_ms=None):
    return _run(project, lambda conn, at, key, met: store.queue_apply_start(conn, presented, targets, at,
                                                                             config=config, met=met, key=key,
                                                                             project=project),
                now, config, busy_timeout_ms)


def apply_finish(project, presented, *, now=None, config=None, busy_timeout_ms=None):
    return _run(project, lambda conn, at, key, met: store.queue_apply_finish(conn, presented, at, config=config,
                                                                              met=met, key=key),
                now, config, busy_timeout_ms)


def add_edges(project, additions, actor, *, now=None, config=None, busy_timeout_ms=None):
    return _run(project, lambda conn, at, key, met: store.queue_add_edges(conn, additions, actor, at, config=config,
                                                                           met=met, key=key),
                now, config, busy_timeout_ms)


def add_edge(project, task_id, needs_id, kind, actor, *, now=None, config=None, busy_timeout_ms=None):
    return add_edges(project, [(task_id, needs_id, kind)], actor, now=now, config=config,
                     busy_timeout_ms=busy_timeout_ms)


def remove_edge(project, task_id, needs_id, kind, actor, *, now=None, config=None, busy_timeout_ms=None):
    return _run(project, lambda conn, at, key, met: store.queue_remove_edge(conn, task_id, needs_id, kind, actor, at,
                                                                             config=config, met=met, key=key),
                now, config, busy_timeout_ms)


def status(project, *, now=None, config=None, busy_timeout_ms=None):
    """Per-task queue state after maintenance (expiry and promotion are written first, in one transaction)."""
    def call(conn, at, key, met):
        store.queue_maintain(conn, at, config=config, met=met)
        return store.queue_snapshot(conn, met)

    return _run(project, call, now, config, busy_timeout_ms)


def entry_for_key(project, key, *, busy_timeout_ms=None):
    """The reconciling read after an `AmbiguousCommit` that was raised to the caller: the entry written under `key`."""
    def run():
        conn = store_tasks._connect(project, False, busy_timeout_ms)
        try:
            return store.queue_entry_for_key(conn, key)
        finally:
            conn.close()

    return task_mod._guarded(run)


__all__ = [
    "add_edge",
    "add_edges",
    "apply_finish",
    "apply_start",
    "claim",
    "complete",
    "enqueue",
    "entry_for_key",
    "fail",
    "hold",
    "reclaim",
    "release",
    "remove_edge",
    "renew",
    "reopen",
    "start",
    "status",
    "submit",
]
