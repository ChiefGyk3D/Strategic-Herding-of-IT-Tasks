"""Run a review-thread request plan with the GitHub CLI. This is the user's own step.

    python3 scripts/threads_run.py [--config PATH] [--gh PATH] [--allow-env NAME ...] PLAN.json

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
write. That is the whole check; the launcher cannot tell a genuine `gh` from a replaced one.

Host and credential: the host comes only from an explicit `--config PATH` (`[tracker] host`), else github.com; no
file is read by default and the plan never chooses it. A plan whose host is not null and differs is refused before any
call (exit 3). Every call carries `--hostname HOST`. The launcher reads the token variables from your environment and
passes the pair that applies to that host to `gh` (GH_TOKEN and GITHUB_TOKEN for github.com, GH_ENTERPRISE_TOKEN and
GITHUB_ENTERPRISE_TOKEN for any other host), and never the other pair. It never prints or logs their values.

Environment: `gh` gets only HOME, PATH, LANG, LC_ALL, TERM, TZ, USER, TMPDIR (HOME is how `gh` finds its own sign-in
and settings), the applicable token pair, GH_PROMPT_DISABLED=1 and NO_COLOR=1. Any other variable of yours is dropped,
GH_HOST included. A value with a control character is dropped.
Proxy and certificate variables (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, NO_PROXY, SSL_CERT_FILE, SSL_CERT_DIR,
REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE and similar) can send the token through a server of someone else's choosing, so the
launcher refuses to start (exit 3, naming the variable) when one is set. `--allow-env NAME` (repeatable) opts one such
name in and passes exactly that variable; it accepts only names on the fixed list in OPT_IN, never a token name.
The same list covers GH_CONFIG_DIR and the XDG_* directories, which are dropped unless opted in.

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
ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "USER", "TMPDIR")
ENV_FORCE = {"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}
# Set in your environment: the launcher refuses to start unless the name is opted in with --allow-env.
RISKY = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "GIT_SSL_CAPATH",
    "NODE_EXTRA_CA_CERTS",
)
# Dropped silently unless opted in (gh settings can name a socket or a host to talk to).
DROPPED_UNLESS_OPTED = ("GH_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")
OPT_IN = (*RISKY, *DROPPED_UNLESS_OPTED)
# The user's own credential, and only the pair that applies to the target host (never both pairs).
DEFAULT_HOST = "github.com"
TOKENS_DOTCOM = ("GH_TOKEN", "GITHUB_TOKEN")
TOKENS_OTHER = ("GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


def trusted_host(configured):
    """The host every call goes to: the configured one, else github.com. A plan never chooses it."""
    return (configured or DEFAULT_HOST).lower()


def clean_value(value):
    return isinstance(value, str) and not any(unicodedata.category(c) in ("Cc", "Cs") for c in value)


def child_env(environ, host=DEFAULT_HOST, allow=()):
    """The environment of `gh`: the fixed allowlist, the token pair of this host, and the names opted in."""
    env = {}
    tokens = TOKENS_DOTCOM if host.lower() == DEFAULT_HOST else TOKENS_OTHER
    for name in (*ENV_ALLOW, *tokens, *(n for n in allow if n in OPT_IN)):
        value = environ.get(name)
        if clean_value(value):
            env[name] = value
    env.update(ENV_FORCE)
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
    def __init__(self, path, popen, env):
        self.path, self.popen, self.env = path, popen, env

    def call(self, args):
        """Return (exit code, stdout text). Reads are bounded, the child is killed on overflow or after the deadline.

        A command that cannot start or times out is the tool-unavailable code; output past the limit is code 7.
        """
        limit = threads.ANSWER_LIMIT
        try:
            proc = self.popen([self.path, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=self.env, close_fds=True)
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


def check_gh_path(text):
    """An explicit `gh`: an absolute path to a regular file, not a symlink, that no group or other can write."""
    if not isinstance(text, str) or not os.path.isabs(text) or ".." in text.split(os.sep) or chr(0) in text:
        raise ShitError(USAGE)
    try:
        st = os.lstat(text)
    except FileNotFoundError:
        raise ShitError(PRECONDITION) from None
    except OSError:
        raise ShitError(CONFINEMENT) from None
    if not stat.S_ISREG(st.st_mode) or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or not os.access(text, os.X_OK):
        raise ShitError(CONFINEMENT)
    return text


def make_handler(popen, which, environ, cwd):
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
        path = check_gh_path(args.gh) if args.gh is not None else which("gh", path=environ.get("PATH"))
        if path is None:
            raise _cli.CodedError(_cli.TOOL_UNAVAILABLE)
        return execute(plan, Gh(path, popen, child_env(environ, host, allow)))

    return handler


def build(popen, which, environ, cwd):
    parser = _cli.SafeParser(prog="threads_run")
    parser.add_argument("--config", default=None, help="configuration file whose [tracker] host is trusted "
                        "(default: none, so github.com)")
    parser.add_argument("--gh", default=None, help="absolute path of the gh program (default: first on PATH)")
    parser.add_argument("--allow-env", action="append", type=allow_name, default=None, metavar="NAME",
                        help="pass one proxy, certificate or gh settings variable (repeatable; never a token)")
    parser.add_argument("plan", help="plan file from `shit threads`, below the working directory")
    parser.set_defaults(func=make_handler(popen, which, environ, cwd))
    return parser


def main(argv=None, popen=subprocess.Popen, which=shutil.which, environ=None, cwd=None):
    environ = os.environ if environ is None else environ
    args = sys.argv[1:] if argv is None else argv
    return _cli.run(lambda: build(popen, which, environ, cwd), args)


if __name__ == "__main__":
    raise SystemExit(main())
