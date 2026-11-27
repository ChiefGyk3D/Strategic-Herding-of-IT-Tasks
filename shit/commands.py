"""Task, evidence, report, review, decision and close commands as a library API.

Every command runs under the store lock. A record file is written first and its event is appended
second; the event carries the sha256 of the record bytes. A reader trusts a record only when a
matching event exists, so a file dropped on disk, edited afterwards or left behind by a crash is never
treated as recorded. A retry of the same id after a crash completes the missing event.

Only a human may record an accepted decision (exit 10), and `close` needs a successful report, a
passing review, evidence that still matches its recorded digest, and an accepted human decision.
Actor strings are provenance labels, not authentication (docs/threat-model.md, residual risks).
"""

import errno
import hashlib
import os
import stat
from datetime import UTC, datetime

from . import artifacts, records
from . import task as task_mod
from ._text import check_text
from .errors import (
    CONFINEMENT,
    HUMAN_APPROVAL,
    ILLEGAL_TRANSITION,
    INPUT_LIMIT,
    INTERNAL,
    PRECONDITION,
    REJECTED,
    USAGE,
    ContractError,
    ShitError,
)
from .log import EventLog

MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
DIRS = {"evidence": "evidence", "report": "reports", "review": "reviews", "decision": "decisions"}
EVENT = {"evidence": "evidence_recorded", "report": "report_recorded", "review": "review_recorded",
         "decision": "decision_recorded"}
LABEL = {"evidence": "Evidence", "report": "Report", "review": "Review", "decision": "Decision"}


def _fault():
    """Fault-injection seam between writing a record file and appending its event."""


def _now():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value):
    try:
        check_text(value)
    except ContractError as exc:
        raise artifacts.convert(exc) from None
    return value


# -- evidence files ---------------------------------------------------------


def _rel_parts(rel):
    if not isinstance(rel, str):
        raise ShitError(REJECTED)
    if len(rel) > artifacts.MAX_PATH:
        raise ShitError(INPUT_LIMIT)
    if rel == "" or "\x00" in rel or rel.startswith("/"):
        raise ShitError(CONFINEMENT)
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or parts[0] == ".shit":
        raise ShitError(CONFINEMENT)
    return parts


def _os_code(exc):
    if exc.errno == errno.ENOENT:
        return PRECONDITION
    if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO, errno.EMLINK):
        return CONFINEMENT
    return INTERNAL


