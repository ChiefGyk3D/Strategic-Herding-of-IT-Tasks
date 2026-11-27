"""Durable queue with per-task leases (docs/queue-spec.md; issue 17).

State lives in plain files under the confined `.shit` root:

- `queue.jsonl` and `queue.chain`: append-only `queue-entry` records with a sidecar SHA-256 hash chain, like the event log.
- `dependencies.json`: the typed `dependency-graph` record.
- `leases/<task-id>.json`: one `lease` record per live lease, trusted only if the chain proves it.
- `queue-pending.json`: the commit point of one queue mutation (lease file, lifecycle transition, entry, chain).
- `QUEUE.md`: a generated view. It is written after each mutation and never read.

Every public operation takes the store lock, finishes any interrupted commit, loads and verifies the whole
state, applies maintenance (orphan leases, expiry, waiting to ready) and only then performs its own step.
A queue operation that cannot verify its state fails closed with exit code 8 and writes nothing. Workers
never supply a time, a lease id, a generation or a state: those come from the wrapper.
"""

import hashlib
import hmac
import json
import secrets
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from functools import partial

from . import commands, records
from . import dependencies as dep
from . import queue_records as qr
from . import task as task_mod
from ._text import check_text
from .errors import (
    FAILED_CLOSED,
    HUMAN_APPROVAL,
    ILLEGAL_TRANSITION,
    LEASE_REFUSED,
    PRECONDITION,
    REJECTED,
    USAGE,
    ContractError,
    ShitError,
)
from .lifecycle import check_transition
from .log import GENESIS, EventLog, chain_hash

QUEUE = "queue.jsonl"
CHAIN = "queue.chain"
GRAPH = "dependencies.json"
PENDING = "queue-pending.json"
VIEW = "QUEUE.md"
LEASES = "leases"
VIEW_LIMIT = 1 << 20
LOCK_TIMEOUT = 60.0  # seconds; a busy queue is slower than a single task command
PENDING_LIMIT = 4 * records.MAX_RECORD_BYTES
STAMP = "%Y-%m-%dT%H:%M:%SZ"
MESSAGE = "Queue entry recorded."
PENDING_KEYS = {"index", "entry_line", "chain_line", "lease", "lease_replaces", "remove_lease", "lifecycle"}
LIFE_KEYS = {"task_id", "from", "to", "actor", "reason", "at", "blocked_reason", "blocking_item"}

State = namedtuple("State", "entries lines chain tasks graph")


def _checkpoint(stage):
    """Fault-injection seam: tests replace it to interrupt a commit between stages."""


def _new_lease_id():
    return secrets.token_hex(16)


def _now():
    return datetime.now(UTC).strftime(STAMP)


def _parse_stamp(value):
    return datetime.strptime(value, STAMP).replace(tzinfo=UTC)


def _plus(stamp, seconds):
    return (_parse_stamp(stamp) + timedelta(seconds=seconds)).strftime(STAMP)


def _closed(fn):
    """Run `fn`; a rejected record while loading queue state is a fail-closed condition (exit code 8)."""
    try:
        return fn()
    except ShitError as exc:
        if exc.code == REJECTED:
            raise ShitError(FAILED_CLOSED) from None
        raise


def _lease_path(task_id):
    return f"{LEASES}/{task_id}.json"


# -- loading and verifying ---------------------------------------------------


def _load_log(project, log):
    lines, chain = log._lines(QUEUE), log._lines(CHAIN)
    if len(lines) != len(chain):
        raise ShitError(REJECTED)
    previous, entries = GENESIS, []
    for line, recorded in zip(lines, chain):
        if len(line) > records.MAX_RECORD_BYTES or line == b"":
            raise ShitError(REJECTED)
        previous = chain_hash(previous, line)
        if recorded.decode("ascii", "replace") != previous:
            raise ShitError(REJECTED)
        entries.append(qr.load_entry(line))
    return entries, lines, chain


def _task_exists(project, task_id):
    return project.exists(task_mod._task_path(task_id))


