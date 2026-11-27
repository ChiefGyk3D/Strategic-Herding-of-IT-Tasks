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
    HUMAN_APPROVAL,
    INTERNAL,
    REJECTED,
    USAGE,
    ShitError,
)
from .lifecycle import LEGAL, check_transition
from .log import GENESIS, EventLog, Prepared, chain_hash

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


def _recover(project, log):
    if not project.exists(PENDING):
        return
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
    log_lines, chain_lines = log._raw()
    if len(chain_lines) < index or len(log_lines) < index:
        raise ShitError(REJECTED)
    previous = GENESIS if index == 0 else chain_lines[index - 1].decode("ascii", "replace")
    if chain_hash(previous, line) != chain_line:
        raise ShitError(REJECTED)
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
            event = _event(log, task_id, actor, "task_created", "Task record created.", now)
            _commit(project, log, record, event)
            return record

    return _guarded(run)


def transition(project, task_id, to, actor, reason, *, now=None, blocked_reason=None, blocking_item=None,
               precheck=None):
    now = now or _now()
    path = _task_path(task_id)
    log = EventLog(project)

    def run():
        with project.locked():
            _recover(project, log)
            current = records.load_task(project.read(path))
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

    return _guarded(run)
