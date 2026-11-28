"""Compatibility names for task operations on the database store."""

from datetime import UTC, datetime

from .errors import INTERNAL, ShitError

PENDING = "pending.json"


def _now():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _guarded(fn):
    try:
        return fn()
    except ShitError:
        raise
    except OSError:
        raise ShitError(INTERNAL) from None


def get_task(*args, **kwargs):
    from .store_tasks import get_task as operation
    return operation(*args, **kwargs)


def create_task(*args, **kwargs):
    from .store_tasks import create_task as operation
    return operation(*args, **kwargs)


def transition(*args, **kwargs):
    from .store_tasks import transition as operation
    return operation(*args, **kwargs)