def _load_graph(project):
    if not project.exists(GRAPH):
        return dep.empty_graph()
    graph = dep.load_graph(project.read(GRAPH, limit=records.MAX_RECORD_BYTES))
    edges = graph["edges"]
    if dep.find_cycle(edges):
        raise ShitError(REJECTED)
    ends = {end for edge in edges for end in (edge["task_id"], edge["needs_id"])}
    if dep.missing_nodes(edges, {end for end in ends if _task_exists(project, end)}):
        raise ShitError(REJECTED)
    return graph


def _check_agreement(project, tasks):
    """Every queued task's lifecycle state must be one the queue state allows (Q-ST-01, Q-REC-04, Q-RES-02)."""
    for task_id, queued in tasks.items():
        try:
            current = records.load_task(project.read(task_mod._task_path(task_id)))
        except ShitError as exc:
            if exc.code in (PRECONDITION, REJECTED):
                raise ShitError(REJECTED) from None
            raise
        if current["state"] not in qr.LIFECYCLE[queued["state"]]:
            raise ShitError(REJECTED)


def _load(project, log):
    def run():
        entries, lines, chain = _load_log(project, log)
        tasks = qr.replay(entries)
        graph = _load_graph(project)
        _check_agreement(project, tasks)
        return State(entries, lines, chain, tasks, graph)

    return _closed(run)


# -- milestones and readiness ------------------------------------------------


def _meter(project, log):
    """Return `met(edge)`: is the prerequisite's milestone reached, read from verified records only."""
    cache, events = {}, []

    def loaded(kind, task_id):
        key = (kind, task_id)
        if key not in cache:
            if not events:
                events.append(log.verify())  # read lazily: a queue without typed edges never parses the event log
            cache[key] = [commands._load(project, events[0], kind, rid, task_id)
                          for rid in commands._for_task(events[0], kind, task_id)]
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


def _unmet(state, met, task_id):
    return dep.unmet_blocking(state.graph["edges"], task_id, met)


def _effective(state, met, task_id):
    queued = state.tasks[task_id]
    if queued["state"] in ("waiting", "ready") and queued["lease"] is None:
        return "waiting" if _unmet(state, met, task_id) else "ready"
    return queued["state"]


# -- committing --------------------------------------------------------------


def _read_pending(project, log):
    try:
        doc = json.loads(project.read(PENDING, limit=PENDING_LIMIT).decode("utf-8"), object_pairs_hook=records._pairs)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ShitError(REJECTED) from None
    if not isinstance(doc, dict) or set(doc) != PENDING_KEYS:
        raise ShitError(REJECTED)
    index, entry_line, chain_line = doc["index"], doc["entry_line"], doc["chain_line"]
    if type(index) is not int or index < 0 or not isinstance(entry_line, str) or not isinstance(chain_line, str):
        raise ShitError(REJECTED)
    if not qr.HEX64.fullmatch(chain_line):
        raise ShitError(REJECTED)
    try:
        line = entry_line.encode("ascii")
    except UnicodeEncodeError:
        raise ShitError(REJECTED) from None
    entry = qr.load_entry(line)
    if doc["lease"] is not None:
        qr.validate_lease(doc["lease"])
        if doc["lease"]["task_id"] != entry["task_id"]:
            raise ShitError(REJECTED)
    replaces = doc["lease_replaces"]
    if replaces is not None and (not isinstance(replaces, str) or not qr.HEX64.fullmatch(replaces)):
        raise ShitError(REJECTED)
    remove = doc["remove_lease"]
    if remove is not None and remove != entry["task_id"]:
        raise ShitError(REJECTED)
    life = doc["lifecycle"]
    if life is not None:
        if not isinstance(life, dict) or set(life) != LIFE_KEYS or life["task_id"] != entry["task_id"]:
            raise ShitError(REJECTED)
        for key in ("from", "to", "actor", "reason", "at"):
            if not isinstance(life[key], str):
                raise ShitError(REJECTED)
        for key in ("blocked_reason", "blocking_item"):
            if life[key] is not None and not isinstance(life[key], str):
                raise ShitError(REJECTED)
    return doc, line, entry


