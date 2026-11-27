"""Durable queue with per-task leases (docs/queue-spec.md; issue 17).

State lives in plain files under the confined `.shit` root:

- `queue.jsonl` and `queue.chain`: append-only `queue-entry` records with a sidecar SHA-256 hash chain, like the event log.
- `deps/<task-id>.json`: one `dependency-shard` record per dependent task; the chain (`graph_changed` entries) proves each shard's digest.
- `leases/<task-id>.json`: one `lease` record per live lease, trusted only if the chain proves it.
- `queue-pending.json`: the commit point of one queue mutation (lease file, lifecycle transition, entry, chain).
- `QUEUE.md`: a generated view. It is written after each mutation and never read.

Every public operation takes the store lock, refuses on sight when a `queue-pending.json` exists (only `recover`
finishes a queue commit; see `_recover`), loads and verifies the whole state, applies maintenance (orphan leases,
expiry, waiting to ready) and only then performs its own step. A queue operation that cannot verify its state
fails closed with exit code 8 and writes nothing. Workers never supply a time, a lease id, a generation or a
state: those come from the wrapper.
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
from . import log as eventlog
from . import queue_records as qr
from . import task as task_mod
from ._text import check_text
from .errors import (
    FAILED_CLOSED,
    HUMAN_APPROVAL,
    ILLEGAL_TRANSITION,
    INPUT_LIMIT,
    LEASE_REFUSED,
    PRECONDITION,
    REJECTED,
    USAGE,
    ContractError,
    ShitError,
)
from .lifecycle import check_transition
from .log import CHAIN_LINE_BYTES, GENESIS, EventLog, chain_hash

QUEUE = "queue.jsonl"
CHAIN = "queue.chain"
DEPS = "deps"
LEGACY_GRAPH = "dependencies.json"  # the pre-shard single file: never written, refused when present
PENDING = "queue-pending.json"
ANCHOR = "queue-anchor.json"  # the queue tip and per-task generation high-water marks, kept apart from the queue files
VIEW = "QUEUE.md"
LEASES = "leases"
VIEW_LIMIT = 1 << 20
LOCK_TIMEOUT = 60.0  # seconds; a busy queue is slower than a single task command
PENDING_LIMIT = 4 * records.MAX_RECORD_BYTES
STAMP = "%Y-%m-%dT%H:%M:%SZ"
MESSAGE = "Queue entry recorded."
PENDING_KEYS = {"index", "entry_line", "chain_line", "lease", "lease_replaces", "remove_lease", "lifecycle", "shards"}
LIFE_KEYS = {"task_id", "from", "to", "actor", "reason", "at", "blocked_reason", "blocking_item"}

State = namedtuple("State", "entries lines chain tasks graph by_task")


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


def _read_anchor(project):
    """The anchor record, or None when absent. Anything malformed is a rejected record."""
    try:
        data = project.read(ANCHOR, limit=records.MAX_RECORD_BYTES)
    except ShitError as exc:
        if exc.code == PRECONDITION:
            return None
        raise
    doc = records.parse(data)
    if not isinstance(doc, dict) or set(doc) != {"schema", "schema_version", "count", "tip", "generations"}:
        raise ShitError(REJECTED)
    gens = doc["generations"]
    if (doc["schema"] != "queue-anchor" or doc["schema_version"] != 1 or type(doc["count"]) is not int
            or doc["count"] < 0 or not isinstance(doc["tip"], str) or not qr.HEX64.fullmatch(doc["tip"])
            or not isinstance(gens, dict) or len(gens) > dep.MAX_TASKS):
        raise ShitError(REJECTED)
    for task_id, generation in gens.items():
        if not records.SLUG.fullmatch(task_id) or type(generation) is not int or generation < 1:
            raise ShitError(REJECTED)
    return doc


def _check_anchor(project, entries, chain, tasks):
    """The queue must end exactly where the anchor says, and no task's generation may be below its high-water mark."""
    anchor = _read_anchor(project)
    tip = chain[-1].decode("ascii") if chain else GENESIS
    if anchor is None:
        if entries:
            raise ShitError(REJECTED)
        return
    if anchor["count"] != len(entries) or anchor["tip"] != tip:
        raise ShitError(REJECTED)
    held = {t: v["generation"] for t, v in tasks.items() if v["generation"]}
    if anchor["generations"] != held:
        raise ShitError(REJECTED)


