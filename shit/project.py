"""Confined project storage under `<repo>/.shit/` (docs/threat-model.md, file confinement).

Every access walks the path one component at a time from a directory descriptor with O_NOFOLLOW,
then re-checks the opened descriptor with fstat. A symlink, a hard link, a FIFO, a socket or a
device is refused with exit code 5. Writes replace the file atomically in the same directory.
"""

import contextlib
import errno
import fcntl
import os
import secrets
import stat
import time
from pathlib import Path

from .errors import CONFINEMENT, INPUT_LIMIT, INTERNAL, PRECONDITION, ShitError

STORE_NAME = ".shit"
SUBDIRS = ("tasks",)
DEFAULT_LIMIT = 65536
DIR_MODE = 0o700
FILE_MODE = 0o600
LOCK_NAME = "lock"

_NOFOLLOW = os.O_NOFOLLOW
_CLOEXEC = os.O_CLOEXEC


def _is_repo_dir(path):
    """True when `path` is a real, user-owned git directory: not a symlink, with HEAD, objects and refs."""
    try:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            return False
        head = os.lstat(os.path.join(path, "HEAD"))
        return (stat.S_ISREG(head.st_mode)
                and all(stat.S_ISDIR(os.lstat(os.path.join(path, name)).st_mode) for name in ("objects", "refs")))
    except OSError:
        return False


def _gitdir_pointer(candidate, git):
    """Read a `.git` file that must hold exactly one `gitdir: <path>` line naming a regular, user-owned file."""
    try:
        fd = os.open(git, os.O_RDONLY | _NOFOLLOW | _CLOEXEC)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > 4096:
                raise ShitError(CONFINEMENT)
            text = os.read(fd, 4097).decode("utf-8")
        finally:
            os.close(fd)
    except (OSError, UnicodeDecodeError):
        raise ShitError(CONFINEMENT) from None
    if not text.endswith("\n") or text.count("\n") != 1 or not text.startswith("gitdir: "):
        raise ShitError(CONFINEMENT)
    target = text[len("gitdir: "):-1]
    if not os.path.isabs(target):
        target = os.path.join(candidate, target)
    return os.path.normpath(target)


def _repo_marker(candidate):
    """Return False when `candidate` has no `.git` entry, True for a real repository, else ShitError(5).

    A planted file, empty directory, symlink, foreign-owned entry or a gitdir pointer to a non-repository
    fails closed instead of being walked past.
    """
    git = os.path.join(candidate, ".git")
    try:
        st = os.lstat(git)
    except FileNotFoundError:
        return False
    except OSError:
        raise ShitError(CONFINEMENT) from None
    if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid():
        raise ShitError(CONFINEMENT)
    if stat.S_ISDIR(st.st_mode):
        gitdir = git
    elif stat.S_ISREG(st.st_mode):
        gitdir = _gitdir_pointer(candidate, git)
    else:
        raise ShitError(CONFINEMENT)
    if not _is_repo_dir(gitdir):
        raise ShitError(CONFINEMENT)
    return True


def discover_root(start, ceiling=None):
    """Return the nearest directory at or above `start` that holds a real repository marker.

    The walk never goes above `ceiling` when one is given.
    """
    here = Path(os.path.realpath(start))
    top = Path(os.path.realpath(ceiling)) if ceiling is not None else None
    for candidate in (here, *here.parents):
        if _repo_marker(str(candidate)):
            return candidate
        if candidate == top:
            break
    raise ShitError(PRECONDITION)


def _os_error(exc):
    """Map an OSError to a stable code without carrying its text."""
    if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO, errno.EMLINK):
        return ShitError(CONFINEMENT)
    if exc.errno == errno.ENOENT:
        return ShitError(PRECONDITION)
    return ShitError(INTERNAL)


