"""Queue records and the pure replay of the queue log (docs/queue-spec.md).

`queue-entry` and `lease` records follow the common rules of docs/schemas.md. `replay` rebuilds every
task's queue state from the entries and rejects any sequence the transition table does not allow, a
lease that does not match, a generation that does not rise by one, or a clock that goes backwards.
No file is read here; `queue.py` does the storage.
"""

import json
import re
from datetime import UTC, datetime

from . import records
from .errors import INPUT_LIMIT, REJECTED, ShitError

STATES = ("waiting", "ready", "dispatched", "running", "review", "hold-owner", "hold-ci", "failed", "done")
STEPS = ("draft", "apply", "review")
WORKER_KINDS = ("ai", "script", "human")
HOLD_KINDS = ("owner", "ci")
LEASE_ID = re.compile(r"[0-9a-f]{32}")
HEX64 = re.compile(r"[0-9a-f]{64}")
MAX_MESSAGE = 500
STAMP = "%Y-%m-%dT%H:%M:%SZ"
# Global hard ceilings, the top of each configuration range. Replay and loading enforce them for every lease,
# whatever configuration the entry was written under.
MAX_TTL = 86400
MAX_LIFETIME = 604800
MAX_RENEWALS = 1000

HOLDS = ("hold-owner", "hold-ci")

# Queue state -> lifecycle states it may sit in (docs/queue-spec.md, "Queue states").
LIFECYCLE = {
    "waiting": ("open",), "ready": ("open",), "dispatched": ("open",), "running": ("running",),
    "review": ("review", "approved"), "hold-owner": ("blocked",), "hold-ci": ("blocked",),
    "failed": ("failed",), "done": ("closed",),
}

# "Allowed queue transitions" of the spec, exactly. Every other pair, including a repeat, is illegal.
ALLOWED = frozenset({
    ("waiting", "ready"), ("ready", "waiting"), ("ready", "dispatched"),
    ("dispatched", "ready"), ("dispatched", "running"), ("dispatched", "hold-owner"),
    ("dispatched", "hold-ci"), ("dispatched", "failed"),
    ("running", "review"), ("running", "hold-owner"), ("running", "hold-ci"), ("running", "failed"),
    ("review", "ready"), ("review", "hold-owner"), ("review", "hold-ci"), ("review", "failed"),
    ("review", "done"),
    ("hold-owner", "ready"), ("hold-ci", "ready"), ("hold-owner", "running"), ("hold-ci", "running"),
    ("hold-owner", "failed"), ("hold-ci", "failed"),
    ("failed", "ready"),
})
# A task re-entering `ready` from review, a hold or failed lands in `waiting` when an edge is unmet.
REENTER_WAITING = frozenset({("review", "waiting"), ("hold-owner", "waiting"), ("hold-ci", "waiting"),
                             ("failed", "waiting")})

HW = ("human", "wrapper")
W = ("wrapper",)

_COMMON = {"schema", "schema_version", "id", "task_id", "timestamp", "actor", "kind", "state_after", "message"}
_LEASE = {"lease_id", "generation"}

# kind -> (required extra fields, optional extra fields, allowed actors, allowed state_after)
KINDS = {
    "enqueued": (set(), set(), HW, ("waiting", "ready")),
    "ready": (set(), set(), W, ("ready",)),
    "claimed": (_LEASE | {"worker", "worker_kind", "step", "expires_at"}, set(), W, ("dispatched",)),
    "started": (_LEASE | {"worker", "step", "attempt"}, set(), W, ("running",)),
    "renewed": (_LEASE | {"worker", "step", "expires_at"}, set(), W, ("dispatched", "running")),
    "released": (_LEASE, set(), W, ("ready",)),
    "expired": (_LEASE, set(), W, ("ready", "failed")),
    "dispatch_failed": (_LEASE | {"not_before"}, set(), W, ("ready",)),
    "apply_started": (_LEASE, set(), W, ("running",)),
    "apply_finished": (_LEASE, set(), W, ("running",)),
    "submitted": (_LEASE, set(), W, ("review",)),
    "held": ({"hold_kind"}, _LEASE, HW, HOLDS),
    "failed": (set(), _LEASE, HW, ("failed",)),
    "reopened": (set(), set(), ("human",), ("ready", "waiting")),
    "done": (set(), set(), HW, ("done",)),
    "orphan_removed": (set(), {"lease_id"}, W, STATES),
    # A change to the dependency graph. It carries no queue state: a task need not be enqueued to have edges.
    "graph_changed": ({"shards"}, set(), HW, ()),
}
ABSENT = "0" * 64  # the digest recorded for a shard that was deleted
MAX_SHARDS_PER_ENTRY = 32

