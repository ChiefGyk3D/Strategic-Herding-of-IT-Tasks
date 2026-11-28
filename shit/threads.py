"""List the unresolved review threads of a pull request, then reply to and resolve them from a file.

    shit threads [--config PATH] list OWNER/NAME PR
    shit threads [--config PATH] apply OWNER/NAME PR DISPOSITIONS.json

`list` prints the unresolved, current (not outdated) review threads grouped by file, one line each: the first
comment's id, `file:line`, the author and the first 200 characters of its body. `apply` reads a strict JSON
list of dispositions `[{"comment_id": 1, "action": "reply|reply_resolve|skip", "body": "..."}]` and carries them
out: a reply is one REST POST, a resolve is one GraphQL `resolveReviewThread` mutation for that thread.

The repository and the pull request are arguments; nothing is hard-coded. The code host comes from the optional
`[tracker] host` of the configuration file named by `--config` (templates/config.toml); without one the GitHub
CLI uses its own default host. All GitHub traffic goes through the `gh` command, which holds the credential:
no token is read, passed or printed, and the output of `gh` is never shown.

Every disposition is checked against the file's own rules and the live thread list before the first write, so a
bad file writes nothing. After that the writes run one at a time and stop at the first failure. A reply is
never retried (a POST whose outcome is unknown could double-post), and a thread is resolved only after its
reply was accepted. The dispositions file must be a regular file below the working directory.

Exit codes are the shared table in docs/exit-codes.md: 0 done; 2 usage; 3 the dispositions file or the
configuration is refused; 5 a path violates confinement; 7 input over a limit; 8 GitHub did not answer as
required (a read failed before any write, or a write failed and the lines printed show what was done);
9 the file, the configuration or a named thread was not found; 11 `gh` is missing, cannot start or timed out.
"""

import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

from . import _cli, config, records
from ._text import _BAD_CATEGORIES, check_text, read_bounded
from .errors import PRECONDITION, REJECTED, USAGE, ShitError

CALL_TIMEOUT = 60
MAX_PAGES = 20  # 1000 threads; more is refused, not silently cut
PAGE = 50
SHOW = 200
MAX_BODY = 2000
MAX_ITEMS = 200
FILE_LIMIT = 262_144
FILE_DEPTH = 4
ANSWER_LIMIT = 4_194_304
ANSWER_DEPTH = 12
ACTIONS = ("reply", "reply_resolve", "skip")
AUTO = object()  # gh_path default: look for `gh` on PATH

_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_THREAD_ID = re.compile(r"[A-Za-z0-9_=-]{1,200}")
_CURSOR = re.compile(r"[A-Za-z0-9_=+/-]{1,500}")

QUERY = (
    "query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name)"
    "{pullRequest(number:$number){reviewThreads(first:50,after:$cursor){pageInfo{hasNextPage endCursor}"
    "nodes{id isResolved isOutdated path line comments(first:1){nodes{databaseId author{login} body}}}}}}}"
)
MUTATION = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"


def _closed():
    """GitHub did not answer as required: the fixed `failed closed` code."""
    return _cli.CodedError(_cli.HOOK_FAILED_CLOSED)


def flat(text, limit):
    """One line: every whitespace run is one space, cut to `limit`, hostile characters written as escapes."""
    spaced = " ".join("".join(" " if c.isspace() else c for c in text).split())[:limit]
    out = []
    for ch in spaced:
        if unicodedata.category(ch) in _BAD_CATEGORIES:
            out.append(f"\\u{ord(ch):04x}" if ord(ch) <= 0xFFFF else f"\\U{ord(ch):08x}")
        else:
            out.append(ch)
    return "".join(out)