def _apply_lease(project, doc, entry):
    """Step one: write, replace or remove the lease file. Returns False on a conflict nothing was applied for."""
    lease = doc["lease"]
    if lease is not None:
        path = _lease_path(lease["task_id"])
        data = qr.dump_lease(lease)
        if project.create_exclusive(path, data):
            return True
        existing = project.read(path, limit=records.MAX_RECORD_BYTES)
        if existing == data:
            return True
        if doc["lease_replaces"] is not None and _sha(existing) == doc["lease_replaces"]:
            project.write_atomic(path, data)
            return True
        return False
    if doc["remove_lease"] is not None:
        project.remove(_lease_path(doc["remove_lease"]))
    return True


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _apply_lifecycle(project, log, life):
    if life is None:
        return
    current = records.load_task(project.read(task_mod._task_path(life["task_id"])))
    done = {"from": life["from"], "to": life["to"], "actor": life["actor"], "at": life["at"], "reason": life["reason"]}
    if current["state"] == life["to"] and current["history"] and current["history"][-1] == done:
        return
    if current["state"] != life["from"]:
        raise ShitError(FAILED_CLOSED)
    task_mod._transition_locked(project, log, life["task_id"], life["to"], life["actor"], life["reason"], life["at"],
                                life["blocked_reason"], life["blocking_item"], None)


def _append_one(project, rel, lines, index, line):
    if len(lines) == index:
        project.append(rel, line + b"\n")
    elif len(lines) != index + 1 or lines[index] != line:
        raise ShitError(FAILED_CLOSED)


def _apply_pending(project, log, doc, line):
    """Apply a pending commit step by step. Every step is idempotent, so it is safe to repeat after a crash."""
    if not _apply_lease(project, doc, None):
        project.remove(PENDING)  # nothing was applied yet: discard the commit and fail closed (Q-REC-02)
        raise ShitError(FAILED_CLOSED)
    _checkpoint("lease-applied")
    _apply_lifecycle(project, log, doc["lifecycle"])
    _checkpoint("lifecycle-applied")
    index = doc["index"]
    _append_one(project, QUEUE, log._lines(QUEUE), index, line)
    _checkpoint("entry-appended")
    _append_one(project, CHAIN, log._lines(CHAIN), index, doc["chain_line"].encode("ascii"))
    _checkpoint("chain-appended")
    project.remove(PENDING)
    _checkpoint("pending-removed")


def _recover(project, log):
    task_mod._recover(project, log)
    if not project.exists(PENDING):
        return
    try:
        doc, line, _entry = _read_pending(project, log)
    except ShitError as exc:
        if exc.code in (REJECTED, 7):
            project.remove(PENDING)  # not verifiable: discard, no change (Q-REC-02)
            raise ShitError(FAILED_CLOSED) from None
        raise
    index = doc["index"]
    lines, torn = log._split(QUEUE)
    chain, chain_torn = log._split(CHAIN)
    try:
        log.heal_torn_tail(QUEUE, lines, torn, index, line + b"\n")
        log.heal_torn_tail(CHAIN, chain, chain_torn, index, doc["chain_line"].encode("ascii") + b"\n")
        lines, _ = log._split(QUEUE)
        chain, _ = log._split(CHAIN)
        if len(lines) < index or len(chain) < index:
            raise ShitError(REJECTED)
        previous = GENESIS if index == 0 else chain[index - 1].decode("ascii", "replace")
        if chain_hash(previous, line) != doc["chain_line"]:
            raise ShitError(REJECTED)
    except ShitError as exc:
        if exc.code == REJECTED:
            raise ShitError(FAILED_CLOSED) from None
        raise
    _closed(lambda: _apply_pending(project, log, doc, line))
    _load(project, log)


def _commit(ctx, fields, *, lease=None, lease_replaces=None, remove_lease=None, lifecycle=None):
    """Validate one queue entry against the replayed log, then apply it with its lease and lifecycle effects."""
    state = ctx.load()
    number = len(state.entries) + 1
    entry = {"schema": "queue-entry", "schema_version": 1, "id": f"queue-{number:04d}", "timestamp": ctx.now,
             "message": MESSAGE, **fields}
    line = qr.dump_entry(entry)
    _closed(lambda: qr.replay([*state.entries, entry]))
    if lifecycle is not None:
        _plan_lifecycle(ctx, lifecycle)
    previous = GENESIS if not state.chain else state.chain[-1].decode("ascii")
    doc = {"index": len(state.entries), "entry_line": line.decode("ascii"), "chain_line": chain_hash(previous, line),
           "lease": lease, "lease_replaces": lease_replaces, "remove_lease": remove_lease, "lifecycle": lifecycle}
    ctx.project.write_atomic(PENDING, json.dumps(doc, ensure_ascii=True).encode("ascii"), limit=PENDING_LIMIT)
    _checkpoint("pending-written")
    ctx.invalidate()
    _apply_pending(ctx.project, ctx.log, doc, line)
    ctx.mutated = True
    return entry