def _write_anchor(project, doc, entry, chain):
    """Advance the anchor by exactly this commit. Idempotent: an anchor already at this commit is kept."""
    index = doc["index"]
    current = _read_anchor(project)
    if current is not None and current["count"] == index + 1:
        if current["tip"] != doc["chain_line"]:
            raise ShitError(REJECTED)
        return
    previous = GENESIS if index == 0 else chain[index - 1].decode("ascii")
    if current is None:
        if index != 0:
            raise ShitError(REJECTED)
        current = {"count": 0, "tip": GENESIS, "generations": {}}
    if current["count"] != index or current["tip"] != previous:
        raise ShitError(REJECTED)
    generations = dict(current["generations"])
    if entry["kind"] == "claimed":
        generations[entry["task_id"]] = entry["generation"]
    new = {"schema": "queue-anchor", "schema_version": 1, "count": index + 1, "tip": doc["chain_line"],
           "generations": generations}
    project.write_atomic(ANCHOR, json.dumps(new, sort_keys=True, ensure_ascii=True).encode("ascii"),
                         limit=records.MAX_RECORD_BYTES)


def _task_exists(project, task_id):
    return project.exists(task_mod._task_path(task_id))


def _list(project, name):
    """Directory listing for a read-only proof: an absent directory is an empty one (it is created after the proof)."""
    try:
        return project.list_dir(name)
    except ShitError as exc:
        if exc.code == PRECONDITION:
            return []
        raise


def _read_shard(project, task_id):
    try:
        return project.read(f"{DEPS}/{task_id}.json", limit=records.MAX_RECORD_BYTES)
    except ShitError as exc:
        if exc.code == INPUT_LIMIT:
            raise ShitError(REJECTED) from None  # an oversize shard is tampering: fail closed
        raise


def _load_graph(project, entries, tasks, overrides=None):
    """Assemble the graph from its shards, each proven by the chain, then check cycles and dangling edges."""
    proven = {t: d for t, d in qr.graph_digests(entries).items() if d != qr.ABSENT}
    if project.exists(LEGACY_GRAPH):
        raise ShitError(REJECTED)
    present = set()
    for name in _list(project, DEPS):
        if name.startswith(".tmp-"):
            continue  # a crash leftover; maintenance removes it
        task_id = name[:-5] if name.endswith(".json") else ""
        if not records.SLUG.fullmatch(task_id):
            raise ShitError(REJECTED)
        present.add(task_id)
    for task_id, content in (overrides or {}).items():  # a pending commit is judged as if it were already applied
        if content is None:
            present.discard(task_id)
        else:
            present.add(task_id)
    if present != set(proven) or len(present | set(tasks)) > dep.MAX_TASKS:
        raise ShitError(REJECTED)  # a shard without a chain entry, a proven shard that is gone, or too many task ids
    edges = []
    for task_id in sorted(present):
        data = overrides[task_id].encode("ascii") if overrides and overrides.get(task_id) is not None \
            else _read_shard(project, task_id)
        if _sha(data) != proven[task_id]:
            raise ShitError(REJECTED)
        try:
            shard = dep.load_shard(data, task_id)
        except ShitError as exc:
            if exc.code == INPUT_LIMIT:
                raise ShitError(REJECTED) from None
            raise
        if not shard["needs"]:
            raise ShitError(REJECTED)  # an empty shard is never written: its owner would be a task with no record
        edges.extend(dep.edges_of(task_id, shard["needs"]))
    graph = dep.validate_graph({**dep.empty_graph(), "edges": edges})
    if dep.find_cycle(edges):
        raise ShitError(REJECTED)
    ends = {end for edge in edges for end in (edge["task_id"], edge["needs_id"])}
    if dep.missing_nodes(edges, {end for end in ends if _task_exists(project, end)}):
        raise ShitError(REJECTED)
    return graph


def _check_agreement(project, tasks, overrides=None):
    """Every queued task's lifecycle state must be one the queue state allows (Q-ST-01, Q-REC-04, Q-RES-02).

    `overrides` maps a task id to the task image a proven, not yet applied task commit would write."""
    for task_id, queued in tasks.items():
        try:
            current = (overrides or {}).get(task_id) or records.load_task(project.read(task_mod._task_path(task_id)))
        except ShitError as exc:
            if exc.code in (PRECONDITION, REJECTED):
                raise ShitError(REJECTED) from None
            raise
        if current["state"] not in qr.LIFECYCLE[queued["state"]]:
            raise ShitError(REJECTED)


