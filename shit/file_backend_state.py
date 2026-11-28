"""Bounded catalogs of backend artifacts below `.shit`."""

from . import audit_export, commands, log, queue, store, task
from .errors import PRECONDITION, ShitError
from .project import LOCK_NAME, SUBDIRS

FILE_ONLY_ARTIFACTS = (
    log.LOG_PATH,
    log.CHAIN_PATH,
    task.PENDING,
    queue.QUEUE,
    queue.CHAIN,
    queue.DEPS,
    queue.LEGACY_GRAPH,
    queue.PENDING,
    queue.ANCHOR,
    queue.VIEW,
    queue.LEASES,
)
SHARED_ARTIFACTS = (LOCK_NAME, *SUBDIRS, *commands.DIRS.values())
SQLITE_ONLY_ARTIFACTS = (store.DB_NAME, audit_export.EXPORT_PATH)

# Compatibility for callers that display or test the file-state catalog.
FILE_BACKEND_ARTIFACTS = FILE_ONLY_ARTIFACTS

_DIRECTORIES = {queue.DEPS, queue.LEASES}


def present(project):
    """File-protocol-only artifact names; artifacts used by another backend are not file state."""
    found = []
    for name in FILE_ONLY_ARTIFACTS:
        if name in _DIRECTORIES:
            try:
                populated = bool(project.list_dir(name))
            except ShitError as exc:
                if exc.code == PRECONDITION:
                    continue
                raise
            if populated:
                found.append(name)
        elif project.exists(name):
            found.append(name)
    return found