def _plan_lifecycle(ctx, life):
    """Refuse a lifecycle step that could not be applied, before anything is written."""
    check_transition(life["from"], life["to"], life["actor"])
    current = records.load_task(ctx.project.read(task_mod._task_path(life["task_id"])))
    if current["state"] != life["from"]:
        raise ShitError(FAILED_CLOSED)
    if life["at"] < current["updated_at"]:
        raise ShitError(FAILED_CLOSED)
    if (life["to"] == "blocked") != (life["blocked_reason"] is not None and life["blocking_item"] is not None):
        raise ShitError(REJECTED)


def _life(task_id, frm, to, actor, reason, now, blocked_reason=None, blocking_item=None):
    return {"task_id": task_id, "from": frm, "to": to, "actor": actor, "reason": reason, "at": now,
            "blocked_reason": blocked_reason, "blocking_item": blocking_item}


# -- the operation context ---------------------------------------------------


class _Ctx:
    def __init__(self, project, log, now, config):
        self.project, self.log, self.now, self.config = project, log, now, config
        self.mutated = False
        self._state = None
        self._met = None

    def load(self):
        if self._state is None:
            self._state = _load(self.project, self.log)
        return self._state

    def met(self):
        if self._met is None:
            self._met = _meter(self.project, self.log)
        return self._met

    def invalidate(self):
        self._state = self._met = None


def _check_clock(ctx, state):
    if state.entries and ctx.now < state.entries[-1]["timestamp"]:
        raise ShitError(FAILED_CLOSED)  # the clock is earlier than a recorded time: no claim, no expiry (Q-LEASE-08)


def _expire(ctx, task_id, state):
    queued = state.tasks[task_id]
    lease = queued["lease"]
    running = queued["state"] == "running"
    fields = {"task_id": task_id, "actor": "wrapper", "kind": "expired", "lease_id": lease["lease_id"],
              "generation": lease["generation"], "state_after": "failed" if running else "ready"}
    life = _life(task_id, "running", "failed", "wrapper", "lease expired", ctx.now) if running else None
    _commit(ctx, fields, remove_lease=task_id, lifecycle=life)


def _read_lease(project, name):
    return qr.load_lease(project.read(f"{LEASES}/{name}", limit=records.MAX_RECORD_BYTES))


def _maintain(ctx):
    """Reconcile files with the chain, expire leases and promote waiting tasks, one committed step at a time."""
    for _ in range(100000):
        if not _maintain_once(ctx):
            return
    raise ShitError(FAILED_CLOSED)


def _maintain_once(ctx):
    state = ctx.load()
    _check_clock(ctx, state)
    for name in ctx.project.list_dir(LEASES):
        if name.startswith(".tmp-"):
            ctx.project.remove(f"{LEASES}/{name}")
            return True
        task_id = name[:-5] if name.endswith(".json") else ""
        if not records.SLUG.fullmatch(task_id):
            raise ShitError(FAILED_CLOSED)
        queued = state.tasks.get(task_id)
        expected = qr.expected_lease(task_id, queued) if queued else None
        if expected is None:
            ctx.project.remove(f"{LEASES}/{name}")
            if queued is not None:
                _commit(ctx, {"task_id": task_id, "actor": "wrapper", "kind": "orphan_removed",
                              "state_after": queued["state"]})
            return True
        on_disk = _closed(partial(_read_lease, ctx.project, name))
        if {k: on_disk[k] for k in expected} != expected:
            raise ShitError(FAILED_CLOSED)
    present = set(ctx.project.list_dir(LEASES))
    for task_id, queued in sorted(state.tasks.items()):
        if queued["lease"] is None:
            continue
        if f"{task_id}.json" not in present or ctx.now >= queued["lease"]["expires_at"]:
            _expire(ctx, task_id, state)  # a missing lease file counts as expiry (Q-REC-03b)
            return True
    met = ctx.met()
    for task_id, queued in sorted(state.tasks.items()):
        if queued["state"] == "waiting" and not _unmet(state, met, task_id):
            _commit(ctx, {"task_id": task_id, "actor": "wrapper", "kind": "ready", "state_after": "ready"})
            return True
    return False