def _load(project, log, overrides=None):
    def run():
        entries, lines, chain = _load_log(project, log)
        tasks = qr.replay(entries)
        _check_anchor(project, entries, chain, tasks)
        graph = _load_graph(project, entries, tasks)
        _check_agreement(project, tasks, overrides)
        by_task = {}
        for edge in graph["edges"]:
            by_task.setdefault(edge["task_id"], []).append(edge)  # indexed once: readiness never rescans every edge
        return State(entries, lines, chain, tasks, graph, by_task)

    return _closed(run)


# -- milestones and readiness ------------------------------------------------


def _meter(project, get_events):
    """Return `met(edge)`: is the prerequisite's milestone reached, read from verified records only."""
    cache, events = {}, []

    def loaded(kind, task_id):
        key = (kind, task_id)
        if key not in cache:
            if not events:
                events.append(get_events())  # read lazily: a queue without typed edges never parses the event log
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
    return dep.unmet_blocking(state.by_task.get(task_id, ()), task_id, met)


def _effective(state, met, task_id):
    queued = state.tasks[task_id]
    if queued["state"] in ("waiting", "ready") and queued["lease"] is None:
        return "waiting" if _unmet(state, met, task_id) else "ready"
    return queued["state"]


# -- committing --------------------------------------------------------------


def _read_pending(project, log):
    """Parse and validate the pending record from its own bytes. Every failure, a size limit included, is a rejection."""
    try:
        return _read_pending_checked(project, log)
    except ShitError as exc:
        if exc.code == INPUT_LIMIT:
            raise ShitError(REJECTED) from None
        raise


def _read_pending_checked(project, log):
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
    _check_pending_shards(doc["shards"], entry)
    qr.check_effects(doc, entry)
    return doc, line, entry


def _check_pending_shards(shards, entry):
    """The shards a pending commit would write must match, byte for byte, the digests its entry will record."""
    if not isinstance(shards, list) or (entry["kind"] == "graph_changed") != bool(shards):
        raise ShitError(REJECTED)
    if not shards:
        return
    proven = {item["task_id"]: item["sha256"] for item in entry["shards"]}
    seen = []
    for item in shards:
        if not isinstance(item, dict) or set(item) != {"task_id", "content"}:
            raise ShitError(REJECTED)
        task_id, content = _task_id(item["task_id"]), item["content"]
        seen.append(task_id)
        if content is None:
            digest = qr.ABSENT
        elif isinstance(content, str) and content.isascii():
            digest = _sha(content.encode("ascii"))
            if not dep.load_shard(content.encode("ascii"), task_id)["needs"]:
                raise ShitError(REJECTED)  # an empty shard is never written
        else:
            raise ShitError(REJECTED)
        if proven.get(task_id) != digest:
            raise ShitError(REJECTED)
    if sorted(seen) != sorted(proven) or len(set(seen)) != len(seen):
        raise ShitError(REJECTED)


def _apply_shards(project, shards):
    for item in shards:
        path = f"{DEPS}/{item['task_id']}.json"
        if item["content"] is None:
            project.remove(path)
        else:
            project.write_atomic(path, item["content"].encode("ascii"))


_UNPROVEN = object()  # a commit applied right after it was written, from state the operation itself just verified


def _apply_lease(project, doc, removal=_UNPROVEN):
    """Step one: write, replace or remove the lease file. Returns False on a conflict nothing was applied for.

    In recovery `removal` is the digest phase one proved for the lease to remove (None: it was absent); the file is
    removed only if it still has that digest."""
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
        path = _lease_path(doc["remove_lease"])
        if removal is not _UNPROVEN:
            try:
                current = _sha(project.read(path, limit=records.MAX_RECORD_BYTES))
            except ShitError as exc:
                if exc.code == PRECONDITION:
                    return removal is None
                raise
            if current != removal:
                return False
        project.remove(path)
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
        project.append(rel, line + b"\n", limit=eventlog.MAX_LOG_BYTES)
    elif len(lines) != index + 1 or lines[index] != line:
        raise ShitError(FAILED_CLOSED)


def _apply_pending(project, log, doc, line, removal=_UNPROVEN):
    """Apply a pending commit step by step. Every step is idempotent, so it is safe to repeat after a crash."""
    if not _apply_lease(project, doc, removal):
        raise ShitError(FAILED_CLOSED)  # defensive: recovery proves the lease path first; the record is never removed here
    _checkpoint("lease-applied")
    _apply_lifecycle(project, log, doc["lifecycle"])
    _checkpoint("lifecycle-applied")
    _apply_shards(project, doc["shards"])
    _checkpoint("shards-applied")
    index = doc["index"]
    _append_one(project, QUEUE, log._lines(QUEUE), index, line)
    _checkpoint("entry-appended")
    _append_one(project, CHAIN, log._lines(CHAIN), index, doc["chain_line"].encode("ascii"))
    _checkpoint("chain-appended")
    _write_anchor(project, doc, qr.load_entry(line), log._lines(CHAIN))
    _checkpoint("anchor-written")
    project.remove(PENDING)
    _checkpoint("pending-removed")