CONFIG_RANGES = {
    "lease_ttl_seconds": (60, 86400),
    "lease_max_seconds": (60, 604800),
    "lease_max_renewals": (0, 1000),
    "max_attempts": (1, 20),
    "max_dispatch_failures": (1, 20),
    "dispatch_backoff_seconds": (1, 3600),
}
CONFIG_DEFAULTS = {
    "lease_ttl_seconds": 900, "lease_max_seconds": 14400, "lease_max_renewals": 100,
    "max_attempts": 3, "max_dispatch_failures": 3, "dispatch_backoff_seconds": 30,
}


def reject():
    return ShitError(REJECTED)


def _parse(stamp):
    return datetime.strptime(stamp, STAMP).replace(tzinfo=UTC)


def _seconds(later, earlier):
    """Whole seconds from `earlier` to `later`, both fixed-width UTC stamps."""
    return int((_parse(later) - _parse(earlier)).total_seconds())


def make_config(**overrides):
    """Validate configuration values against their ranges (docs/queue-spec.md, "Configuration values")."""
    unknown = set(overrides) - set(CONFIG_DEFAULTS)
    if unknown:
        raise reject()
    config = {**CONFIG_DEFAULTS, **overrides}
    for key, (low, high) in CONFIG_RANGES.items():
        value = config[key]
        if type(value) is not int or not low <= value <= high:
            raise reject()
    if config["lease_max_seconds"] < config["lease_ttl_seconds"]:
        raise reject()
    return config


# -- entries -----------------------------------------------------------------


def validate_entry(obj):
    if not isinstance(obj, dict):
        raise reject()
    kind = obj.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise reject()
    required, optional, actors, states = KINDS[kind]
    base = _COMMON - {"message"}
    if kind == "graph_changed":
        base = base - {"state_after"}
    records._keys(obj, base | required, optional | {"message"})
    records._version(obj, "queue-entry")
    records._slug(obj, "id")
    records._slug(obj, "task_id")
    records._stamp(obj, "timestamp")
    records._choice(obj, "actor", actors)
    if kind == "graph_changed":
        _validate_shards(obj)
    else:
        records._choice(obj, "state_after", states)
    if "message" in obj:
        records._text(obj, "message", MAX_MESSAGE)
    if "lease_id" in obj and (not isinstance(obj["lease_id"], str) or not LEASE_ID.fullmatch(obj["lease_id"])):
        raise reject()
    if "generation" in obj and (type(obj["generation"]) is not int or obj["generation"] < 1):
        raise reject()
    if kind in ("held", "failed") and ("lease_id" in obj) != ("generation" in obj):
        raise reject()
    if "worker" in obj:
        records._slug(obj, "worker")
    if "step" in obj:
        records._choice(obj, "step", STEPS)
    if "worker_kind" in obj:
        records._choice(obj, "worker_kind", WORKER_KINDS)
    for key in ("expires_at", "not_before"):
        if key in obj:
            records._stamp(obj, key)
    if "attempt" in obj and (type(obj["attempt"]) is not int or obj["attempt"] < 1):
        raise reject()
    if kind == "held":
        records._choice(obj, "hold_kind", HOLD_KINDS)
        if f"hold-{obj['hold_kind']}" != obj["state_after"]:
            raise reject()
    return obj