def _operate(project, now, config, body):
    now = now or _now()
    config = qr.make_config(**(config or {}))
    log = EventLog(project)

    def run():
        with project.locked(LOCK_TIMEOUT):
            _recover(project, log)
            project.ensure_dir(LEASES)
            ctx = _Ctx(project, log, now, config)
            state = ctx.load()
            _check_clock(ctx, state)
            _maintain(ctx)
            ctx.mutated = False
            try:
                result = body(ctx)
            finally:
                _render_quiet(ctx)
            return result

    return task_mod._guarded(run)


def _render_quiet(ctx):
    """Regenerate QUEUE.md after a mutation. The view is derived, so a failure to write it never undoes state."""
    try:
        _write_view(ctx)
    except ShitError:
        pass


# -- the view ----------------------------------------------------------------


def _snapshot(ctx, state):
    met = ctx.met()
    out = {}
    for task_id, queued in sorted(state.tasks.items()):
        lease = queued["lease"]
        out[task_id] = {
            "state": _effective(state, met, task_id), "stored_state": queued["state"],
            "attempts": queued["attempts"], "generation": queued["generation"],
            "waiting_on": sorted({e["needs_id"] for e in _unmet(state, met, task_id)}),
            "not_before": queued["not_before"],
            "lease": None if lease is None else {k: lease[k] for k in ("worker", "step", "generation", "expires_at")},
        }
    return out


def _render(snapshot):
    lines = ["# Queue", "", "Generated view. Never read as input; deleting or editing it changes nothing.", "",
             "| Task | State | Attempts | Waiting on | Worker | Step | Lease expires |", "|---|---|---|---|---|---|---|"]
    for task_id, row in snapshot.items():
        lease = row["lease"] or {}
        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            task_id, row["state"], row["attempts"], ", ".join(row["waiting_on"]) or "-",
            lease.get("worker", "-"), lease.get("step", "-"), lease.get("expires_at", "-")))
    return ("\n".join(lines) + "\n").encode("ascii")


def _write_view(ctx):
    state = ctx.load()
    ctx.project.write_atomic(VIEW, _render(_snapshot(ctx, state)), limit=VIEW_LIMIT)


def render_view(project, *, now=None, config=None):
    """Regenerate QUEUE.md on request (Q-REC-06). Raises instead of hiding a failure."""
    def body(ctx):
        _write_view(ctx)

    return _operate(project, now, config, body)


def status(project, *, now=None, config=None):
    """Per-task queue state after maintenance: effective state, attempts, lease holder label and blockers."""
    return _operate(project, now, config, lambda ctx: _snapshot(ctx, ctx.load()))


# -- graph and enqueue -------------------------------------------------------


def _human_or_wrapper(actor):
    if actor not in ("human", "wrapper"):
        raise ShitError(HUMAN_APPROVAL)


def _task_id(value):
    if not isinstance(value, str) or not records.SLUG.fullmatch(value):
        raise ShitError(REJECTED)
    return value


def add_edge(project, task_id, needs_id, kind, actor, *, now=None, config=None):
    """Add a typed edge. Only `human` or `wrapper` may change the graph (Q-DEP-07); a cycle is exit code 3."""
    _human_or_wrapper(actor)

    def body(ctx):
        state = ctx.load()
        known = {end for end in (task_id, needs_id) if isinstance(end, str) and records.SLUG.fullmatch(end)
                 and _task_exists(project, end)}
        graph = dep.add_edge(state.graph, task_id, needs_id, kind, known)
        project.write_atomic(GRAPH, dep.dump_graph(graph))
        ctx.invalidate()
        ctx.mutated = True
        return graph

    return _operate(project, now, config, body)


def remove_edge(project, task_id, needs_id, kind, actor, *, now=None, config=None):
    _human_or_wrapper(actor)

    def body(ctx):
        state = ctx.load()
        queued = state.tasks.get(task_id)
        graph = dep.remove_edge(state.graph, task_id, needs_id, kind)
        if queued is not None and queued["lease"] is not None:
            raise ShitError(ILLEGAL_TRANSITION)
        project.write_atomic(GRAPH, dep.dump_graph(graph))
        ctx.invalidate()
        ctx.mutated = True
        return graph

    return _operate(project, now, config, body)