def _check_anchor_for_pending(project, doc, chain):
    """The anchor must sit at the pre-commit prefix or already at this exact commit; anything else applies nothing."""
    index = doc["index"]
    anchor = _read_anchor(project)
    if index > len(chain):
        raise ShitError(REJECTED)
    previous = GENESIS if index == 0 else chain[index - 1].decode("ascii", "replace")
    if anchor is None:
        if index != 0:
            raise ShitError(REJECTED)
        return
    at_prefix = anchor["count"] == index and anchor["tip"] == previous
    at_commit = anchor["count"] == index + 1 and anchor["tip"] == doc["chain_line"]
    if not (at_prefix or at_commit):
        raise ShitError(REJECTED)


def _check_lease_effect(project, doc):
    """A lease effect needs the path free, already holding this lease, or holding the one it replaces. Read-only.

    A leftover publication temp name (an exact pair, see `Project.publication_pairs`) is read as the pair it is;
    its cleanup is a phase-two write."""
    lease = doc["lease"]
    if lease is None:
        return
    path = _lease_path(lease["task_id"])
    pair = any(sibling == f"{lease['task_id']}.json" for _tmp, sibling in project.publication_pairs(LEASES))
    try:
        existing = project.read(path, limit=records.MAX_RECORD_BYTES, links=2 if pair else 1)
    except ShitError as exc:
        if exc.code == PRECONDITION:
            return  # the file, or the directory that would hold it, is absent: the effect creates it
        raise
    if existing != qr.dump_lease(lease) and not (doc["lease_replaces"] is not None
                                                 and _sha(existing) == doc["lease_replaces"]):
        raise ShitError(REJECTED)


def _prove_tail(project, doc, line, entry, lines, chain, task_plan, events):
    """The pending commit must be the next step of the verified chain (or its already-appended last step).

    `lines` and `chain` are the queue files as they would be once a torn tail is dropped. Pure: nothing is written."""
    index = doc["index"]
    if len(lines) not in (index, index + 1) or len(chain) not in (index, index + 1) or len(chain) > len(lines):
        raise ShitError(REJECTED)
    previous, prior = GENESIS, []
    for raw, recorded in zip(lines[:index], chain[:index]):
        previous = chain_hash(previous, raw)
        if raw == b"" or len(raw) > records.MAX_RECORD_BYTES or recorded.decode("ascii", "replace") != previous:
            raise ShitError(REJECTED)
        prior.append(qr.load_entry(raw))
    if (len(lines) == index + 1 and lines[index] != line) or (
            len(chain) == index + 1 and chain[index].decode("ascii", "replace") != doc["chain_line"]):
        raise ShitError(REJECTED)
    tasks = qr.replay(prior)
    after = qr.replay([*prior, entry])
    _prove_anchor_state(project, doc, tasks, after)
    qr.check_effects(doc, entry, tasks.get(entry["task_id"]), after.get(entry["task_id"]))
    removal = _prove_removal(project, doc, entry, tasks)
    life = doc["lifecycle"]
    lifecycle_open = False
    if life is not None:
        lifecycle_open = _prove_lifecycle(project, doc, entry, after, task_plan, events)
    if doc["shards"]:
        _load_graph(project, [*prior, entry], after, {item["task_id"]: item["content"] for item in doc["shards"]})
    return removal, lifecycle_open


def _held(tasks):
    return {t: v["generation"] for t, v in tasks.items() if v["generation"]}


def _prove_anchor_state(project, doc, tasks, after):
    """The anchor's generation marks must equal the replayed state it claims to be at: the prefix, or the commit."""
    anchor = _read_anchor(project)
    index = doc["index"]
    if anchor is None:
        if index != 0 or _held(tasks):
            raise ShitError(REJECTED)
        return
    want = _held(after) if anchor["count"] == index + 1 else _held(tasks)
    if anchor["generations"] != want:
        raise ShitError(REJECTED)


