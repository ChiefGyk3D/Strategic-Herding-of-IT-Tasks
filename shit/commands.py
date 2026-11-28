"""Shared record-body helpers and compatibility names for the database store."""

import hashlib
import os
import stat
from datetime import UTC, datetime

from ._text import check_text
from .errors import CONFINEMENT, INPUT_LIMIT, REJECTED, ContractError, ShitError

MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
DIRS = {"evidence": "evidence", "report": "reports", "review": "reviews", "decision": "decisions"}
EVENT = {"evidence": "evidence_recorded", "report": "report_recorded", "review": "review_recorded",
         "decision": "decision_recorded"}
KINDS_ORDER = ("evidence", "report", "review", "decision")


def _now():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value):
    if not isinstance(value, str) or not value:
        raise ShitError(REJECTED)
    try:
        check_text(value)
    except ContractError:
        raise ShitError(REJECTED) from None
    return value


def _rel_parts(rel):
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel or "\x00" in rel:
        raise ShitError(CONFINEMENT)
    if len(rel) > 500:
        raise ShitError(INPUT_LIMIT)
    parts = rel.split("/")
    if parts[0] == ".shit" or any(not part or part in (".", "..") for part in parts):
        raise ShitError(CONFINEMENT)
    for part in parts:
        _text(part)
    return parts


def hash_file(repo, rel):
    parts = _rel_parts(rel)
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        file_fd = os.open(parts[-1], flags, dir_fd=fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(file_fd)
            raise ShitError(CONFINEMENT)
    except FileNotFoundError:
        raise ShitError(9) from None
    except OSError:
        raise ShitError(CONFINEMENT) from None
    finally:
        os.close(fd)
    digest, size = hashlib.sha256(), 0
    try:
        with os.fdopen(file_fd, "rb") as stream:
            while block := stream.read(65536):
                size += len(block)
                if size > MAX_EVIDENCE_BYTES:
                    raise ShitError(INPUT_LIMIT)
                digest.update(block)
    except OSError:
        raise ShitError(CONFINEMENT) from None
    return digest.hexdigest(), size


def _base(kind, rid, task_id, actor, now, **fields):
    return {"schema": kind, "schema_version": 1, "id": rid, "task_id": task_id,
            "created_at": now or _now(), "actor": actor, **fields}


def new_task(*args, **kwargs):
    from .store_tasks import new_task as operation
    return operation(*args, **kwargs)


def show(*args, **kwargs):
    from .store_tasks import show as operation
    return operation(*args, **kwargs)


def add_evidence(*args, **kwargs):
    from .store_tasks import add_evidence as operation
    return operation(*args, **kwargs)


def add_report(*args, **kwargs):
    from .store_tasks import add_report as operation
    return operation(*args, **kwargs)


def add_review(*args, **kwargs):
    from .store_tasks import add_review as operation
    return operation(*args, **kwargs)


def add_decision(*args, **kwargs):
    from .store_tasks import add_decision as operation
    return operation(*args, **kwargs)


def close_task(*args, **kwargs):
    from .store_tasks import close_task as operation
    return operation(*args, **kwargs)