def enqueue(project, task_id, actor, *, now=None, config=None):
    """Put an open task into the queue as `waiting` or `ready`. A task id is enqueued once, ever (Q-RES-01)."""
    _human_or_wrapper(actor)
    _task_id(task_id)

    def body(ctx):
        state = ctx.load()
        if task_id in state.tasks or any(e["task_id"] == task_id for e in state.entries):
            raise ShitError(REJECTED)
        current = records.load_task(project.read(task_mod._task_path(task_id)))
        if current["state"] != "open":
            raise ShitError(ILLEGAL_TRANSITION)
        unmet = dep.unmet_blocking(state.graph["edges"], task_id, ctx.met())
        _commit(ctx, {"task_id": task_id, "actor": actor, "kind": "enqueued",
                      "state_after": "waiting" if unmet else "ready"})

    return _operate(project, now, config, body)


# -- claims and lease operations ---------------------------------------------

CLAIM_KEYS = {"task_id", "step", "worker", "worker_kind"}


def _shape(request, keys):
    """A request must carry exactly `keys`. Extra fields (a time, a lease id, a state) are exit code 2 (Q-CLAIM-05)."""
    if not isinstance(request, dict) or set(request) != keys:
        raise ShitError(USAGE)


def claim(project, request, *, now=None, config=None):
    """Claim one step of a ready task under a new lease. The request holds only task id, step, worker label and kind."""
    _shape(request, CLAIM_KEYS)
    task_id, step, worker, kind = (request[k] for k in ("task_id", "step", "worker", "worker_kind"))
    if (not isinstance(task_id, str) or not records.SLUG.fullmatch(task_id) or step not in qr.STEPS
            or not isinstance(worker, str) or not records.SLUG.fullmatch(worker) or kind not in qr.WORKER_KINDS):
        raise ShitError(USAGE)

    def body(ctx):
        state = ctx.load()
        queued = state.tasks.get(task_id)
        if queued is None:
            raise ShitError(PRECONDITION)
        if _effective(state, ctx.met(), task_id) != "ready":
            raise ShitError(ILLEGAL_TRANSITION)
        if queued["not_before"] is not None and ctx.now < queued["not_before"]:
            raise ShitError(ILLEGAL_TRANSITION)
        if step == "apply":
            for pre in dep.apply_prerequisites(state.graph["edges"], task_id):
                if not state.tasks.get(pre, {}).get("apply_finished"):
                    raise ShitError(ILLEGAL_TRANSITION)
        lease = {"schema": "lease", "schema_version": 1, "task_id": task_id, "step": step, "worker": worker,
                 "worker_kind": kind, "lease_id": _new_lease_id(), "generation": queued["generation"] + 1,
                 "claimed_at": ctx.now, "expires_at": _plus(ctx.now, ctx.config["lease_ttl_seconds"]), "renewals": 0}
        _commit(ctx, {"task_id": task_id, "actor": "wrapper", "kind": "claimed", "state_after": "dispatched",
                      "lease_id": lease["lease_id"], "generation": lease["generation"], "worker": worker,
                      "step": step, "expires_at": lease["expires_at"]}, lease=lease)
        return lease

    return _operate(project, now, config, body)


def _check_presented(state, presented, extra=()):
    """Verify a presented lease against the chain-proven lease (Q-LEASE-01, Q-LEASE-02, Q-LEASE-10)."""
    _shape(presented, {"task_id", "lease_id", "generation", *extra})
    task_id, lease_id, generation = presented["task_id"], presented["lease_id"], presented["generation"]
    if (not isinstance(task_id, str) or not records.SLUG.fullmatch(task_id) or not isinstance(lease_id, str)
            or type(generation) is not int):
        raise ShitError(USAGE)
    queued = state.tasks.get(task_id)
    if queued is None:
        raise ShitError(PRECONDITION)
    lease = queued["lease"]
    if lease is None:
        raise ShitError(LEASE_REFUSED)
    same_id = hmac.compare_digest(lease_id.encode("utf-8"), lease["lease_id"].encode("ascii"))
    current = generation == lease["generation"] and generation >= queued["generation"]
    if not (same_id and current):
        raise ShitError(LEASE_REFUSED)
    return queued, lease