def _prove_removal(project, doc, entry, tasks):
    """A lease to remove must be absent (already removed) or exactly the lease the replay says is live.

    Returns the SHA-256 of the proven file, or None when it is absent. Read-only."""
    task_id = doc["remove_lease"]
    if task_id is None:
        return None
    expected = qr.expected_lease(task_id, tasks[task_id]) if task_id in tasks else None
    path = _lease_path(task_id)
    pair = any(sibling == f"{task_id}.json" for _tmp, sibling in project.publication_pairs(LEASES))
    try:
        data = project.read(path, limit=records.MAX_RECORD_BYTES, links=2 if pair else 1)
    except ShitError as exc:
        if exc.code == PRECONDITION:
            return None
        raise
    if expected is None:
        raise ShitError(REJECTED)
    on_disk = qr.load_lease(data)
    if {k: on_disk.get(k) for k in expected} != expected:
        raise ShitError(REJECTED)
    return _sha(data)


def _prove_lifecycle(project, doc, entry, after, task_plan, events):
    """The lifecycle step must be applicable now: legal, in time order against task and event log, and in agreement."""
    life = doc["lifecycle"]
    for text in (life["reason"], life["blocked_reason"], life["blocking_item"]):
        if text is not None:
            _clean(text)
    if life["to"] not in qr.LIFECYCLE[after[entry["task_id"]]["state"]]:
        raise ShitError(REJECTED)
    current = records.load_task(project.read(task_mod._task_path(life["task_id"])))
    done = {"from": life["from"], "to": life["to"], "actor": life["actor"], "at": life["at"], "reason": life["reason"]}
    if current["state"] == life["to"] and current["history"] and current["history"][-1] == done:
        return False  # already applied
    if current["state"] != life["from"] or life["at"] < current["updated_at"]:
        raise ShitError(REJECTED)
    check_transition(life["from"], life["to"], life["actor"])
    if task_plan:  # the nested task commit must be exactly this lifecycle step
        event = task_plan["event"]
        if (event["event"] != "state_transition" or event["task_id"] != life["task_id"]
                or event["from_state"] != life["from"] or event["to_state"] != life["to"]
                or event["actor"] != life["actor"] or event["timestamp"] != life["at"]):
            raise ShitError(REJECTED)
    if events and life["at"] < events[-1]["timestamp"]:
        raise ShitError(REJECTED)  # the event would precede the tip of the verified event log
    return True


def _refusing(fn, limits=False):
    """Run a validation pass: anything that does not verify is a fail-closed refusal (exit code 8), nothing written.

    With `limits` a size or capacity failure is a refusal too (proving a pending record); elsewhere it keeps exit 7."""
    try:
        return fn()
    except ShitError as exc:
        if exc.code in (REJECTED, ILLEGAL_TRANSITION, HUMAN_APPROVAL, PRECONDITION) or (limits and exc.code == INPUT_LIMIT):
            raise ShitError(FAILED_CLOSED) from None
        raise


def _plan_queue_commit(project, log, task_plan):
    """Prove a pending queue commit before the first write: record, healed tail, anchor, effects, lifecycle."""
    doc, line, entry = _read_pending(project, log)
    index = doc["index"]
    lines, torn = log._split(QUEUE)
    chain, chain_torn = log._split(CHAIN)
    lines = log.healed(lines, torn, index, line + b"\n")
    chain = log.healed(chain, chain_torn, index, doc["chain_line"].encode("ascii") + b"\n")
    _check_anchor_for_pending(project, doc, chain)
    if len(lines) < index or len(chain) < index:
        raise ShitError(REJECTED)
    previous = GENESIS if index == 0 else chain[index - 1].decode("ascii", "replace")
    if chain_hash(previous, line) != doc["chain_line"]:
        raise ShitError(REJECTED)
    if task_plan is not None and doc["lifecycle"] is None:
        raise ShitError(REJECTED)  # a task commit beside a queue commit must be that commit's own lifecycle step
    if task_plan is None:
        events = log.verify()  # the event log is proven too, even when this commit leaves it alone
    else:
        events = log.verify_lines(task_plan["log_lines"][:task_plan["index"]],
                                  log._split(eventlog.CHAIN_PATH)[0][:task_plan["index"]])
    removal, lifecycle_open = _prove_tail(project, doc, line, entry, lines, chain, task_plan, events)
    _check_lease_effect(project, doc)
    _preflight_room(log, doc, line, lines, chain, task_plan, lifecycle_open)
    return doc, line, removal


def _preflight_room(log, doc, line, lines, chain, task_plan, lifecycle_open):
    """Every append the commit still has to make must fit before the first write (a limit failure is a refusal)."""
    index = doc["index"]
    log.check_room(len(line) + 1 if len(lines) == index else 0, CHAIN_LINE_BYTES if len(chain) == index else 0,
                   data=QUEUE, chain=CHAIN)
    if task_plan is not None:
        if len(task_plan["log_lines"]) == task_plan["index"]:
            log.check_room(len(task_plan["line"]) + 1, CHAIN_LINE_BYTES)
    elif lifecycle_open:
        log.check_room(records.MAX_RECORD_BYTES + 1, CHAIN_LINE_BYTES)  # the event is built later: assume the largest


