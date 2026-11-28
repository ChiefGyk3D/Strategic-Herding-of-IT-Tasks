"""Run a review-thread request plan with the GitHub CLI. This is the user's own step.

    python3 scripts/threads_run.py [--config PATH] [--gh PATH] [--gh-config-dir PATH] [--allow-env NAME ...] PLAN.json

The plan comes from `shit threads list ...` or `shit threads apply ...` (docs/threads.md), saved to a file below
the working directory. This launcher sits outside the `shit` package on purpose: no module of the package may
start a process (docs/hostile-fixtures.md, H-29d), so the one place that does is a script you read and run
yourself. It uses the standard library and the package's own checks, and nothing else.

What it does, in this order:
  1. Refuses to run if a proxy or certificate variable is set in your environment (see below).
  2. Validates the plan (it must equal, key for key, what `shit threads` would print) before any call.
  3. Reads the unresolved threads (up to 20 pages of 50) with `gh api graphql`. A read that returns two threads with
     the same comment id or thread id is refused (exit 8) before any write.
  4. `list` prints them. `apply` checks that every replied comment is an unresolved thread, then runs every
     reply, then every resolve, one call at a time.
Rules: no shell (an argument list); stdin closed; 60 second deadline per call; output read in bounded pieces
(at most 4 MiB of standard output and of standard error; the child is killed past that, exit 7); a reply is never
retried (a POST whose outcome is unknown could double-post); a resolve runs only after every reply was accepted, and
counts only when the answer says `isResolved` is true; the first failure stops the run and prints what was done; the
output of `gh` (stdout and stderr) is never shown, only checked.

Which `gh`: the first one on PATH, unless `--gh PATH` names one. A `gh` that an attacker can put on your PATH
receives your token, so use `--gh` with an absolute path to a regular file (not a symlink) that no other user can
write, in a directory chain (every parent, after resolving links) that no other user can write unless it is a
root-owned sticky directory such as /tmp. The file is opened once, checked through that descriptor, and run through
/proc/self/fd/N where the platform has it, so replacing the path after the check does not change what runs; without
/proc the residual is a race on the path. The launcher cannot tell a genuine `gh` from a replaced one.

Host and credential: the host comes only from an explicit `--config PATH` (`[tracker] host`), else github.com; no
file is read by default and the plan never chooses it. A plan whose host is not null and differs is refused before any
call (exit 3). Every call carries `--hostname HOST`. The launcher reads the token variables from your environment and
passes the pair that applies to that host to `gh` (GH_TOKEN and GITHUB_TOKEN for github.com, GH_ENTERPRISE_TOKEN and
GITHUB_ENTERPRISE_TOKEN for any other host), and never the other pair. It never prints or logs their values.

Environment: `gh` gets only PATH, LANG, LC_ALL, TERM, TZ, USER, the applicable token pair, GH_PROMPT_DISABLED=1,
GH_NO_UPDATE_NOTIFIER=1 and NO_COLOR=1, and a fresh private HOME, GH_CONFIG_DIR, XDG_*_HOME and TMPDIR: a new
mode-0700 directory made for this run and removed afterwards (see below for where). Your own HOME is never passed, so no `gh` configuration
of yours (hosts, `api_host`, `http_unix_socket`, aliases, pager, extensions, proxy) applies. The token then comes
only from the token variable pair. Any other variable of yours is dropped, GH_HOST included.
If you rely on `gh auth login`, `--gh-config-dir PATH` opts in one configuration directory: absolute, an existing
directory you own, mode 0700, no symlink in the path, and no group- or other-writable directory above it unless it is
a root-owned sticky one (exit 5 otherwise, 2 if relative, 9 if missing). It is opened once and checked by descriptor, and
GH_CONFIG_DIR is /proc/self/fd/N for that descriptor (the path itself where there is no /proc, with a race on the path);
HOME stays private. Residual: a `config.yml` or `hosts.yml` in that directory can still redirect traffic or name a
socket, so the trust boundary covers whoever can write there or replace it through a parent you did not check.
Where: under your own /run/user/UID when it is yours and mode 0700, else under /tmp or /var/tmp when that is a root-owned
sticky directory, with every ancestor owned by root or you and not group or other writable; TMPDIR, TMP and TEMP are
never read. If none qualifies the launcher refuses (exit 9) before any call. The directory is created with mkdtemp,
checked by descriptor, and removed on every exit.
Proxy and certificate variables (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, NO_PROXY, SSL_CERT_FILE, SSL_CERT_DIR,
REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE and similar) can send the token through a server of someone else's choosing, so the
launcher refuses to start (exit 3, naming the variable) when one is set. `--allow-env NAME` (repeatable) opts one such
name in and passes exactly that variable; it accepts only names on the fixed list in OPT_IN, never a token name.

Exit codes (docs/exit-codes.md): 0 done; 2 usage; 3 plan rejected or a risky variable set; 5 plan or `gh` path
outside confinement or not a regular file; 7 limit; 8 GitHub did not answer as required or a write failed; 9 plan
file or thread not found; 11 `gh` is missing, cannot start or timed out.
"""