def _leased(project, presented, now, config, body, extra=()):
    def run(ctx):
        state = ctx.load()
        queued, lease = _check_presented(state, presented, extra)
        return body(ctx, state, queued, lease)

    return _operate(project, now, config, run)


def _lease_fields(task_id, lease, kind, after, **more):
    return {"task_id": task_id, "actor": "wrapper", "kind": kind, "state_after": after,
            "lease_id": lease["lease_id"], "generation": lease["generation"], **more}


def start(project, presented, *, now=None, config=None):
    """The worker starts the step: dispatched to running, open to running in the lifecycle."""
    def body(ctx, state, queued, lease):
        if queued["state"] != "dispatched":
            raise ShitError(ILLEGAL_TRANSITION)
        task_id = presented["task_id"]
        _commit(ctx, _lease_fields(task_id, lease, "started", "running", worker=lease["worker"], step=lease["step"],
                                   attempt=queued["attempts"] + 1),
                lifecycle=_life(task_id, "open", "running", "wrapper", "worker started", ctx.now))

    return _leased(project, presented, now, config, body)


def renew(project, presented, *, now=None, config=None):
    """Extend the lease by one TTL, bounded by `lease_max_seconds` and `lease_max_renewals` (Q-LEASE-03)."""
    def body(ctx, state, queued, lease):
        task_id = presented["task_id"]
        longest = _plus(lease["claimed_at"], ctx.config["lease_max_seconds"])
        new_expiry = min(_plus(lease["expires_at"], ctx.config["lease_ttl_seconds"]), longest)
        if lease["renewals"] + 1 > ctx.config["lease_max_renewals"] or new_expiry <= lease["expires_at"]:
            raise ShitError(LEASE_REFUSED)
        path = _lease_path(task_id)
        old = ctx.project.read(path, limit=records.MAX_RECORD_BYTES)
        record = qr.load_lease(old)
        record.update(expires_at=new_expiry, renewals=lease["renewals"] + 1)
        _commit(ctx, _lease_fields(task_id, lease, "renewed", queued["state"], worker=lease["worker"],
                                   step=lease["step"], expires_at=new_expiry),
                lease=record, lease_replaces=_sha(old))
        return record

    return _leased(project, presented, now, config, body)


def release(project, presented, *, now=None, config=None):
    """Give back a lease before the worker started. A running task cannot return to ready (exit code 4)."""
    def body(ctx, state, queued, lease):
        if queued["state"] != "dispatched":
            raise ShitError(ILLEGAL_TRANSITION)
        task_id = presented["task_id"]
        _commit(ctx, _lease_fields(task_id, lease, "released", "ready"), remove_lease=task_id)

    return _leased(project, presented, now, config, body)


def submit(project, presented, *, now=None, config=None):
    """Report finished work: running to review. Refused with exit code 9 while a blocking edge is unmet (Q-DEP-09)."""
    def body(ctx, state, queued, lease):
        if queued["state"] != "running":
            raise ShitError(ILLEGAL_TRANSITION)
        task_id = presented["task_id"]
        if _unmet(state, ctx.met(), task_id):
            raise ShitError(PRECONDITION)
        _commit(ctx, _lease_fields(task_id, lease, "submitted", "review"), remove_lease=task_id,
                lifecycle=_life(task_id, "running", "review", "wrapper", "work submitted", ctx.now))

    return _leased(project, presented, now, config, body)


def fail(project, presented, *, now=None, config=None):
    """The worker reports a failed attempt. The task stays failed until a human reopens it (Q-RET-02)."""
    def body(ctx, state, queued, lease):
        task_id = presented["task_id"]
        frm = "running" if queued["state"] == "running" else "open"
        _commit(ctx, _lease_fields(task_id, lease, "failed", "failed"), remove_lease=task_id,
                lifecycle=_life(task_id, frm, "failed", "wrapper", "worker reported failure", ctx.now))

    return _leased(project, presented, now, config, body)


def _clean(value):
    if not isinstance(value, str) or value == "":
        raise ShitError(REJECTED)
    try:
        check_text(value)
    except ContractError:
        raise ShitError(REJECTED) from None
    return value