def _prepare_dirs(project):
    """The store directories, and the temp name of each interrupted exclusive publication."""
    project.ensure_dir(LEASES)
    project.ensure_dir(DEPS)
    project.finish_publications(LEASES)


MODES = ("queue", "recover", "writer", "reader")


def _plan(project, log, mode, verified=False):
    """The single recovery planner for both subsystems. Reads only; raises on any refusal.

    It covers a pending task commit, a pending queue commit (the task commit nested in it included) and, when no
    queue commit is pending, the whole queue state, proven against the task image a pending task commit would
    write. `mode` says who asks: a queue operation or the explicit `recover` (every refusal is exit code 8), a task
    or artifact `writer` (always proves the queue state before it commits; the task module's own codes are kept for
    the event log and its own record) or a `reader` (proves only when a pending record forces a write). Returns None
    when a reader has nothing to prove."""
    strict = mode in ("queue", "recover")
    task_plan = None
    if project.exists(task_mod.PENDING):
        task_plan = _refusing(lambda: task_mod._plan_recovery(project, log)) if strict \
            else task_mod._plan_recovery(project, log)
    if project.exists(PENDING):  # only `recover` is ever asked to plan a queue commit
        return {"task": task_plan, "queue": _refusing(lambda: _plan_queue_commit(project, log, task_plan), True),
                "state": None}
    if task_plan is None:
        if mode == "reader":
            return None
        if verified:
            pass  # the caller (`shit check`) verified the event log in this same pass
        elif strict:
            _closed(log.verify)
        else:
            log.verify()  # raw codes, as the writer's own later verification would report them
    overrides = {task_plan["task"]["id"]: task_plan["task"]} if task_plan else None
    return {"task": task_plan, "queue": None, "state": _load(project, log, overrides)}


def _recover(project, log, mode="queue"):
    """Recover an interrupted commit: one planner, then one write path. Returns the verified queue state.

    A pending QUEUE commit is never recovered by an ordinary entry point: any mode but `recover` refuses on sight
    (exit code 8) and writes nothing, so a record that is merely present, genuine or planted, is never applied by
    whichever command happens to run next. Only `recover` (the named operation) plans and applies it.
    Every locked entry point of the task, evidence, report, review, decision, yardstick and queue commands calls
    this first (tests/test_queue.py enumerates them). Write order, all after `plan-proven`, and nothing before it:
      1. the task commit (heal the event and chain tails, append the event and chain line, write the task image,
         remove pending.json), whether or not a queue commit is nested in it;
      2. the queue files' torn-tail rewrites;
      3. the store directories and the removal of a publication temp name (the proof read such a pair as the pair
         it is and a missing directory as an empty one; neither changes any state the proof reads);
      4. the queue commit: lease file, shards, queue and chain appends, anchor, removal of queue-pending.json.
    A refusal in the planner writes nothing and keeps every pending record."""
    if mode not in MODES:
        raise ShitError(USAGE)
    if mode != "recover" and project.exists(PENDING):
        raise ShitError(FAILED_CLOSED)
    strict = mode in ("queue", "recover")
    plan = _refusing(lambda: _plan(project, log, mode)) if strict else _plan(project, log, mode)
    if plan is None:
        return None
    _checkpoint("plan-proven")
    if plan["task"]:
        task_mod._apply_task_plan(project, log, plan["task"])
    if plan["queue"] is None:
        if strict:
            _prepare_dirs(project)
        return plan["state"]
    doc, line, removal = plan["queue"]
    index = doc["index"]
    lines, torn = log._split(QUEUE)
    chain, chain_torn = log._split(CHAIN)
    log.heal_torn_tail(QUEUE, lines, torn, index, line + b"\n")
    log.heal_torn_tail(CHAIN, chain, chain_torn, index, doc["chain_line"].encode("ascii") + b"\n")
    _prepare_dirs(project)
    _closed(lambda: _apply_pending(project, log, doc, line, removal))
    return _load(project, log)


def recover(project, *, now=None, config=None):
    """The one named operation that may finish a pending queue commit. Proves the whole commit read-only first.

    Returns True when a queue commit was finished, False when none was pending. A record it cannot prove stays,
    exit code 8, nothing written; the operator procedure is in docs/queue-spec.md (Q-REC-02)."""
    log = EventLog(project)

    def run():
        with project.locked(LOCK_TIMEOUT, create=True):
            had = project.exists(PENDING)
            _recover(project, log, "recover")
            return had

    return task_mod._guarded(run)


