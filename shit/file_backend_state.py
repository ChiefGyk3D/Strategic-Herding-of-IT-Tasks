"""The bounded catalog of paths the file persistence protocol creates below `.shit`."""

from . import commands, log, queue, task
from .errors import PRECONDITION, ShitError
from .project import LOCK_NAME, SUBDIRS

FILE_BACKEND_ARTIFACTS = (
    LOCK_NAME,
    *SUBDIRS,
    log.LOG_PATH,
    log.CHAIN_PATH,
    task.PENDING,
    *commands.DIRS.values(),
    queue.QUEUE,
    queue.CHAIN,
    queue.DEPS,
    queue.PENDING,
    queue.ANCHOR,
    queue.VIEW,
    queue.LEASES,
)

_SKELETON = {LOCK_NAME, *SUBDIRS}
_DIRECTORIES = {*SUBDIRS, *commands.DIRS.values(), queue.DEPS, queue.LEASES}


def present(project, *, include_bodies=True):
    """Artifact names carrying file-backend state; the empty init skeleton is not state."""
    found = []
    for name in FILE_BACKEND_ARTIFACTS:
        if not include_bodies and name in {*SUBDIRS, *commands.DIRS.values()}:
            continue
        if name in _DIRECTORIES:
            try:
                populated = bool(project.list_dir(name))
            except ShitError as exc:
                if exc.code == PRECONDITION:
                    continue
                raise
            if populated:
                found.append(name)
        elif name not in _SKELETON and project.exists(name):
            found.append(name)
    return found