def _validate_shards(obj):
    shards = obj["shards"]
    if not isinstance(shards, list) or not 1 <= len(shards) <= MAX_SHARDS_PER_ENTRY:
        raise reject()
    ids = []
    for item in shards:
        if not isinstance(item, dict):
            raise reject()
        records._keys(item, {"task_id", "sha256"}, set())
        ids.append(records._slug(item, "task_id"))
        if not isinstance(item["sha256"], str) or not HEX64.fullmatch(item["sha256"]):
            raise reject()
    if ids != sorted(set(ids)) or obj["task_id"] != ids[0]:
        raise reject()


def graph_digests(entries):
    """{dependent task id: sha256 of its shard, or ABSENT} as proven by the chain, latest entry wins."""
    out = {}
    for entry in entries:
        if entry["kind"] == "graph_changed":
            for item in entry["shards"]:
                out[item["task_id"]] = item["sha256"]
    return out


def load_entry(data):
    return validate_entry(records.parse(data))


def dump_entry(obj):
    """One JSON Lines line without the trailing newline."""
    validate_entry(obj)
    data = json.dumps(obj, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    if len(data) > records.MAX_RECORD_BYTES:
        raise ShitError(INPUT_LIMIT)
    return data


# -- leases ------------------------------------------------------------------

LEASE_KEYS = {"schema", "schema_version", "task_id", "step", "worker", "worker_kind", "lease_id", "generation",
              "claimed_at", "expires_at", "renewals"}


def validate_lease(obj):
    if not isinstance(obj, dict):
        raise reject()
    records._keys(obj, LEASE_KEYS, set())
    records._version(obj, "lease")
    records._slug(obj, "task_id")
    records._choice(obj, "step", STEPS)
    records._slug(obj, "worker")
    records._choice(obj, "worker_kind", WORKER_KINDS)
    if not isinstance(obj["lease_id"], str) or not LEASE_ID.fullmatch(obj["lease_id"]):
        raise reject()
    if type(obj["generation"]) is not int or obj["generation"] < 1:
        raise reject()
    if type(obj["renewals"]) is not int or obj["renewals"] < 0:
        raise reject()
    claimed, expires = records._stamp(obj, "claimed_at"), records._stamp(obj, "expires_at")
    if expires <= claimed or _seconds(obj["expires_at"], obj["claimed_at"]) > MAX_LIFETIME or obj["renewals"] > MAX_RENEWALS:
        raise reject()
    return obj


def load_lease(data):
    return validate_lease(records.parse(data))


def dump_lease(obj):
    validate_lease(obj)
    data = (json.dumps(obj, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
    if len(data) > records.MAX_RECORD_BYTES:
        raise ShitError(INPUT_LIMIT)
    return data


# -- replay ------------------------------------------------------------------


def _new_task(state):
    return {"state": state, "generation": 0, "lease": None, "attempts": 0, "not_before": None,
            "apply_finished": False, "apply_phase": None, "last_failure": None, "last_stamp": None}


def _lease_matches(task, entry):
    lease = task["lease"]
    return (lease is not None and lease["lease_id"] == entry["lease_id"]
            and lease["generation"] == entry["generation"])


def _step(task, entry, before):
    """Apply one entry to `task`, raising when the sequence is not allowed. `before` is the prior state."""
    kind, after, stamp = entry["kind"], entry["state_after"], entry["timestamp"]
    pair = (before, after)
    if kind in ("renewed", "apply_started", "apply_finished", "orphan_removed"):
        if kind == "orphan_removed":
            if after != before:
                raise reject()
            return
        if not _lease_matches(task, entry) or before not in ("dispatched", "running") or after != before:
            raise reject()
        if kind == "renewed":
            lease = task["lease"]
            if entry["worker"] != lease["worker"] or entry["step"] != lease["step"] or entry["expires_at"] <= lease["expires_at"]:
                raise reject()
            if (stamp >= lease["expires_at"] or _seconds(entry["expires_at"], stamp) > MAX_TTL
                    or _seconds(entry["expires_at"], lease["expires_at"]) > MAX_TTL
                    or _seconds(entry["expires_at"], lease["claimed_at"]) > MAX_LIFETIME
                    or lease["renewals"] + 1 > MAX_RENEWALS):
                raise reject()
            lease["expires_at"] = entry["expires_at"]
            lease["renewals"] += 1
        elif before != "running" or task["lease"]["step"] != "apply":
            raise reject()
        elif kind == "apply_started":
            if task["apply_phase"] is not None:
                raise reject()
            task["apply_phase"] = "started"
        else:
            if task["apply_phase"] != "started":
                raise reject()
            task["apply_phase"] = "finished"
            task["apply_finished"] = True
        return
    reenter = kind == "reopened" and pair in REENTER_WAITING
    if pair not in ALLOWED and not reenter:
        raise reject()
    if kind == "ready":
        if pair != ("waiting", "ready"):
            raise reject()
    elif kind == "claimed":
        if entry["generation"] != task["generation"] + 1 or task["lease"] is not None or entry["expires_at"] <= stamp:
            raise reject()
        if _seconds(entry["expires_at"], stamp) > MAX_TTL:
            raise reject()
        if task["not_before"] is not None and stamp < task["not_before"]:
            raise reject()
        task["generation"] = entry["generation"]
        task["apply_phase"] = None  # every attempt starts without an apply marker
        task["apply_finished"] = False
        task["lease"] = {"lease_id": entry["lease_id"], "generation": entry["generation"], "worker": entry["worker"],
                         "worker_kind": entry["worker_kind"], "step": entry["step"], "claimed_at": stamp,
                         "expires_at": entry["expires_at"], "renewals": 0}
    elif kind == "started":
        if before != "dispatched" or not _lease_matches(task, entry) or entry["attempt"] != task["attempts"] + 1:
            raise reject()
        if entry["worker"] != task["lease"]["worker"] or entry["step"] != task["lease"]["step"]:
            raise reject()
        task["attempts"] = entry["attempt"]
        task["not_before"] = None
    elif kind in ("released", "dispatch_failed", "expired", "submitted"):
        _lease_ends(kind, task, entry, before)
        if kind == "dispatch_failed":
            task["not_before"] = entry["not_before"]
    elif kind in ("held", "failed"):
        leased = before in ("dispatched", "running")
        if leased != ("lease_id" in entry) or (leased and not _lease_matches(task, entry)):
            raise reject()
        if kind == "failed":
            task["last_failure"] = stamp
        task["lease"] = None
    elif kind in ("reopened", "done") and task["lease"] is not None:
        raise reject()
    task["state"] = after


def _lease_ends(kind, task, entry, before):
    if not _lease_matches(task, entry):
        raise reject()
    if kind == "released" and before != "dispatched":
        raise reject()
    if kind == "dispatch_failed" and before != "dispatched":
        raise reject()
    if kind == "submitted" and before != "running":
        raise reject()
    if kind == "expired":
        if (before, entry["state_after"]) not in (("dispatched", "ready"), ("running", "failed")):
            raise reject()
        if entry["state_after"] == "failed":
            task["last_failure"] = entry["timestamp"]
    task["lease"] = None


def replay(entries):
    """Rebuild per-task queue state from validated entries; raise exit code 3 on any impossible sequence."""
    tasks, previous = {}, ""
    for number, entry in enumerate(entries, start=1):
        if entry["id"] != f"queue-{number:04d}" or entry["timestamp"] < previous:
            raise reject()
        previous = entry["timestamp"]
        if entry["kind"] == "graph_changed":
            continue  # graph changes carry no queue state; `graph_digests` reads them
        task_id = entry["task_id"]
        task = tasks.get(task_id)
        if entry["kind"] == "enqueued":
            if task is not None:
                raise reject()  # a task id is never reused (Q-RES-01)
            tasks[task_id] = _new_task(entry["state_after"])
            tasks[task_id]["last_stamp"] = entry["timestamp"]
            continue
        if task is None:
            raise reject()
        _step(task, entry, task["state"])
        task["last_stamp"] = entry["timestamp"]
    return tasks


def expected_lease(task_id, task):
    """The lease fields the chain proves for `task_id`, or None when the chain says no lease is live."""
    lease = task["lease"]
    if lease is None:
        return None
    return {"task_id": task_id, **lease}


# Side effects a pending commit may carry, by entry kind. `life` lists permitted (from, to, actor) lifecycle steps;
# actor "entry" means the entry's own actor. Every other effect (lease file, lease removal, shards) is derived from the kind.
_NONE = {"life": (), "lease": None, "remove": False}
_LEAVE = (("running", "", "wrapper"), ("open", "", "wrapper"))
EFFECTS = {
    "claimed": {**_NONE, "lease": "new"},
    "renewed": {**_NONE, "lease": "renew"},
    "started": {**_NONE, "life": (("open", "running", "wrapper"),)},
    "released": {**_NONE, "remove": True},
    "expired": {**_NONE, "remove": True, "life": (("running", "failed", "wrapper"),)},
    "submitted": {**_NONE, "remove": True, "life": (("running", "review", "wrapper"),)},
    "failed": {**_NONE, "remove": True, "life": tuple((a, "failed", c) for a, _, c in _LEAVE)},
    "held": {**_NONE, "remove": True, "life": tuple((a, "blocked", c) for a, _, c in _LEAVE)},
    "reopened": {**_NONE, "life": tuple((a, "open", "entry") for a in ("review", "failed", "blocked"))},
    "done": {**_NONE, "life": (("approved", "closed", "entry"),)},
}


REASONS = {"started": "worker started", "submitted": "work submitted", "failed": "worker reported failure",
           "held": "blocker identified", "expired": "lease expired"}  # fixed wrapper text; reopen and done carry the caller's


def check_effects(doc, entry, before=None, after=None):
    """Raise unless the lease, lifecycle and removal effects of pending `doc` are exactly those `entry` implies.

    `before` and `after` are the replayed queue state of the task before and after the entry, when known. With
    `after`, a lease effect must equal, field for field, the lease the replay says the entry produces."""
    spec = EFFECTS.get(entry["kind"], _NONE)
    task_id = entry["task_id"]
    life, lease = doc["lifecycle"], doc["lease"]
    if (doc["remove_lease"] is not None) != spec["remove"] or (spec["remove"] and doc["remove_lease"] != task_id):
        raise reject()
    if (lease is None) != (spec["lease"] is None) or (doc["lease_replaces"] is not None) != (spec["lease"] == "renew"):
        raise reject()
    if lease is not None:
        same = ("task_id", "lease_id", "generation", "expires_at") if spec["lease"] == "renew" else (
            "task_id", "lease_id", "generation", "expires_at", "step", "worker", "worker_kind")
        if any(lease[k] != entry.get(k, task_id if k == "task_id" else None) for k in same):
            raise reject()
        if spec["lease"] == "new" and (lease["claimed_at"] != entry["timestamp"] or lease["renewals"] != 0):
            raise reject()
    if after is not None and lease is not None:
        expected = expected_lease(task_id, after)
        if expected is None or {k: lease.get(k) for k in expected} != expected:
            raise reject()
    allowed = spec["life"]
    if entry["kind"] == "expired" and entry["state_after"] != "failed":
        allowed = ()
    if (life is None) != (not allowed):
        raise reject()
    if life is None:
        return
    actor_ok = {(a, b, entry["actor"] if c == "entry" else c) for a, b, c in allowed}
    if (life["from"], life["to"], life["actor"]) not in actor_ok or life["at"] != entry["timestamp"]:
        raise reject()
    if (life["to"] == "blocked") != (life["blocked_reason"] is not None and life["blocking_item"] is not None):
        raise reject()
    if entry["kind"] in REASONS and life["reason"] != REASONS[entry["kind"]]:
        raise reject()
    if before is not None and life["from"] not in LIFECYCLE[before["state"]]:
        raise reject()


def check_queue_transition(before, after):
    """True when the spec's transition table allows `before` to `after` (a repeat is never allowed)."""
    return (before, after) in ALLOWED