def diagnose(project, events_verified=False):
    """Read-only report for `shit check`: [(exit code, text)] and the queue summary line, or ([], None) when the
    queue has never been used. Takes the store lock without creating anything and never recovers."""
    log = EventLog(project)
    findings = []

    def run():
        with project.locked(LOCK_TIMEOUT):
            had = project.exists(PENDING)
            try:
                plan = _refusing(lambda: _plan(project, log, "recover", events_verified))  # reads only
            except ShitError:
                findings.append((FAILED_CLOSED, "queue pending record cannot be proven; see Q-REC-02" if had
                                 else "queue state does not verify"))
                return None
            if had:
                findings.append((FAILED_CLOSED, "queue pending record present and provable; run queue.recover"))
            return plan["state"]

    state = task_mod._guarded(run)
    if state is not None and state.entries:
        return findings, f"queue: {len(state.entries)} entries, chain, anchor and lifecycle agreement verified"
    return findings, None


def _commit(ctx, fields, *, lease=None, lease_replaces=None, remove_lease=None, lifecycle=None, shards=()):
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
           "lease": lease, "lease_replaces": lease_replaces, "remove_lease": remove_lease, "lifecycle": lifecycle,
           "shards": list(shards)}
    _check_pending_shards(doc["shards"], entry)
    _closed(lambda: _check_lease_effect(ctx.project, doc))  # a conflicting lease file: refuse before anything is written
    ctx.log.check_room(len(line) + 1, CHAIN_LINE_BYTES, data=QUEUE, chain=CHAIN)  # refuse before anything is written
    if lifecycle is not None:
        ctx.log.check_room(records.MAX_RECORD_BYTES + 1, CHAIN_LINE_BYTES)  # the event is built later: assume the largest
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
        self._events = None

    def load(self):
        if self._state is None:
            self._state = _load(self.project, self.log)
        return self._state

    def met(self):
        if self._met is None:
            self._met = _meter(self.project, self.events)
        return self._met

    def events(self):
        """The event log, verified (chain and lifecycle continuity) once per state; a broken log fails closed."""
        if self._events is None:
            self._events = _closed(self.log.verify)
        return self._events

    def invalidate(self):
        self._state = self._met = self._events = None


def _check_clock(ctx, state):
    if state.entries and ctx.now < state.entries[-1]["timestamp"]:
        raise ShitError(FAILED_CLOSED)  # the clock is earlier than a recorded time: no claim, no expiry (Q-LEASE-08)
    events = ctx.events()
    if events and ctx.now < events[-1]["timestamp"]:
        raise ShitError(FAILED_CLOSED)  # the same holds for the verified event log: an event must not precede its tip


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
    for name in ctx.project.list_dir(DEPS):
        if name.startswith(".tmp-"):
            ctx.project.remove(f"{DEPS}/{name}")  # one pass removes every leftover: state is unchanged
    for name in ctx.project.list_dir(LEASES):
        if name.startswith(".tmp-"):
            ctx.project.remove(f"{LEASES}/{name}")
            continue
        task_id = name[:-5] if name.endswith(".json") else ""
        if not records.SLUG.fullmatch(task_id):
            raise ShitError(FAILED_CLOSED)
        queued = state.tasks.get(task_id)
        expected = qr.expected_lease(task_id, queued) if queued else None
        if expected is None:
            observed = _sha(ctx.project.read(f"{LEASES}/{name}", limit=records.MAX_RECORD_BYTES)) \
                if queued is not None else None
            last = state.entries[-1] if state.entries else None
            repeat = (last is not None and last["kind"] == "orphan_removed" and last["task_id"] == task_id
                      and last.get("orphan_sha256") == observed and observed is not None)
            if queued is not None and not repeat:  # the audit entry names the file's digest and commits (cap, chain)
                # before the file goes; a repeat after a crash between the two is skipped only for that same file
                _commit(ctx, {"task_id": task_id, "actor": "wrapper", "kind": "orphan_removed",
                              "state_after": queued["state"], "orphan_sha256": observed})
            ctx.project.remove(f"{LEASES}/{name}")
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
        with project.locked(LOCK_TIMEOUT, create=True):
            ctx = _Ctx(project, log, now, config)
            ctx._state = _recover(project, log)
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