def hash_file(repo, rel):
    """Return (sha256 hex, size) of a regular, single-link file under `repo`; no symlink on the way."""
    parts = _rel_parts(rel)
    cloexec = os.O_CLOEXEC
    fds = []
    try:
        fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY | cloexec)
        fds.append(fd)
        for part in parts[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | cloexec, dir_fd=fd)
            fds.append(fd)
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | cloexec, dir_fd=fd)
        fds.append(leaf)
        st = os.fstat(leaf)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise ShitError(CONFINEMENT)
        digest, total = hashlib.sha256(), 0
        while True:
            chunk = os.read(leaf, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_EVIDENCE_BYTES:
                raise ShitError(INPUT_LIMIT)
            digest.update(chunk)
        return digest.hexdigest(), total
    except OSError as exc:
        raise ShitError(_os_code(exc)) from None
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


# -- loading recorded records -----------------------------------------------


def _record_path(kind, rid):
    if not records.SLUG.fullmatch(rid if isinstance(rid, str) else ""):
        raise ShitError(REJECTED)
    return f"{DIRS[kind]}/{rid}.json"


def _find_event(events, kind, rid):
    for event in events:
        if event["event"] == EVENT[kind] and event["record_id"] == rid:
            return event
    return None


def _digest_message(kind, data):
    return f"{LABEL[kind]} record added; sha256 {hashlib.sha256(data).hexdigest()}."


def _load(project, events, kind, rid, task_id=None):
    """Load a record that has a matching event and an unchanged digest, else raise 9 or 3."""
    path = _record_path(kind, rid)
    event = _find_event(events, kind, rid)
    if event is None:
        raise ShitError(PRECONDITION)
    data = project.read(path, limit=records.MAX_RECORD_BYTES)
    rec = artifacts.load(kind, data)
    if (event["message"] != _digest_message(kind, data) or rec["task_id"] != event["task_id"]
            or rec["actor"] != event["actor"] or rec["id"] != rid):
        raise ShitError(REJECTED)
    if task_id is not None and rec["task_id"] != task_id:
        raise ShitError(PRECONDITION)
    return rec


def _for_task(events, kind, task_id):
    return [e["record_id"] for e in events if e["event"] == EVENT[kind] and e["task_id"] == task_id]


def show(project, kind, rid):
    if kind not in DIRS:
        raise ShitError(USAGE)
    log = EventLog(project)

    def run():
        with project.locked():
            task_mod._recover(project, log, "reader")
            return _load(project, log.verify(), kind, rid)

    return task_mod._guarded(run)


# -- adding records ---------------------------------------------------------


def _add(project, kind, record, check):
    data = artifacts.dump(kind, record)
    task_id, rid = record["task_id"], record["id"]
    path = _record_path(kind, rid)
    log = EventLog(project)

    def run():
        with project.locked(create=True):
            task_mod._recover(project, log)
            current = records.load_task(project.read(task_mod._task_path(task_id)))
            if current["state"] == "closed":
                raise ShitError(ILLEGAL_TRANSITION)
            events = log.verify()
            project.ensure_dir(DIRS[kind])
            body = data
            if project.exists(path):
                if _find_event(events, kind, rid) is not None:
                    raise ShitError(USAGE)
                # A crash left the file without its event: complete it from the file on disk.
                body = project.read(path, limit=records.MAX_RECORD_BYTES)
                existing = artifacts.load(kind, body)
                if (existing["task_id"], existing["actor"]) != (task_id, record["actor"]):
                    raise ShitError(REJECTED)
                record_now = existing
            else:
                record_now = record
            check(current, events, record_now)
            if body is data:
                project.write_atomic(path, data)
            _fault()
            event = task_mod._event(log, task_id, record["actor"], EVENT[kind], _digest_message(kind, body),
                                    record["created_at"], record_id=rid)
            log.append(event)
            return record_now

    return task_mod._guarded(run)


def _base(kind, rid, task_id, actor, now, **fields):
    return {"schema": kind, "schema_version": 1, "id": rid, "task_id": task_id, "created_at": now or _now(),
            "actor": actor, **fields}


def new_task(project, *, task_id, title, summary, owner, actor, now=None):
    _text(title)
    _text(summary)
    return task_mod.create_task(project, task_id=task_id, title=title, summary=summary, owner=owner,
                                actor=actor, now=now)


def add_evidence(project, *, task_id, evidence_id, path, description, actor, now=None):
    _rel_parts(path)
    _text(description)
    digest, size = hash_file(project.repo, path)
    record = _base("evidence", evidence_id, task_id, actor, now, description=description, path=path,
                   sha256=digest, size=size)
    return _add(project, "evidence", record, lambda current, events, rec: None)


def add_report(project, *, task_id, report_id, outcome, summary, evidence_ids, actor, log_reference=None,
               now=None):
    extra = {} if log_reference is None else {"log_reference": log_reference}
    record = _base("report", report_id, task_id, actor, now, outcome=outcome, summary=summary,
                   evidence_ids=list(evidence_ids), **extra)

    def check(current, events, rec):
        for eid in rec["evidence_ids"]:
            _load(project, events, "evidence", eid, task_id)

    return _add(project, "report", record, check)


def add_review(project, *, task_id, review_id, report_id, verdict, summary, actor, now=None):
    record = _base("review", review_id, task_id, actor, now, report_id=report_id, verdict=verdict,
                   summary=summary)

    def check(current, events, rec):
        if current["state"] != "review":
            raise ShitError(ILLEGAL_TRANSITION)
        _load(project, events, "report", rec["report_id"], task_id)

    return _add(project, "review", record, check)


def add_decision(project, *, task_id, decision_id, status, question, choice, rationale, actor,
                 supersedes_id=None, now=None):
    extra = {} if supersedes_id is None else {"supersedes_id": supersedes_id}
    record = _base("decision", decision_id, task_id, actor, now, status=status, question=question,
                   choice=choice, rationale=rationale, **extra)
    artifacts.validate("decision", record)
    if status == "accepted" and actor != "human":
        raise ShitError(HUMAN_APPROVAL)

    def check(current, events, rec):
        if "supersedes_id" in rec:
            _load(project, events, "decision", rec["supersedes_id"], task_id)

    return _add(project, "decision", record, check)


# -- closing ----------------------------------------------------------------


def _check_closable(project, log, task_id):
    events = log.verify()
    loaded = {kind: [_load(project, events, kind, rid, task_id) for rid in _for_task(events, kind, task_id)]
              for kind in KINDS_ORDER}
    reports = {r["id"]: r for r in loaded["report"]}
    if not any(r["outcome"] == "success" for r in reports.values()):
        raise ShitError(PRECONDITION)
    if not loaded["review"] or loaded["review"][-1]["verdict"] != "pass" or loaded["review"][-1]["actor"] != "human":
        raise ShitError(PRECONDITION)
    reviewed = reports.get(loaded["review"][-1]["report_id"])
    if reviewed is None or reviewed["outcome"] != "success":
        raise ShitError(PRECONDITION)
    by_id = {e["id"]: e for e in loaded["evidence"]}
    for eid in reviewed["evidence_ids"]:
        ev = by_id.get(eid)
        if ev is None:
            raise ShitError(PRECONDITION)
        if hash_file(project.repo, ev["path"]) != (ev["sha256"], ev["size"]):
            raise ShitError(REJECTED)
    superseded = {d["supersedes_id"] for d in loaded["decision"] if "supersedes_id" in d}
    effective = [d for d in loaded["decision"] if d["id"] not in superseded and d["status"] in ("accepted", "rejected")]
    if not effective or effective[-1]["status"] != "accepted" or effective[-1]["actor"] != "human":
        raise ShitError(HUMAN_APPROVAL)


KINDS_ORDER = ("evidence", "report", "review", "decision")


def close_task(project, task_id, actor, reason, *, now=None):
    log = EventLog(project)

    def precheck(current):
        _check_closable(project, log, task_id)

    return task_mod.transition(project, task_id, "closed", actor, reason, now=now, precheck=precheck)