def hold(project, presented, hold_kind, blocked_reason, blocking_item, *, now=None, config=None):
    """The worker identifies a blocker: the lease and any apply slot are released and the task waits for a human or a check."""
    if hold_kind not in qr.HOLD_KINDS:
        raise ShitError(USAGE)
    _clean(blocked_reason)
    _clean(blocking_item)

    def body(ctx, state, queued, lease):
        task_id = presented["task_id"]
        frm = "running" if queued["state"] == "running" else "open"
        _commit(ctx, {**_lease_fields(task_id, lease, "held", f"hold-{hold_kind}"), "hold_kind": hold_kind},
                remove_lease=task_id,
                lifecycle=_life(task_id, frm, "blocked", "wrapper", "blocker identified", ctx.now,
                                blocked_reason, blocking_item))

    return _leased(project, presented, now, config, body)


# -- human and wrapper operations on a task ----------------------------------


def _decided_after(project, log, task_id, stamp):
    """True when an accepted, effective human decision on the task was recorded after `stamp` (Q-RET-03)."""
    events = log.verify()
    decisions = [commands._load(project, events, "decision", rid, task_id)
                 for rid in commands._for_task(events, "decision", task_id)]
    superseded = {d["supersedes_id"] for d in decisions if "supersedes_id" in d}
    return any(d["status"] == "accepted" and d["actor"] == "human" and d["id"] not in superseded
               and (stamp is None or d["created_at"] > stamp) for d in decisions)


def reopen(project, task_id, actor, reason, *, now=None, config=None):
    """Human only: review, a hold or failed back to ready (or waiting when an edge is unmet)."""
    _task_id(task_id)
    _clean(reason)

    def body(ctx, state, queued, *_):
        before = queued["state"]
        if before not in ("review", "failed", *qr.HOLDS):
            raise ShitError(ILLEGAL_TRANSITION)
        frm = {"review": "review", "failed": "failed"}.get(before, "blocked")
        check_transition(frm, "open", actor)
        needs_decision = before == "failed" and queued["attempts"] >= ctx.config["max_attempts"]
        if needs_decision and not _decided_after(ctx.project, ctx.log, task_id, queued["last_failure"]):
            raise ShitError(HUMAN_APPROVAL)
        after = "waiting" if _unmet(state, ctx.met(), task_id) else "ready"
        _commit(ctx, {"task_id": task_id, "actor": actor, "kind": "reopened", "state_after": after},
                lifecycle=_life(task_id, frm, "open", actor, reason, ctx.now))

    return _task_op(project, task_id, now, config, body)


def complete(project, task_id, actor, reason, *, now=None, config=None):
    """Close an approved task under the `close` rules and mark it done. Terminal (Q-ST-05)."""
    _task_id(task_id)
    _clean(reason)

    def body(ctx, state, queued, *_):
        if queued["state"] != "review":
            raise ShitError(ILLEGAL_TRANSITION)
        if task_mod.get_task_locked(ctx.project, task_id)["state"] != "approved":
            raise ShitError(ILLEGAL_TRANSITION)
        check_transition("approved", "closed", actor)
        commands._check_closable(ctx.project, ctx.log, task_id)
        _commit(ctx, {"task_id": task_id, "actor": actor, "kind": "done", "state_after": "done"},
                lifecycle=_life(task_id, "approved", "closed", actor, reason, ctx.now))

    return _task_op(project, task_id, now, config, body)


def reclaim(project, task_id, actor, *, now=None, config=None):
    """A human forces a live lease to expire. The entries are exactly those of a natural expiry (Q-REC-05)."""
    _task_id(task_id)
    if actor != "human":
        raise ShitError(HUMAN_APPROVAL)

    def body(ctx, state, queued, *_):
        if queued["lease"] is None:
            raise ShitError(PRECONDITION)
        _expire(ctx, task_id, state)

    return _task_op(project, task_id, now, config, body)


def _task_op(project, task_id, now, config, body):
    def run(ctx):
        state = ctx.load()
        queued = state.tasks.get(task_id)
        if queued is None:
            raise ShitError(PRECONDITION)
        return body(ctx, state, queued)

    return _operate(project, now, config, run)