class Project:
    def __init__(self, repo_path, store_fd):
        self.repo = repo_path
        self._root = os.path.realpath(os.path.join(repo_path, STORE_NAME))
        self._fd = store_fd
        self._lock_fd = None

    # -- creation and opening -------------------------------------------------

    @classmethod
    def _open_store(cls, repo, create):
        repo = Path(os.path.realpath(repo))
        try:
            repo_fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC)
        except OSError as exc:
            raise _os_error(exc) from None
        try:
            if create:
                try:
                    os.mkdir(STORE_NAME, DIR_MODE, dir_fd=repo_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise _os_error(exc) from None
            try:
                fd = os.open(STORE_NAME, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=repo_fd)
            except OSError as exc:
                raise _os_error(exc) from None
        finally:
            os.close(repo_fd)
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            os.close(fd)
            raise ShitError(CONFINEMENT)
        return cls(repo, fd)

    @classmethod
    def init(cls, repo):
        """Create `.shit/` and its subdirectories, forcing mode 0700 on each (existing ones too)."""
        proj = cls._open_store(repo, create=True)
        try:
            os.fchmod(proj._fd, stat.S_IRWXU)  # == DIR_MODE (0o700)
            for sub in SUBDIRS:
                try:
                    os.mkdir(sub, DIR_MODE, dir_fd=proj._fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise _os_error(exc) from None
                fd = proj._walk((sub,))
                try:
                    os.fchmod(fd, stat.S_IRWXU)
                except OSError as exc:
                    raise _os_error(exc) from None
                finally:
                    proj._close(fd)
        except BaseException:
            proj.close()
            raise
        return proj

    @classmethod
    def open(cls, repo):
        return cls._open_store(repo, create=False)

    def close(self):
        if self._lock_fd is not None:
            self._close(self._lock_fd)
            self._lock_fd = None
        if self._fd is not None:
            self._close(self._fd)
            self._fd = None

    @staticmethod
    def _close(fd):
        with contextlib.suppress(OSError):
            os.close(fd)

    # -- path handling --------------------------------------------------------

    def _split(self, rel):
        """Split a store-relative path into safe components, or raise ShitError(5)."""
        if not isinstance(rel, str) or rel == "" or "\x00" in rel or rel.startswith("/"):
            raise ShitError(CONFINEMENT)
        parts = rel.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ShitError(CONFINEMENT)
        # Defense in depth: the lexical path must also resolve inside the real store root.
        resolved = os.path.realpath(os.path.join(self._root, *parts))
        if os.path.commonpath([self._root, resolved]) != self._root:
            raise ShitError(CONFINEMENT)
        return parts

    def _walk(self, dir_parts):
        """Open the directory named by `dir_parts` below the store, refusing symlinks. Caller closes."""
        fd = os.dup(self._fd)
        try:
            for part in dir_parts:
                try:
                    nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=fd)
                except OSError as exc:
                    raise _os_error(exc) from None
                os.close(fd)
                fd = nxt
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _stat_leaf(self, dirfd, name):
        """lstat the leaf: None when absent, else its stat result after type and link checks."""
        try:
            st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _os_error(exc) from None
        self._check_regular(st)
        return st

    @staticmethod
    def _check_regular(st):
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise ShitError(CONFINEMENT)

    # -- reading and writing --------------------------------------------------

    def read(self, rel, limit=DEFAULT_LIMIT):
        parts = self._split(rel)
        dirfd = self._walk(parts[:-1])
        try:
            before = self._stat_leaf(dirfd, parts[-1])
            if before is None:
                raise ShitError(PRECONDITION)
            try:
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW | _CLOEXEC, dir_fd=dirfd)
            except OSError as exc:
                raise _os_error(exc) from None
        finally:
            self._close(dirfd)
        try:
            after = os.fstat(fd)
            self._check_regular(after)
            if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                raise ShitError(CONFINEMENT)
            chunks, total = [], 0
            while True:
                chunk = os.read(fd, min(65536, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > limit:
                    raise ShitError(INPUT_LIMIT)
            return b"".join(chunks)
        except OSError:
            raise ShitError(INTERNAL) from None
        finally:
            self._close(fd)

    def write_atomic(self, rel, data, limit=DEFAULT_LIMIT):
        """Replace `rel` with `data` via a temp file in the same directory (fsync then rename)."""
        parts = self._split(rel)
        if len(data) > limit:
            raise ShitError(INPUT_LIMIT)
        dirfd = self._walk(parts[:-1])
        tmp = f".tmp-{secrets.token_hex(8)}"
        created = False
        try:
            self._stat_leaf(dirfd, parts[-1])
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, FILE_MODE, dir_fd=dirfd)
            except OSError as exc:
                raise _os_error(exc) from None
            created = True
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                self._close(fd)
            os.rename(tmp, parts[-1], src_dir_fd=dirfd, dst_dir_fd=dirfd)
            created = False
            os.fsync(dirfd)
        except ShitError:
            raise
        except OSError:
            raise ShitError(INTERNAL) from None
        finally:
            if created:
                with contextlib.suppress(OSError):
                    os.unlink(tmp, dir_fd=dirfd)
            self._close(dirfd)

    def append(self, rel, data):
        """Append `data` to a regular single-link file, creating it with mode 0600."""
        parts = self._split(rel)
        dirfd = self._walk(parts[:-1])
        try:
            self._stat_leaf(dirfd, parts[-1])
            try:
                fd = os.open(parts[-1], os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | _NOFOLLOW | _CLOEXEC,
                             FILE_MODE, dir_fd=dirfd)
            except OSError as exc:
                raise _os_error(exc) from None
            try:
                self._check_regular(os.fstat(fd))
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            except OSError:
                raise ShitError(INTERNAL) from None
            finally:
                self._close(fd)
        finally:
            self._close(dirfd)

    def ensure_dir(self, name):
        """Create the single-component directory `name` below the store (mode 0700) if it is absent."""
        parts = self._split(name)
        if len(parts) != 1:
            raise ShitError(CONFINEMENT)
        try:
            os.mkdir(name, DIR_MODE, dir_fd=self._fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise _os_error(exc) from None
        self._close(self._walk(parts))

    def list_dir(self, name):
        """Return the sorted entry names of the single-component directory `name` below the store."""
        parts = self._split(name)
        if len(parts) != 1:
            raise ShitError(CONFINEMENT)
        fd = self._walk(parts)
        try:
            return sorted(os.listdir(fd))
        except OSError:
            raise ShitError(INTERNAL) from None
        finally:
            self._close(fd)

    def create_exclusive(self, rel, data, limit=DEFAULT_LIMIT):
        """Create `rel` with `data` only if it does not exist; return False when something is already there.

        The content is written to a temp file and hard-linked into place, so the file appears complete or
        not at all, and an existing entry (even a symlink) is never opened or followed.
        """
        parts = self._split(rel)
        if len(data) > limit:
            raise ShitError(INPUT_LIMIT)
        dirfd = self._walk(parts[:-1])
        tmp = f".tmp-{secrets.token_hex(8)}"
        created = False
        try:
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, FILE_MODE, dir_fd=dirfd)
            except OSError as exc:
                raise _os_error(exc) from None
            created = True
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                self._close(fd)
            try:
                os.link(tmp, parts[-1], src_dir_fd=dirfd, dst_dir_fd=dirfd, follow_symlinks=False)
            except FileExistsError:
                return False
            os.unlink(tmp, dir_fd=dirfd)
            created = False
            os.fsync(dirfd)
            return True
        except ShitError:
            raise
        except OSError:
            raise ShitError(INTERNAL) from None
        finally:
            if created:
                with contextlib.suppress(OSError):
                    os.unlink(tmp, dir_fd=dirfd)
            self._close(dirfd)

    def finish_publications(self, name):
        """Finish an exclusive publication interrupted between its link and its unlink.

        Only an exact pair is touched: a `.tmp-` name that is a regular file with two links whose other link is
        a sibling in the same directory. Anything else keeps the ordinary link rejection. Caller holds the lock.
        """
        parts = self._split(name)
        if len(parts) != 1:
            raise ShitError(CONFINEMENT)
        fd = self._walk(parts)
        try:
            names = os.listdir(fd)
            temps = [n for n in names if n.startswith(".tmp-")]
            if not temps:
                return
            seen = {}
            for entry in names:
                try:
                    st = os.stat(entry, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_nlink == 2:
                    seen.setdefault((st.st_dev, st.st_ino), []).append(entry)
            for tmp in temps:
                pair = [n for n in seen.get(self._ident(fd, tmp), []) if n != tmp]
                if len(seen.get(self._ident(fd, tmp), [])) == 2 and len(pair) == 1 and not pair[0].startswith(".tmp-"):
                    os.unlink(tmp, dir_fd=fd)
                    os.fsync(fd)
        except OSError:
            raise ShitError(INTERNAL) from None
        finally:
            self._close(fd)

    @staticmethod
    def _ident(fd, name):
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        return (st.st_dev, st.st_ino)

    def exists(self, rel):
        parts = self._split(rel)
        dirfd = self._walk(parts[:-1])
        try:
            return self._stat_leaf(dirfd, parts[-1]) is not None
        finally:
            self._close(dirfd)

    def remove(self, rel):
        parts = self._split(rel)
        dirfd = self._walk(parts[:-1])
        try:
            if self._stat_leaf(dirfd, parts[-1]) is None:
                return
            try:
                os.unlink(parts[-1], dir_fd=dirfd)
                os.fsync(dirfd)
            except OSError:
                raise ShitError(INTERNAL) from None
        finally:
            self._close(dirfd)

    # -- locking --------------------------------------------------------------

    @property
    def holds_lock(self):
        return self._lock_fd is not None

    @contextlib.contextmanager
    def locked(self, timeout=5.0):
        """Hold the store's exclusive advisory lock. Contention past `timeout` is exit code 1.

        The lock is taken on a fresh descriptor of the store directory itself: a descriptor, not a pathname, so
        unlinking or replacing the `lock` file cannot admit a second holder. The `lock` file is still opened and
        checked (a special file there is refused) but exclusion does not depend on it.
        """
        if self._lock_fd is not None:
            raise ShitError(INTERNAL)
        try:
            check = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | _NOFOLLOW | _CLOEXEC, FILE_MODE, dir_fd=self._fd)
        except OSError as exc:
            raise _os_error(exc) from None
        try:
            self._check_regular(os.fstat(check))
        finally:
            self._close(check)
        try:
            fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=self._fd)
        except OSError as exc:
            raise _os_error(exc) from None
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN) or time.monotonic() >= deadline:
                        raise ShitError(INTERNAL) from None
                    time.sleep(0.02)
        except BaseException:
            self._close(fd)
            raise
        self._lock_fd = fd
        try:
            yield
        finally:
            self._lock_fd = None
            self._close(fd)