def _check_population(state, owners, extra=()):
    """Queued task ids and shard owners together never exceed the documented task cap."""
    if len({*state.tasks, *owners, *extra}) > dep.MAX_TASKS:
        raise ShitError(INPUT_LIMIT)


def _graph_commit(ctx, state, graph, actor):
    """Write the shards that differ between the loaded graph and `graph`, with one chain entry covering them all."""
    _check_population(state, {edge["task_id"] for edge in graph["edges"]})
    old, new = dep.group_by_task(state.graph["edges"]), dep.group_by_task(graph["edges"])
    changed = sorted(t for t in {*old, *new} if old.get(t) != new.get(t))
    shards, proof = [], []
    for task_id in changed:
        if task_id in new:
            data = dep.dump_shard(task_id, new[task_id])
            shards.append({"task_id": task_id, "content": data.decode("ascii")})
            proof.append({"task_id": task_id, "sha256": _sha(data)})
        else:
            shards.append({"task_id": task_id, "content": None})
            proof.append({"task_id": task_id, "sha256": qr.ABSENT})
    _commit(ctx, {"task_id": changed[0], "actor": actor, "kind": "graph_changed", "shards": proof}, shards=shards)


def add_edges(project, additions, actor, *, now=None, config=None):
    """Add typed edges `(task_id, needs_id, type)` in one commit, or none. Only `human` or `wrapper` may change the
    graph (Q-DEP-07). A cycle or duplicate is exit code 3, an unknown task 9, a cap breach 7."""
    _human_or_wrapper(actor)

    def body(ctx):
        state = ctx.load()
        ends = {end for edge in additions for end in edge[:2]}
        known = {end for end in ends if isinstance(end, str) and records.SLUG.fullmatch(end)
                 and _task_exists(project, end)}
        graph = dep.add_edges(state.graph, additions, known)
        _graph_commit(ctx, state, graph, actor)
        return graph

    return _operate(project, now, config, body)


def add_edge(project, task_id, needs_id, kind, actor, *, now=None, config=None):
    return add_edges(project, [(task_id, needs_id, kind)], actor, now=now, config=config)


def remove_edge(project, task_id, needs_id, kind, actor, *, now=None, config=None):
    _human_or_wrapper(actor)

    def body(ctx):
        state = ctx.load()
        queued = state.tasks.get(task_id)
        graph = dep.remove_edge(state.graph, task_id, needs_id, kind)
        if queued is not None and queued["lease"] is not None:
            raise ShitError(ILLEGAL_TRANSITION)
        _graph_commit(ctx, state, graph, actor)
        return graph

    return _operate(project, now, config, body)


def enqueue(project, task_id, actor, *, now=None, config=None):
    """Put an open task into the queue as `waiting` or `ready`. A task id is enqueued once, ever (Q-RES-01)."""
    _human_or_wrapper(actor)
    _task_id(task_id)

    def body(ctx):
        state = ctx.load()
        if task_id in state.tasks or any(e["task_id"] == task_id and e["kind"] != "graph_changed"
                                         for e in state.entries):
            raise ShitError(REJECTED)
        _check_population(state, {edge["task_id"] for edge in state.graph["edges"]}, (task_id,))
        current = records.load_task(project.read(task_mod._task_path(task_id)))
        if current["state"] != "open":
            raise ShitError(ILLEGAL_TRANSITION)
        unmet = dep.unmet_blocking(state.by_task.get(task_id, ()), task_id, ctx.met())
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
        # STUB (issue 18, OQ-7): for a `draft` step the wrapper will compute `base_sha256` of each declared target
        # here, at claim time, and record it for the later apply step. Nothing is computed or stored yet.
        # Ordering among competing apply steps will use (created_at, task id) only: there is no priority (OQ-9).
        if step == "apply":
            for pre in dep.apply_prerequisites(state.graph["edges"], task_id):
                if not state.tasks.get(pre, {}).get("apply_finished"):
                    raise ShitError(ILLEGAL_TRANSITION)
        lease = {"schema": "lease", "schema_version": 1, "task_id": task_id, "step": step, "worker": worker,
                 "worker_kind": kind, "lease_id": _new_lease_id(), "generation": queued["generation"] + 1,
                 "claimed_at": ctx.now, "expires_at": _plus(ctx.now, ctx.config["lease_ttl_seconds"]), "renewals": 0}
        _commit(ctx, {"task_id": task_id, "actor": "wrapper", "kind": "claimed", "state_after": "dispatched",
                      "lease_id": lease["lease_id"], "generation": lease["generation"], "worker": worker,
                      "worker_kind": kind, "step": step, "expires_at": lease["expires_at"]}, lease=lease)
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