import os
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shit import _cli, config, threads
from shit._text import read_bounded
from shit.errors import (
    CONFINEMENT,
    INPUT_LIMIT,
    PRECONDITION,
    REJECTED,
    USAGE,
    ShitError,
)

CALL_TIMEOUT = 60
READ_CHUNK = 65536

# What `gh` receives from your environment. Nothing else is passed on.
ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "TERM", "TZ", "USER")
ENV_FORCE = {"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
# Set in your environment: the launcher refuses to start unless the name is opted in with --allow-env.
RISKY = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "GIT_SSL_CAPATH",
    "NODE_EXTRA_CA_CERTS",
)
OPT_IN = RISKY
# The user's own credential, and only the pair that applies to the target host (never both pairs).
DEFAULT_HOST = "github.com"
TOKENS_DOTCOM = ("GH_TOKEN", "GITHUB_TOKEN")
TOKENS_OTHER = ("GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


def trusted_host(configured):
    """The host every call goes to: the configured one, else github.com. A plan never chooses it."""
    return (configured or DEFAULT_HOST).lower()


def clean_value(value):
    return isinstance(value, str) and not any(unicodedata.category(c) in ("Cc", "Cs") for c in value)


def has_proc():
    return os.path.isdir("/proc/self/fd")


def private_env(root, config_dir=None, root_fd=None):
    """Make the private directories under `root` (mode 0700) and return the variables that point `gh` at them.

    With `root_fd` (and /proc) the directories are made relative to that descriptor and the variables name them as
    /proc/self/fd/N/<name>, so what `gh` uses is the directory that was checked, whatever the path names later.
    """
    names = {"HOME": "home", "XDG_CONFIG_HOME": "xdg-config", "XDG_CACHE_HOME": "xdg-cache",
             "XDG_DATA_HOME": "xdg-data", "XDG_STATE_HOME": "xdg-state", "TMPDIR": "tmp", "GH_CONFIG_DIR": "gh"}
    env = {}
    for name, sub in names.items():
        if root_fd is not None:
            os.mkdir(sub, 0o700, dir_fd=root_fd)
            env[name] = f"/proc/self/fd/{root_fd}/{sub}"
        else:
            path = os.path.join(root, sub)
            os.mkdir(path, 0o700)
            env[name] = path
    if config_dir is not None:
        env["GH_CONFIG_DIR"] = config_dir
    return env


def root_unchanged(root, fd):
    """True while the path still names the directory that was checked (device, inode, owner, mode)."""
    try:
        now, held = os.lstat(root), os.fstat(fd)
    except OSError:
        return False
    return ((now.st_dev, now.st_ino) == (held.st_dev, held.st_ino) and stat.S_ISDIR(now.st_mode)
            and now.st_uid == os.geteuid() and stat.S_IMODE(now.st_mode) == 0o700)


def remove_root(root, fd, proc):
    """Remove what this run made. Through the descriptor where there is /proc; the directory itself only if the path
    still names it (otherwise it is left in place and the caller has already reported the change)."""
    if proc:
        for name in os.listdir(fd):
            shutil.rmtree(f"/proc/self/fd/{fd}/{name}", ignore_errors=True)
    elif root_unchanged(root, fd):
        shutil.rmtree(root, ignore_errors=True)
        return
    if root_unchanged(root, fd):
        try:
            os.rmdir(root)
        except OSError:
            pass


def child_env(environ, host=DEFAULT_HOST, allow=(), private=None):
    """The environment of `gh`: the fixed allowlist, the token pair of this host, the names opted in, and the
    private directories. The caller's HOME and every XDG or config variable are never copied."""
    env = {}
    tokens = TOKENS_DOTCOM if host.lower() == DEFAULT_HOST else TOKENS_OTHER
    for name in (*ENV_ALLOW, *tokens, *(n for n in allow if n in OPT_IN)):
        value = environ.get(name)
        if clean_value(value):
            env[name] = value
    env.update(ENV_FORCE)
    env.update(private or {})
    return env


def risky_set(environ, allow=()):
    """Names of proxy and certificate variables that are set and not opted in, in a fixed order."""
    return [name for name in RISKY if name in environ and name not in allow]


def allow_name(text):
    """argparse type for --allow-env: a name from OPT_IN only. A token name or anything else is a usage error."""
    if text not in OPT_IN:
        raise ValueError("not an allowed name")
    return text


class Gh:
    def __init__(self, path, popen, env, fd=None, extra_fds=()):
        self.path, self.popen, self.env, self.fd, self.extra_fds = path, popen, env, fd, tuple(extra_fds)

    def call(self, args):
        """Return (exit code, stdout text). Reads are bounded, the child is killed on overflow or after the deadline.

        A command that cannot start or times out is the tool-unavailable code; output past the limit is code 7.
        """
        limit = threads.ANSWER_LIMIT
        try:
            extra = {}
            if self.fd is not None:  # run the very file that was checked, not whatever the path names by now
                extra["executable"] = f"/proc/self/fd/{self.fd}"
            keep = tuple(f for f in (self.fd, *self.extra_fds) if f is not None)
            if keep:
                extra["pass_fds"] = keep
            proc = self.popen([self.path, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=self.env, close_fds=True, **extra)
        except (OSError, ValueError):  # ValueError: NUL or lone surrogate in argv
            raise _cli.CodedError(_cli.TOOL_UNAVAILABLE) from None
        out = bytearray()
        failure = None
        sel = selectors.DefaultSelector()
        try:
            streams = {proc.stdout: "out", proc.stderr: "err"}
            for stream in streams:
                os.set_blocking(stream.fileno(), False)
                sel.register(stream, selectors.EVENT_READ)
            deadline = time.monotonic() + CALL_TIMEOUT
            seen = {"out": 0, "err": 0}
            while sel.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    failure = _cli.CodedError(_cli.TOOL_UNAVAILABLE)
                    break
                for key, _ in sel.select(min(left, 1.0)):
                    chunk = os.read(key.fileobj.fileno(), READ_CHUNK)
                    if not chunk:
                        sel.unregister(key.fileobj)
                        continue
                    which = streams[key.fileobj]
                    seen[which] += len(chunk)
                    if seen[which] > limit:
                        failure = ShitError(INPUT_LIMIT)
                        break
                    if which == "out":
                        out += chunk  # stderr is counted and dropped, never kept
                if failure is not None:
                    break
            if failure is None:  # both streams closed: the child is exiting, give it the rest of the deadline
                try:
                    proc.wait(timeout=max(deadline - time.monotonic(), 0.1))
                except subprocess.TimeoutExpired:
                    failure = _cli.CodedError(_cli.TOOL_UNAVAILABLE)
        finally:
            sel.close()
            if failure is not None:
                proc.kill()
            try:
                proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass
            for stream in (proc.stdout, proc.stderr):
                stream.close()
        if failure is not None:
            raise failure
        try:
            text = bytes(out).decode("utf-8")
        except UnicodeDecodeError:
            raise threads.closed() from None
        return int(proc.returncode), text

    def json(self, args):
        code, out = self.call(args)
        if code != 0:
            raise threads.closed()  # the stderr of `gh` can echo request text: it is never read
        return threads.read_answer(out)


def read_threads(gh, plan, read_request):
    found = []
    cursor = ""
    for _ in range(threads.MAX_PAGES):
        page, more, cursor = threads.parse_page(gh.json(threads.request_args(plan, read_request, cursor=cursor)))
        found += page
        if not more:
            threads.check_unique(found)  # two threads with one comment id are ambiguous: refused before any write
            return found
    raise threads.closed()  # more than PAGE * MAX_PAGES threads: refused, not silently cut


def execute(plan, gh):
    """Run a validated plan. Returns (exit code, lines)."""
    read_request, *writes = plan["requests"]
    found = read_threads(gh, plan, read_request)
    if plan["kind"] == "list":
        return _cli.OK, threads.format_list(found, plan["repo"], plan["pr"])
    # Precondition, before any write: every replied or resolved comment is an unresolved thread of this PR (exit 9).
    threads.check_targets(found, [w["comment_id"] for w in writes])
    thread_of = {t["comment_id"]: t["thread_id"] for t in found}
    lines = []
    for done, request in enumerate(writes):
        cid = request["comment_id"]
        try:
            if request["op"] == "reply":
                code, _out = gh.call(threads.request_args(plan, request))
                ok = code == 0
            else:
                thread_id = thread_of.get(cid)  # None only if the precondition above were bypassed: an error
                ok = thread_id is not None and threads.resolved_ok(
                    gh.json(threads.request_args(plan, request, thread_id))
                )
        except _cli.CodedError as exc:
            lines.append(f"{done} of {len(writes)} done before this failure")
            return exc.code, lines
        except ShitError as exc:  # an answer over the limit
            lines.append(f"{done} of {len(writes)} done before this failure")
            return exc.code, lines
        if not ok:
            lines.append(f"{done} of {len(writes)} done before this failure")
            lines.append(f"{request['op']} {cid} failed or was not confirmed; not retried")
            return _cli.HOOK_FAILED_CLOSED, lines
        lines.append(f"{'replied' if request['op'] == 'reply' else 'resolved'} {cid}")
    return _cli.OK, lines


def configured_host(config_arg, base):
    """`[tracker] host` of the explicit --config file, else None. No file is ever picked up by default."""
    if config_arg is None:
        return None
    return config.read(base / config_arg, base).get("tracker", {}).get("host")


def _safe_dir_chain(directory):
    """Every directory from / to `directory` (links resolved) is owned by root or you and is not writable by group or
    others, unless it is a root-owned sticky directory (such as /tmp). Raises the confinement error otherwise."""
    path = os.path.realpath(directory)
    parts = [p for p in path.split(os.sep) if p]
    current = os.sep
    for part in ["", *parts]:
        current = os.path.join(current, part) if part else os.sep
        st = os.lstat(current)
        if not stat.S_ISDIR(st.st_mode):
            raise ShitError(CONFINEMENT)
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not (st.st_uid == 0 and st.st_mode & stat.S_ISVTX):
            raise ShitError(CONFINEMENT)
        if st.st_uid not in (0, os.geteuid()):  # a directory another user owns, whatever its mode, is theirs to change
            raise ShitError(CONFINEMENT)
    return path


def open_gh(text):
    """An explicit `gh`: absolute path, regular file (no symlink), executable, not writable by group or others,
    in a directory chain that no other user can write. Returns (path, open file descriptor); the caller closes it."""
    if not isinstance(text, str) or not os.path.isabs(text) or ".." in text.split(os.sep) or chr(0) in text:
        raise ShitError(USAGE)
    try:
        before = os.lstat(text)
        _safe_dir_chain(os.path.dirname(text))
        fd = os.open(text, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise ShitError(PRECONDITION) from None
    except OSError:
        raise ShitError(CONFINEMENT) from None
    try:
        st = os.fstat(fd)
        same = (st.st_dev, st.st_ino) == (before.st_dev, before.st_ino)
        if (not same or not stat.S_ISREG(st.st_mode) or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or st.st_uid not in (0, os.geteuid()) or not os.access(text, os.X_OK)):
            raise ShitError(CONFINEMENT)
    except BaseException:
        os.close(fd)
        raise
    return text, fd


def open_config_dir(text):
    """The opt-in `gh` configuration directory: absolute, existing, owned by you, mode 0700, no symlink in the path,
    and every directory above it not writable by group or others (a root-owned sticky directory such as /tmp is
    fine). The directory is opened once and checked through that descriptor. Returns (path, descriptor)."""
    if not isinstance(text, str) or not os.path.isabs(text) or ".." in text.split(os.sep) or chr(0) in text:
        raise ShitError(USAGE)
    current = os.sep
    try:
        for part in [p for p in text.split(os.sep) if p]:
            current = os.path.join(current, part)
            if stat.S_ISLNK(os.lstat(current).st_mode):
                raise ShitError(CONFINEMENT)
        before = os.lstat(text)
        _safe_dir_chain(os.path.dirname(text))
        fd = os.open(text, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise ShitError(PRECONDITION) from None
    except OSError:
        raise ShitError(CONFINEMENT) from None
    try:
        st = os.fstat(fd)
        same = (st.st_dev, st.st_ino) == (before.st_dev, before.st_ino)
        if not same or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise ShitError(CONFINEMENT)
    except BaseException:
        os.close(fd)
        raise
    return text, fd


def default_bases():
    """The places the private directory may be made, best first. The ambient TMPDIR, TMP and TEMP are never read."""
    return [(f"/run/user/{os.geteuid()}", "runtime"), ("/tmp", "system"), ("/var/tmp", "system")]


def usable_base(path, kind):
    """True if `path` is a safe parent for the private directory.

    runtime: absolute, not a symlink, owned by you, mode exactly 0700, safe ancestors. system: absolute, not a
    symlink, owned by root, sticky, safe ancestors (so only the owner of an entry can rename or remove it).
    """
    if not isinstance(path, str) or not os.path.isabs(path) or ".." in path.split(os.sep) or chr(0) in path:
        return False
    try:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):  # lstat: a symlink is not a directory here
            return False
        if kind == "runtime":
            ok = st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) == 0o700
        else:
            ok = st.st_uid == 0 and bool(st.st_mode & stat.S_ISVTX)
        if ok:
            _safe_dir_chain(os.path.dirname(path))
        return ok
    except (OSError, ShitError):
        return False


def pick_base(bases):
    """The first usable base, or None."""
    for path, kind in bases:
        if usable_base(path, kind):
            return path
    return None


def make_private_root(base):
    """A new directory under `base`, mode 0700, checked after creation through a descriptor. Returns (path, fd)."""
    root = tempfile.mkdtemp(prefix="shit-threads-", dir=base)
    fd = None
    try:
        before = os.lstat(root)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        st = os.fstat(fd)
        if ((st.st_dev, st.st_ino) != (before.st_dev, before.st_ino) or not stat.S_ISDIR(st.st_mode)
                or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700):
            raise ShitError(CONFINEMENT)
    except BaseException:
        if fd is not None:
            os.close(fd)
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root, fd


def make_handler(popen, which, environ, cwd, bases):
    def handler(args):
        base = Path(cwd) if cwd is not None else Path.cwd()
        allow = tuple(args.allow_env or ())
        risky = risky_set(environ, allow)
        if risky:
            return REJECTED, [f"refused: {risky[0]} is set in the environment (opt in with --allow-env {risky[0]})"]
        host = trusted_host(configured_host(args.config, base))
        plan = threads.parse_plan(read_bounded(base / args.plan, threads.PLAN_LIMIT, base))
        # The host, and so where the token goes, comes only from this machine's configuration, never from the
        # plan file. A plan may leave it out (null) or name the same host; any other host is refused, no call made.
        if plan["host"] is not None and plan["host"].lower() != host:
            raise ShitError(REJECTED)
        plan = {**plan, "host": host}
        fds = []
        try:
            config_dir = None
            if args.gh_config_dir is not None:
                config_dir, cfd = open_config_dir(args.gh_config_dir)
                fds.append(cfd)
            fd = None
            if args.gh is not None:
                path, fd = open_gh(args.gh)
                fds.append(fd)
            else:
                path = which("gh", path=environ.get("PATH"))
            if path is None:
                raise _cli.CodedError(_cli.TOOL_UNAVAILABLE)
            # The descriptor paths keep the program and the directory that were checked, whatever the paths name
            # later. No /proc: the paths themselves are used and the race on them is a documented residual.
            proc = has_proc()
            if config_dir is not None and proc:
                config_dir = f"/proc/self/fd/{cfd}"
            base = pick_base(bases if bases is not None else default_bases())
            if base is None:
                return PRECONDITION, [("refused: no safe directory for the private environment (need your 0700 "
                                       "/run/user/UID, or a root-owned sticky /tmp or /var/tmp); TMPDIR is never used")]
            root, root_fd = make_private_root(base)
            fds.append(root_fd)
            try:
                if not root_unchanged(root, root_fd):  # re-verified immediately before launch
                    return CONFINEMENT, ["refused: the private directory changed before launch; nothing was run"]
                env = child_env(environ, host, allow, private_env(root, config_dir, root_fd if proc else None))
                extra = ([cfd] if config_dir and proc else []) + ([root_fd] if proc else [])
                code, lines = execute(plan, Gh(path, popen, env, fd if proc else None, extra))
                if not root_unchanged(root, root_fd):  # and again after the child exits
                    lines.append("warning: the private directory was replaced while gh ran; its contents are not "
                                 "trusted and it was left in place")
                    return CONFINEMENT, lines
                return code, lines
            finally:
                remove_root(root, root_fd, proc)
        finally:
            for open_fd in fds:
                os.close(open_fd)

    return handler


def build(popen, which, environ, cwd, bases=None):
    parser = _cli.SafeParser(prog="threads_run")
    parser.add_argument("--config", default=None, help="configuration file whose [tracker] host is trusted "
                        "(default: none, so github.com)")
    parser.add_argument("--gh", default=None, help="absolute path of the gh program (default: first on PATH)")
    parser.add_argument("--gh-config-dir", default=None, help="absolute path of a gh configuration directory you own "
                        "(mode 0700) to use instead of an empty private one")
    parser.add_argument("--allow-env", action="append", type=allow_name, default=None, metavar="NAME",
                        help="pass one proxy, certificate or gh settings variable (repeatable; never a token)")
    parser.add_argument("plan", help="plan file from `shit threads`, below the working directory")
    parser.set_defaults(func=make_handler(popen, which, environ, cwd, bases))
    return parser


def main(argv=None, popen=subprocess.Popen, which=shutil.which, environ=None, cwd=None, bases=None):
    environ = os.environ if environ is None else environ
    args = sys.argv[1:] if argv is None else argv
    return _cli.run(lambda: build(popen, which, environ, cwd, bases), args)


if __name__ == "__main__":
    raise SystemExit(main())
