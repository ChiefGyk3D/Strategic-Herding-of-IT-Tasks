"""Tasks, events and records on the database store, with the signatures of `task.py` and `commands.py` (issue 61).

This is the store adapter layer: a caller that used `task.create_task`, `task.transition` or one of the `commands`
functions finds the same name and arguments here and gets the same result and the same exit codes.

A record BODY is still a file under `.shit/`, written to its final name before the database transaction and compared
with its digest in the records index after it. The index row, the event and (for a task) the state change are one
`store` write transaction, so a crash leaves either all of them or only a body file with no index row. A repeated
call adopts such a body: the bytes on disk win, as in the file protocol. A task body holds the task as created; its
state, owner, history and times come from the database, and `get_task` joins the two.

`store.py` is the only module that opens the database, so every database call here goes through its helpers.
"""

import hashlib
import os
import stat

from . import artifacts, audit_export, commands, records, store
from . import task as task_mod
from .errors import (
    CONFINEMENT,
    HUMAN_APPROVAL,
    ILLEGAL_TRANSITION,
    PRECONDITION,
    REJECTED,
    USAGE,
    ShitError,
)
from .project import bounded_names

_TASK_DIR = "tasks"
_MAX_BODY = records.MAX_RECORD_BYTES
_now = commands._now


def _fault(stage):
    """Fault-injection seam: tests replace it to stop between the body file and the transaction, or after the commit."""


# -- paths, bodies and the store -------------------------------------------------------------------------------


def _directory(kind):
    return _TASK_DIR if kind == "task" else commands.DIRS[kind]


def _body_path(kind, record_id):
    if not records.SLUG.fullmatch(record_id if isinstance(record_id, str) else ""):
        raise ShitError(REJECTED)
    return f"{_directory(kind)}/{record_id}.json"


