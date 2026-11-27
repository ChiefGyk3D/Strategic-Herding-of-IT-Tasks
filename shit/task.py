"""Task creation and lifecycle transitions, committed together with their event as one recoverable step.

A mutation takes the store lock, writes `pending.json` (the commit point), then applies the event,
the chain entry and the task file, and removes `pending.json`. Each apply step is idempotent, so a
crash at any stage is finished by the next locked operation (`_recover`) and never leaves a task
without its event or an event without its task.
"""

import json
import re
from datetime import UTC, datetime

from . import records
from .errors import (
    FAILED_CLOSED,
    HUMAN_APPROVAL,
    INTERNAL,
    REJECTED,
    USAGE,
    ShitError,
)
from .lifecycle import LEGAL, check_transition
from .log import (
    CHAIN_PATH,
    GENESIS,
    LOG_PATH,
    EventLog,
    Prepared,
    _check_lifecycle,
    chain_hash,
)

PENDING = "pending.json"
PENDING_LIMIT = 4 * records.MAX_RECORD_BYTES
HEX64 = re.compile(r"[0-9a-f]{64}")


def _checkpoint(stage):
    """Fault-injection seam: tests replace it to interrupt a commit between stages."""


def _now():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _task_path(task_id):
    if not records.SLUG.fullmatch(task_id if isinstance(task_id, str) else ""):
        raise ShitError(REJECTED)
    return f"tasks/{task_id}.json"


def _guarded(fn):
    """Run `fn`, turning an unexpected OS error into the fixed internal-error code."""
    try:
        return fn()
    except ShitError:
        raise
    except OSError:
        raise ShitError(INTERNAL) from None


# -- recovery ---------------------------------------------------------------


QUEUE_PENDING = "queue-pending.json"  # owned by shit/queue.py


def _plan_recovery(project, log):
    """Validate an interrupted commit without writing anything. Returns the plan, or None when nothing is pending."""
    if not project.exists(PENDING):
        return None
    data = project.read(PENDING, limit=PENDING_LIMIT)
    try:
        doc = json.loads(data.decode("utf-8"), object_pairs_hook=records._pairs)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ShitError(REJECTED) from None
    if not isinstance(doc, dict) or set(doc) != {"index", "event_line", "chain_line", "task"}:
        raise ShitError(REJECTED)
    index, event_line, chain_line = doc["index"], doc["event_line"], doc["chain_line"]
    if type(index) is not int or index < 0 or not isinstance(event_line, str) or not isinstance(chain_line, str):
        raise ShitError(REJECTED)
    if not HEX64.fullmatch(chain_line) or not isinstance(doc["task"], dict):
        raise ShitError(REJECTED)
    task = records.validate_task(doc["task"])
    try:
        line = event_line.encode("ascii")
    except UnicodeEncodeError:
        raise ShitError(REJECTED) from None
    event = records.load_event(line)
    if event["task_id"] != task["id"]:
        raise ShitError(REJECTED)
    log_lines, log_torn = log._split(LOG_PATH)
    chain_lines, chain_torn = log._split(CHAIN_PATH)
    log_lines = log.healed(log_lines, log_torn, index, line + b"\n")
    chain_lines = log.healed(chain_lines, chain_torn, index, chain_line.encode("ascii") + b"\n")
    if len(log_lines) not in (index, index + 1) or len(chain_lines) not in (index, index + 1) \
            or len(chain_lines) > len(log_lines):
        raise ShitError(REJECTED)
    events = log.verify_lines(log_lines[:index], chain_lines[:index])  # the whole existing prefix and its chain
    previous = GENESIS if index == 0 else chain_lines[index - 1].decode("ascii", "replace")
    if chain_hash(previous, line) != chain_line:
        raise ShitError(REJECTED)
    if (len(log_lines) == index + 1 and log_lines[index] != line) or (
            len(chain_lines) == index + 1 and chain_lines[index].decode("ascii", "replace") != chain_line):
        raise ShitError(REJECTED)
    if event["id"] != f"event-{index + 1:04d}" or any(e["id"] == event["id"] for e in events):
        raise ShitError(REJECTED)
    _check_lifecycle([*events, event])  # the candidate is the legal next event of the verified prefix
    _prove_image(project, event, task)
    return {"index": index, "line": line, "chain_line": chain_line, "task": task, "event": event,
            "log_lines": log_lines, "torn": (log_torn, chain_torn)}


def _prove_image(project, event, image):
    """The pending task image must be exactly what the event does to the task file as it is now (or already is)."""
    path = _task_path(image["id"])
    exists = project.exists(path)
    current = records.load_task(project.read(path)) if exists else None
    if current is not None and records.dump_task(current) == records.dump_task(image):
        applied = True
    else:
        applied = False
    kind = event["event"]
    if kind == "task_created":
        if (current is not None and not applied) or image["state"] != "open" or image["history"] \
                or not image["created_at"] == image["updated_at"] == event["timestamp"]:
            raise ShitError(REJECTED)
        return
    if kind != "state_transition" or not image["history"]:
        raise ShitError(REJECTED)
    step = image["history"][-1]
    if (step["from"] != event["from_state"] or step["to"] != event["to_state"] or step["actor"] != event["actor"]
            or step["at"] != event["timestamp"] or image["state"] != event["to_state"]
            or image["updated_at"] != event["timestamp"]):
        raise ShitError(REJECTED)
    if applied:
        return
    if current is None or current["state"] != event["from_state"]:
        raise ShitError(REJECTED)
    check_transition(event["from_state"], event["to_state"], event["actor"])
    fixed = ("blocked_reason", "blocking_item", "state", "updated_at", "history")
    if {k: v for k, v in current.items() if k not in fixed} != {k: v for k, v in image.items() if k not in fixed}:
        raise ShitError(REJECTED)
    if image["history"][:-1] != current["history"] or event["timestamp"] < current["updated_at"]:
        raise ShitError(REJECTED)