class Gh:
    """Runs the `gh` command: no shell, no stdin, a timeout, and the host from the configuration."""

    def __init__(self, path, runner, host):
        self.path = path
        self.runner = runner
        self.host = host

    def call(self, *args):
        """Return (exit code, stdout). A command that cannot start or times out is the tool-unavailable code."""
        argv = [self.path, "api"]
        if self.host:
            argv += ["--hostname", self.host]
        argv += list(args)
        try:
            done = self.runner(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=CALL_TIMEOUT,
                env=dict(os.environ),
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):  # ValueError: NUL or lone surrogate in argv
            raise _cli.CodedError(_cli.TOOL_UNAVAILABLE) from None
        return int(done.returncode), str(done.stdout or "")

    def json(self, *args):
        """One `gh api` call whose answer must be strict JSON; its stderr is never read or shown."""
        code, out = self.call(*args)
        if code != 0:
            raise _closed()
        try:
            return records.parse_value(out.encode("utf-8", "surrogatepass"), ANSWER_LIMIT, ANSWER_DEPTH)
        except ShitError as exc:
            if exc.code == REJECTED:
                raise _closed() from None
            raise


def fetch_threads(gh, repo, number):
    """Unresolved, current threads of the pull request. The answer is checked field by field."""
    owner, name = repo.split("/", 1)
    found = []
    cursor = ""
    for _ in range(MAX_PAGES):
        args = ["graphql", "-f", f"query={QUERY}", "-f", f"owner={owner}", "-f", f"name={name}",
                "-F", f"number={number}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        data = gh.json(*args)
        try:
            if data.get("errors"):
                raise _closed()
            block = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            more = block["pageInfo"]["hasNextPage"]
            if not isinstance(more, bool):
                raise TypeError("hasNextPage")
            cursor = str(block["pageInfo"]["endCursor"] or "")
            for item in block["nodes"]:
                if item["isResolved"] or item["isOutdated"]:
                    continue
                first = item["comments"]["nodes"][0]
                comment_id, thread_id = first["databaseId"], item["id"]
                if isinstance(comment_id, bool) or not isinstance(comment_id, int) or comment_id < 1:
                    raise ValueError("comment id")
                if not isinstance(thread_id, str) or not _THREAD_ID.fullmatch(thread_id):
                    raise ValueError("thread id")
                found.append({
                    "thread_id": thread_id,
                    "comment_id": comment_id,
                    "path": str(item.get("path") or "?"),
                    "line": str(item.get("line") or "?"),
                    "author": str((first.get("author") or {}).get("login") or "?"),
                    "body": str(first.get("body") or ""),
                })
        except _cli.CodedError:
            raise
        except (KeyError, TypeError, IndexError, AttributeError, ValueError):
            raise _closed() from None
        if not more:
            return found
        if not _CURSOR.fullmatch(cursor):
            raise _closed()
    raise _closed()  # more than PAGE * MAX_PAGES threads: refused, not silently cut


def _rejected():
    return ShitError(REJECTED)


def parse_dispositions(path, base):
    """Read and validate the dispositions file. Any problem is a refusal before anything is written."""
    data = read_bounded(path, FILE_LIMIT, base)  # 5 outside the base or not a regular file, 9 if missing
    rows = records.parse_value(data, FILE_LIMIT, FILE_DEPTH)  # 7 over a limit, 3 for hostile JSON
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ITEMS:
        raise _rejected()
    items = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise _rejected()
        keys = set(row)
        if not {"comment_id", "action"} <= keys or not keys <= {"comment_id", "action", "body"}:
            raise _rejected()
        cid, action = row["comment_id"], row["action"]
        if isinstance(cid, bool) or not isinstance(cid, int) or cid < 1 or cid in seen:
            raise _rejected()
        if action not in ACTIONS:
            raise _rejected()
        body = row.get("body", "")
        if action == "skip":
            if "body" in row:
                raise _rejected()
        else:
            if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY:
                raise _rejected()
            try:
                check_text(body)  # NUL, other control, bidi and zero-width characters; \n is allowed
            except ValueError:
                raise _rejected() from None
        seen.add(cid)
        items.append((cid, action, body))
    return items


def list_threads(gh, repo, number):
    threads = fetch_threads(gh, repo, number)
    if not threads:
        return _cli.OK, [f"no unresolved threads on {repo}#{number}"]
    by_file = {}
    for t in threads:
        by_file.setdefault(t["path"], []).append(t)
    lines = []
    for path in sorted(by_file):
        lines.append(flat(path, 300))
        for t in by_file[path]:
            lines.append(f"  {t['comment_id']} {flat(t['path'], 100)}:{flat(t['line'], 10)} "
                         f"{flat(t['author'], 60)}: {flat(t['body'], SHOW)}")
    lines.append(f"{len(threads)} unresolved thread(s) on {repo}#{number}")
    return _cli.OK, lines


def apply_dispositions(gh, repo, number, items):
    known = {t["comment_id"]: t for t in fetch_threads(gh, repo, number)}
    for cid, _action, _body in items:
        if cid not in known:
            raise ShitError(PRECONDITION)  # not an unresolved, current thread of this pull request
    lines = []
    done = 0
    for cid, action, body in items:
        if action == "skip":
            lines.append(f"skip {cid}")
            continue
        try:
            code, _out = gh.call("-X", "POST", f"repos/{repo}/pulls/{number}/comments/{cid}/replies",
                                 "-f", f"body={body}")
            if code != 0:
                lines.append(f"{done} of {len(items)} done before this failure")
                lines.append(f"reply to {cid} failed; not retried")
                return _cli.HOOK_FAILED_CLOSED, lines
            lines.append(f"replied {cid}")
            if action == "reply_resolve":
                if not _resolve(gh, known[cid]["thread_id"]):
                    lines.append(f"reply to {cid} posted, but the thread was not resolved")
                    return _cli.HOOK_FAILED_CLOSED, lines
                lines.append(f"resolved {cid}")
        except _cli.CodedError as exc:
            lines.append(f"{done} of {len(items)} done before this failure")
            return exc.code, lines
        done += 1
    return _cli.OK, lines


def _resolve(gh, thread_id):
    try:
        answer = gh.json("graphql", "-f", f"query={MUTATION}", "-f", f"id={thread_id}")
        thread = answer["data"]["resolveReviewThread"]["thread"]
        return thread["isResolved"] is True and not answer.get("errors")
    except (ShitError, KeyError, TypeError, AttributeError):
        return False
    # a CodedError (gh missing, timed out, failed closed) is the caller's to report, with the done count


def _read_host(config_arg, base):
    """The optional `[tracker] host` of the configuration file, or None. A bad file is refused."""
    if config_arg is None:
        return None
    return config.read(base / config_arg, base).get("tracker", {}).get("host")


def make_handler(runner, gh_path, cwd):
    def handler(args):
        repo, number = args.repo, args.pr
        if not _SLUG.fullmatch(repo) or ".." in repo or repo.startswith(".") or number < 1:
            raise ShitError(USAGE)
        base = Path(cwd) if cwd is not None else Path.cwd()
        host = _read_host(args.config, base)
        found = shutil.which("gh") if gh_path is AUTO else gh_path
        if found is None:
            raise _cli.CodedError(_cli.TOOL_UNAVAILABLE)
        gh = Gh(found, runner, host)
        if args.action == "list":
            return list_threads(gh, repo, number)
        items = parse_dispositions(base / args.file, base)
        return apply_dispositions(gh, repo, number, items)

    return handler


def register(subparsers, runner=subprocess.run, gh_path=AUTO, cwd=None):
    parser = subparsers.add_parser("threads", help="list, reply to and resolve pull request review threads")
    parser.add_argument("--config", default=None, help="configuration file (for the optional tracker host)")
    sub = parser.add_subparsers(dest="action", required=True, parser_class=_cli.SafeParser)
    for name, text in (("list", "show unresolved review threads"), ("apply", "reply and resolve from a file")):
        p = sub.add_parser(name, help=text)
        p.add_argument("repo", help="OWNER/NAME")
        p.add_argument("pr", type=int, help="pull request number")
        if name == "apply":
            p.add_argument("file", help="JSON file of dispositions, below the working directory")
    parser.set_defaults(func=make_handler(runner, gh_path, cwd))
    return parser


def _build(runner, gh_path, cwd):
    parser = _cli.SafeParser(prog="shit")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_cli.SafeParser)
    register(sub, runner, gh_path, cwd)
    return parser


def main(argv=None, runner=subprocess.run, gh_path=AUTO, cwd=None):
    args = ["threads", *sys.argv[1:]] if argv is None else argv
    return _cli.run(lambda: _build(runner, gh_path, cwd), args)


if __name__ == "__main__":
    raise SystemExit(main())