def _connect(project, create, busy_timeout_ms, *, write=False):
    """Open the store. A writer first makes the audit export current (or refuses a restored database, exit code 8);
    a reader refuses only a database that is provably not the one the export was made from (docs/audit-export.md)."""
    if create and not project.exists(store.DB_NAME):
        store.init_store(project, busy_timeout_ms)
    conn = store.connect(project, busy_timeout_ms)
    try:
        if write:
            audit_export.sync(project, conn)
        else:
            audit_export.guard(project, conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _digest(data):
    return hashlib.sha256(data).hexdigest(), len(data)


def _publish(project, kind, record_id, data):
    """Write `data` to the body's final name unless a body is already there. Returns the bytes now on disk.

    The temp-and-link publication is atomic (a body appears complete or not at all) and nothing already on disk is
    overwritten, so two callers that race for one name cannot both win. No lock is taken: a crash between the link
    and the removal of the temp name leaves a second link on the body, which every read then refuses (exit code 5);
    that record id is unusable until the extra `.tmp-` name is removed by hand, and another id works."""
    path = _body_path(kind, record_id)
    project.ensure_dir(_directory(kind))
    if project.create_exclusive(path, data, limit=_MAX_BODY):
        return data
    return project.read(path, limit=_MAX_BODY)


def _read_body(project, kind, record_id, index):
    """The bytes of a body, only if they match the digest the index holds."""
    data = project.read(_body_path(kind, record_id), limit=_MAX_BODY)
    if _digest(data) != (index["sha256"], index["size"]):
        raise ShitError(REJECTED)
    return data


def _check_published(project, conn, kind, record_id):
    """After the commit: the file on disk is still the file the index describes."""
    index = store.get_record(conn, kind, record_id)
    if index is None:
        raise ShitError(PRECONDITION)
    _read_body(project, kind, record_id, index)


def verify_bodies(project, conn, *, include_orphans=False):
    """Read-only: names of the problems between the records index and the body files.

    `body_missing` (no file under a recorded name), `body_unreadable` (a symlink, a second link or a special file there)
    and `body_differs_from_index` (bytes that do not match the digest). With `include_orphans`, the bounded directory
    scan also reports `body_orphan` and `body_unexpected_name`; it uses lstat before any body is opened."""
    found = set()
    rows = store.list_records(conn) if conn is not None else []
    if include_orphans:
        indexed = {(row["kind"], row["record_id"]) for row in rows}
        for kind in ("task", *commands.DIRS):
            try:
                fd = project._walk((_directory(kind),))
            except ShitError as exc:
                if exc.code == PRECONDITION:
                    continue
                raise
            try:
                for name in bounded_names(fd):
                    try:
                        entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except OSError:
                        found.add("body_unreadable")
                        continue
                    if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                        found.add("body_unreadable")
                        continue
                    if not name.endswith(".json"):
                        continue  # workflow files (for example reports/<id>.md) can share these directories
                    record_id = name[:-5]
                    if not records.SLUG.fullmatch(record_id):
                        found.add("body_unexpected_name")
                    elif (kind, record_id) not in indexed:
                        found.add("body_orphan")
            finally:
                os.close(fd)
    for row in rows:
        try:
            data = project.read(_body_path(row["kind"], row["record_id"]), limit=_MAX_BODY)
        except ShitError as exc:
            if exc.code == PRECONDITION:
                found.add("body_missing")
            elif exc.code == CONFINEMENT:
                found.add("body_unreadable")  # a symlink, a second link, a special file
            else:
                raise
            continue
        if _digest(data) != (row["sha256"], row["size"]):
            found.add("body_differs_from_index")
    return sorted(found)


# -- tasks -----------------------------------------------------------------------------------------------------


def _assemble(project, conn, task_id):
    """The task as the file protocol held it: the body as created, joined with the state and history in the database."""
    with store.read_snapshot(conn):
        row = store.get_task(conn, task_id)
        index = store.get_record(conn, "task", task_id)
    if row is None or index is None:
        raise ShitError(PRECONDITION)
    body = records.parse(_read_body(project, "task", task_id, index))
    task = {key: value for key, value in body.items() if key not in ("blocked_reason", "blocking_item")}
    task.update(state=row["state"], owner=row["owner"], created_at=row["created_at"], updated_at=row["updated_at"],
                history=row["history"])
    for key in ("blocked_reason", "blocking_item"):
        if key in row:
            task[key] = row[key]
    return records.validate_task(task)


def _adopt_task(existing, record):
    """A task body a crash left without an index row is adopted when it is the task being created, else USAGE."""
    try:
        found = records.load_task(existing)
    except ShitError:
        raise ShitError(USAGE) from None
    same = ("id", "title", "summary", "owner")
    if (any(found[key] != record[key] for key in same) or found["state"] != "open" or found["history"]
            or found["created_at"] != found["updated_at"] or "blocked_reason" in found):
        raise ShitError(USAGE)
    return found


def create_task(project, *, task_id, title, summary, owner, actor, now=None, busy_timeout_ms=None):
    now = now or _now()
    record = {
        "schema": "task", "schema_version": 1, "id": task_id, "title": title, "summary": summary,
        "state": "open", "owner": owner, "created_at": now, "updated_at": now, "history": [],
    }
    records.validate_task(record)
    data = records.dump_task(record)

    def run():
        conn = _connect(project, True, busy_timeout_ms, write=True)
        try:
            if store.get_task(conn, task_id) is not None:
                raise ShitError(USAGE)
            existing = _publish(project, "task", task_id, data)
            image = record if existing == data else _adopt_task(existing, record)
            sha256, size = _digest(existing)
            _fault("body-published")
            store.create_task(conn, image, actor, sha256, size)
            audit_export.after_commit(project, conn)
            _fault("committed")
            _check_published(project, conn, "task", task_id)
            return image
        finally:
            conn.close()

    return task_mod._guarded(run)


def new_task(project, *, task_id, title, summary, owner, actor, now=None, busy_timeout_ms=None):
    commands._text(title)
    commands._text(summary)
    return create_task(project, task_id=task_id, title=title, summary=summary, owner=owner, actor=actor, now=now,
                       busy_timeout_ms=busy_timeout_ms)


def get_task(project, task_id, *, busy_timeout_ms=None):
    _body_path("task", task_id)

    def run():
        conn = _connect(project, False, busy_timeout_ms)
        try:
            return _assemble(project, conn, task_id)
        finally:
            conn.close()

    return task_mod._guarded(run)


def transition(project, task_id, to, actor, reason, *, now=None, blocked_reason=None, blocking_item=None,
               precheck=None, busy_timeout_ms=None):
    """As `task.transition`. `precheck(current)` gets the task row from the database (state, owner, times), inside
    the transaction; the title and summary are not read for it."""
    now = now or _now()
    _body_path("task", task_id)

    def run():
        conn = _connect(project, False, busy_timeout_ms, write=True)
        try:
            store.transition_task(conn, task_id, to, actor, reason, now, blocked_reason=blocked_reason,
                                  blocking_item=blocking_item, precheck=precheck)
            audit_export.after_commit(project, conn)
            return _assemble(project, conn, task_id)
        finally:
            conn.close()

    return task_mod._guarded(run)


# -- records ---------------------------------------------------------------------------------------------------


def _load(project, conn, kind, record_id, task_id=None):
    """Load a record whose body matches its index digest and whose event agrees, else raise 9 or 3."""
    index = store.get_record(conn, kind, record_id)
    event = store.record_event(conn, kind, record_id)
    if index is None or event is None:
        raise ShitError(PRECONDITION)
    record = artifacts.load(kind, _read_body(project, kind, record_id, index))
    if (event["message"] != store.record_message(kind, index["sha256"]) or record["task_id"] != index["task_id"]
            or record["task_id"] != event["task_id"] or record["actor"] != event["actor"] or record["id"] != record_id):
        raise ShitError(REJECTED)
    if task_id is not None and record["task_id"] != task_id:
        raise ShitError(PRECONDITION)
    return record


def show(project, kind, record_id, *, busy_timeout_ms=None):
    if kind not in commands.DIRS:
        raise ShitError(USAGE)
    _body_path(kind, record_id)

    def run():
        conn = _connect(project, False, busy_timeout_ms)
        try:
            with store.read_snapshot(conn):
                return _load(project, conn, kind, record_id)
        finally:
            conn.close()

    return task_mod._guarded(run)


def _add(project, kind, record, check, busy_timeout_ms):
    data = artifacts.dump(kind, record)
    task_id, record_id = record["task_id"], record["id"]
    _body_path(kind, record_id)

    def run():
        conn = _connect(project, False, busy_timeout_ms, write=True)
        try:
            task = store.get_task(conn, task_id)
            if task is None:
                raise ShitError(PRECONDITION)
            if task["state"] == "closed":
                raise ShitError(ILLEGAL_TRANSITION)
            if store.get_record(conn, kind, record_id) is not None:
                raise ShitError(USAGE)
            existing = _publish(project, kind, record_id, data)
            if _digest(existing) != _digest(data):
                raise ShitError(REJECTED)
            current = artifacts.load(kind, existing)
            sha256, size = _digest(existing)
            _fault("body-published")
            store.add_record(conn, kind, current, sha256, size, check=lambda row: check(conn, row, current))
            audit_export.after_commit(project, conn)
            _fault("committed")
            _check_published(project, conn, kind, record_id)
            return current
        finally:
            conn.close()

    return task_mod._guarded(run)


def add_evidence(project, *, task_id, evidence_id, path, description, actor, now=None, busy_timeout_ms=None):
    commands._rel_parts(path)
    commands._text(description)
    digest, size = commands.hash_file(project.repo, path)
    record = commands._base("evidence", evidence_id, task_id, actor, now, description=description, path=path,
                            sha256=digest, size=size)
    return _add(project, "evidence", record, lambda conn, task, rec: None, busy_timeout_ms)


def add_report(project, *, task_id, report_id, outcome, summary, evidence_ids, actor, log_reference=None, now=None,
               busy_timeout_ms=None):
    extra = {} if log_reference is None else {"log_reference": log_reference}
    record = commands._base("report", report_id, task_id, actor, now, outcome=outcome, summary=summary,
                            evidence_ids=list(evidence_ids), **extra)

    def check(conn, task, rec):
        for evidence_id in rec["evidence_ids"]:
            _load(project, conn, "evidence", evidence_id, task_id)

    return _add(project, "report", record, check, busy_timeout_ms)


def add_review(project, *, task_id, review_id, report_id, verdict, summary, actor, now=None, busy_timeout_ms=None):
    record = commands._base("review", review_id, task_id, actor, now, report_id=report_id, verdict=verdict,
                            summary=summary)

    def check(conn, task, rec):
        if task["state"] != "review":
            raise ShitError(ILLEGAL_TRANSITION)
        _load(project, conn, "report", rec["report_id"], task_id)

    return _add(project, "review", record, check, busy_timeout_ms)


def add_decision(project, *, task_id, decision_id, status, question, choice, rationale, actor, supersedes_id=None,
                 now=None, busy_timeout_ms=None):
    extra = {} if supersedes_id is None else {"supersedes_id": supersedes_id}
    record = commands._base("decision", decision_id, task_id, actor, now, status=status, question=question,
                            choice=choice, rationale=rationale, **extra)
    artifacts.validate("decision", record)
    if status == "accepted" and actor != "human":
        raise ShitError(HUMAN_APPROVAL)

    def check(conn, task, rec):
        if "supersedes_id" in rec:
            _load(project, conn, "decision", rec["supersedes_id"], task_id)

    return _add(project, "decision", record, check, busy_timeout_ms)


# -- closing ---------------------------------------------------------------------------------------------------


def _check_closable(project, conn, task_id):
    loaded = {kind: [_load(project, conn, kind, rid, task_id) for rid in store.record_ids(conn, kind, task_id)]
              for kind in commands.KINDS_ORDER}
    reports = {r["id"]: r for r in loaded["report"]}
    if not any(r["outcome"] == "success" for r in reports.values()):
        raise ShitError(PRECONDITION)
    if not loaded["review"] or loaded["review"][-1]["verdict"] != "pass" or loaded["review"][-1]["actor"] != "human":
        raise ShitError(PRECONDITION)
    reviewed = reports.get(loaded["review"][-1]["report_id"])
    if reviewed is None or reviewed["outcome"] != "success":
        raise ShitError(PRECONDITION)
    by_id = {e["id"]: e for e in loaded["evidence"]}
    for evidence_id in reviewed["evidence_ids"]:
        evidence = by_id.get(evidence_id)
        if evidence is None:
            raise ShitError(PRECONDITION)
        if commands.hash_file(project.repo, evidence["path"]) != (evidence["sha256"], evidence["size"]):
            raise ShitError(REJECTED)
    superseded = {d["supersedes_id"] for d in loaded["decision"] if "supersedes_id" in d}
    effective = [d for d in loaded["decision"] if d["id"] not in superseded and d["status"] in ("accepted", "rejected")]
    if not effective or effective[-1]["status"] != "accepted" or effective[-1]["actor"] != "human":
        raise ShitError(HUMAN_APPROVAL)


def close_task(project, task_id, actor, reason, *, now=None, busy_timeout_ms=None):
    """As `commands.close_task`: the checks run inside the closing transaction, so nothing can change between them."""
    now = now or _now()
    _body_path("task", task_id)

    def run():
        conn = _connect(project, False, busy_timeout_ms, write=True)
        try:
            store.transition_task(conn, task_id, "closed", actor, reason, now,
                                  precheck=lambda row: _check_closable(project, conn, task_id))
            audit_export.after_commit(project, conn)
            return _assemble(project, conn, task_id)
        finally:
            conn.close()

    return task_mod._guarded(run)
