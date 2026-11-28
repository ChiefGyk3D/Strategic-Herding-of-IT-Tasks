"""Run a review-thread request plan with the GitHub CLI. This is the user's own step.

    python3 scripts/threads_run.py PLAN.json

The plan comes from `shit threads list ...` or `shit threads apply ...` (docs/threads.md), saved to a file below
the working directory. This launcher sits outside the `shit` package on purpose: no module of the package may
start a process (docs/hostile-fixtures.md, H-29d), so the one place that does is a script you read and run
yourself. It uses the standard library and the package's own checks, and nothing else.

What it does, in this order:
  1. Validates the plan (it must equal, key for key, what `shit threads` would print) before any call.
  2. Reads the unresolved threads (up to 20 pages of 50) with `gh api graphql`.
  3. `list` prints them. `apply` checks that every replied comment is an unresolved thread, then runs every
     reply, then every resolve, one call at a time.
Rules: no shell (an argument list); stdin closed; 60 second timeout per call; a reply is never retried (a POST
whose outcome is unknown could double-post); a resolve runs only after every reply was accepted, and counts only
when the answer says `isResolved` is true; the first failure stops the run and prints what was done; the output
of `gh` (stdout and stderr) is never shown, only checked.

Environment: `gh` gets only the variables in ENV_ALLOW, plus GH_PROMPT_DISABLED=1 and NO_COLOR=1. The token
variables on the list are passed through untouched so `gh` can sign in; this script never reads, prints or logs
them. Any other variable of yours is dropped. A value with a control character is dropped too.

Exit codes (docs/exit-codes.md): 0 done; 2 usage; 3 plan rejected; 5 plan path outside the working directory or
not a regular file; 7 limit; 8 GitHub did not answer as required or a write failed; 9 plan file or thread not
found; 11 `gh` is missing, cannot start or timed out.
"""

import os
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shit import _cli, threads
from shit._text import read_bounded

CALL_TIMEOUT = 60

# The variables `gh` may receive. Nothing else from the caller's environment is passed on.
ENV_ALLOW = (
    "PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "USER", "TMPDIR",
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",  # the user's own credential
    "GH_HOST", "GH_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
)
ENV_FORCE = {"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}


def child_env(environ):
    env = {}
    for name in ENV_ALLOW:
        value = environ.get(name)
        if isinstance(value, str) and not any(unicodedata.category(c) in ("Cc", "Cs") for c in value):
            env[name] = value
    env.update(ENV_FORCE)
    return env


class Gh:
    def __init__(self, path, runner, env):
        self.path, self.runner, self.env = path, runner, env

    def call(self, args):
        """Return (exit code, stdout). A command that cannot start or times out is the tool-unavailable code."""
        try:
            done = self.runner(
                [self.path, *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
                timeout=CALL_TIMEOUT, env=self.env,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):  # ValueError: NUL or lone surrogate in argv
            raise _cli.CodedError(_cli.TOOL_UNAVAILABLE) from None
        return int(done.returncode), str(done.stdout or "")

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
        if not ok:
            lines.append(f"{done} of {len(writes)} done before this failure")
            lines.append(f"{request['op']} {cid} failed or was not confirmed; not retried")
            return _cli.HOOK_FAILED_CLOSED, lines
        lines.append(f"{'replied' if request['op'] == 'reply' else 'resolved'} {cid}")
    return _cli.OK, lines


def make_handler(runner, which, environ, cwd):
    def handler(args):
        base = Path(cwd) if cwd is not None else Path.cwd()
        plan = threads.parse_plan(read_bounded(base / args.plan, threads.PLAN_LIMIT, base))
        path = which("gh", path=environ.get("PATH"))
        if path is None:
            raise _cli.CodedError(_cli.TOOL_UNAVAILABLE)
        return execute(plan, Gh(path, runner, child_env(environ)))

    return handler


def build(runner, which, environ, cwd):
    parser = _cli.SafeParser(prog="threads_run")
    parser.add_argument("plan", help="plan file from `shit threads`, below the working directory")
    parser.set_defaults(func=make_handler(runner, which, environ, cwd))
    return parser


def main(argv=None, runner=subprocess.run, which=shutil.which, environ=None, cwd=None):
    environ = os.environ if environ is None else environ
    args = sys.argv[1:] if argv is None else argv
    return _cli.run(lambda: build(runner, which, environ, cwd), args)


if __name__ == "__main__":
    raise SystemExit(main())