def _recover(project, log, plan=None, *, queue_pending_ok=False):
    """Finish an interrupted commit. Every check runs before the first write (`_plan_recovery`).

    A queue commit that is still pending also holds back every event writer: its lifecycle event has to be
    proven against the event log as it is now, so the queue recovers first (exit code 8 until it does)."""
    if not queue_pending_ok and project.exists(QUEUE_PENDING):
        raise ShitError(FAILED_CLOSED)
    plan = plan or _plan_recovery(project, log)
    if plan is None:
        return
    index, line, chain_line, task = plan["index"], plan["line"], plan["chain_line"], plan["task"]
    log_lines, log_torn = log._split(LOG_PATH)
    chain_lines, chain_torn = log._split(CHAIN_PATH)
    log.heal_torn_tail(LOG_PATH, log_lines, log_torn, index, line + b"\n")
    log.heal_torn_tail(CHAIN_PATH, chain_lines, chain_torn, index, chain_line.encode("ascii") + b"\n")
    log.apply(Prepared(index, line, chain_line))
    project.write_atomic(_task_path(task["id"]), records.dump_task(task))
    project.remove(PENDING)
    log.verify()


def _commit(project, log, task, event):
    """Validate, then apply task and event together. `project` must hold the lock."""
    _checkpoint("start")
    prepared = log.prepare(event)
    task_bytes = records.dump_task(task)
    pending = {
        "index": prepared.index,
        "event_line": prepared.line.decode("ascii"),
        "chain_line": prepared.chain_line,
        "task": task,
    }
    project.write_atomic(PENDING, json.dumps(pending, ensure_ascii=True).encode("ascii"), limit=PENDING_LIMIT)
    _checkpoint("pending-written")
    log.apply(prepared, lambda stage: _checkpoint(stage))
    project.write_atomic(_task_path(task["id"]), task_bytes)
    _checkpoint("task-written")
    project.remove(PENDING)


def _event(log, task_id, actor, kind, message, now, **extra):
    number = len(log.verify()) + 1
    return {
        "schema": "event-log", "schema_version": 1, "id": f"event-{number:04d}", "task_id": task_id,
        "timestamp": now, "actor": actor, "event": kind, "message": message, "record_id": task_id, **extra,
    }


# -- public operations ------------------------------------------------------


def get_task(project, task_id):
    path = _task_path(task_id)
    log = EventLog(project)

    def run():
        with project.locked():
            _recover(project, log)
            return records.load_task(project.read(path))

    return _guarded(run)


def get_task_locked(project, task_id):
    """Read a task while the caller already holds the store lock and has run recovery."""
    return records.load_task(project.read(_task_path(task_id)))


def create_task(project, *, task_id, title, summary, owner, actor, now=None):
    now = now or _now()
    record = {
        "schema": "task", "schema_version": 1, "id": task_id, "title": title, "summary": summary,
        "state": "open", "owner": owner, "created_at": now, "updated_at": now, "history": [],
    }
    records.validate_task(record)
    path = _task_path(task_id)
    log = EventLog(project)

    def run():
        with project.locked():
            _recover(project, log)
            if project.exists(path):
                raise ShitError(USAGE)
            if any(e["task_id"] == task_id for e in log.verify()):
                raise ShitError(REJECTED)  # a task id is never reused, even if its file is gone (Q-RES-01)
            event = _event(log, task_id, actor, "task_created", "Task record created.", now)
            _commit(project, log, record, event)
            return record

    return _guarded(run)


def _transition_locked(project, log, task_id, to, actor, reason, now, blocked_reason, blocking_item, precheck):
    """The body of `transition`; `project` must hold the lock and recovery must already have run."""
    current = records.load_task(project.read(_task_path(task_id)))
    frm = current["state"]
    if to == "approved" and actor != "human" and (frm, to) in LEGAL:
        raise ShitError(HUMAN_APPROVAL)
    check_transition(frm, to, actor)
    if precheck is not None:
        precheck(current)
    if now < current["updated_at"]:
        raise ShitError(REJECTED)
    record = {k: v for k, v in current.items() if k not in ("blocked_reason", "blocking_item")}
    if to == "blocked":
        if not blocked_reason or not blocking_item:
            raise ShitError(REJECTED)
        record["blocked_reason"], record["blocking_item"] = blocked_reason, blocking_item
    elif blocked_reason is not None or blocking_item is not None:
        raise ShitError(REJECTED)
    record["state"] = to
    record["updated_at"] = now
    record["history"] = [*current["history"], {"from": frm, "to": to, "actor": actor, "at": now, "reason": reason}]
    records.validate_task(record)
    event = _event(log, task_id, actor, "state_transition", "State transition recorded.", now,
                   from_state=frm, to_state=to)
    _commit(project, log, record, event)
    return record


def transition(project, task_id, to, actor, reason, *, now=None, blocked_reason=None, blocking_item=None,
               precheck=None):
    now = now or _now()
    _task_path(task_id)
    log = EventLog(project)

    def run():
        with project.locked():
            _recover(project, log)
            return _transition_locked(project, log, task_id, to, actor, reason, now, blocked_reason,
                                      blocking_item, precheck)

    return _guarded(run)
