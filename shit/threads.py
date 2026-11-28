"""Review threads of a pull request: validate a dispositions file and print the exact `gh api` request plan.

    shit threads [--config PATH] list OWNER/NAME PR
    shit threads [--config PATH] apply OWNER/NAME PR DISPOSITIONS.json

This module never starts a process, opens a socket or reads the environment (docs/hostile-fixtures.md, H-29d).
It checks its inputs and prints one JSON document, the request plan, as data. Running the plan is the user's own
step, done by `scripts/threads_run.py`, which sits outside this package for that reason (docs/threads.md).

`list` prints the plan for reading the unresolved threads. `apply` reads a strict JSON list of dispositions
`[{"comment_id": 1, "action": "reply|reply_resolve|skip", "body": "..."}]` and prints the plan to carry them out:
the thread read, then every reply (one REST POST each), then every resolve (one GraphQL `resolveReviewThread`
mutation each), in the order of the file. `skip` rows write nothing and are not in the plan. The plan holds no
token. The code host is the optional `[tracker] host` of the configuration named by `--config`; the repository
and the pull request are arguments.

The helpers below (answer parsing, plan validation, request arguments, list formatting) are what the launcher
uses, so the checks live in one tested place. Exit codes are the shared table in docs/exit-codes.md: 2 usage;
3 the dispositions file, the configuration or a plan is rejected; 5 a path violates confinement; 7 input over a
limit; 8 GitHub did not answer as required; 9 a file, the configuration or a named thread was not found;
11 the launcher could not start `gh`.
"""

import json
import re
import sys
import unicodedata
from pathlib import Path

from . import _cli, config, records
from ._text import _BAD_CATEGORIES, check_text, read_bounded
from .errors import PRECONDITION, REJECTED, USAGE, ShitError

PAGE = 50
MAX_PAGES = 20  # 1000 threads; more is refused, not silently cut
SHOW = 200
MAX_BODY = 2000
MAX_ITEMS = 200
FILE_LIMIT = 262_144
FILE_DEPTH = 4
PLAN_LIMIT = 1_048_576
PLAN_DEPTH = 6
ANSWER_LIMIT = 4_194_304
ANSWER_DEPTH = 12
ACTIONS = ("reply", "reply_resolve", "skip")
KINDS = ("list", "apply")

_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_THREAD_ID = re.compile(r"[A-Za-z0-9_=-]{1,200}")
_CURSOR = re.compile(r"[A-Za-z0-9_=+/-]{1,500}")

QUERY = (
    "query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name)"
    "{pullRequest(number:$number){reviewThreads(first:50,after:$cursor){pageInfo{hasNextPage endCursor}"
    "nodes{id isResolved isOutdated path line comments(first:1){nodes{databaseId author{login} body}}}}}}}"
)
MUTATION = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"


def closed():
    """GitHub did not answer as required: the fixed `failed closed` code."""
    return _cli.CodedError(_cli.HOOK_FAILED_CLOSED)


def _rejected():
    return ShitError(REJECTED)


def valid_repo(repo):
    return bool(_SLUG.fullmatch(repo)) and ".." not in repo and not repo.startswith(".")


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


# -- reading the thread list (data in, data out) --------------------------------------------------


def read_answer(text):
    """Strict JSON of one `gh api` answer. Anything unreadable is the failed-closed code, a limit is 7."""
    try:
        return records.parse_value(text.encode("utf-8", "surrogatepass"), ANSWER_LIMIT, ANSWER_DEPTH)
    except ShitError as exc:
        if exc.code == REJECTED:
            raise closed() from None
        raise


def parse_page(data):
    """One page of the thread query: (unresolved current threads, has_next_page, end_cursor)."""
    found = []
    try:
        if data.get("errors"):
            raise closed()
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
        raise closed() from None
    if more and not _CURSOR.fullmatch(cursor):
        raise closed()
    return found, more, cursor


def check_targets(threads, comment_ids):
    """Every comment id must be an unresolved, current thread of the pull request (exit 9 otherwise)."""
    known = {t["comment_id"] for t in threads}
    for cid in comment_ids:
        if cid not in known:
            raise ShitError(PRECONDITION)


def check_unique(found):
    """A read that names one comment id or one thread id twice is ambiguous: refused before any write (exit 8)."""
    comments = [t["comment_id"] for t in found]
    ids = [t["thread_id"] for t in found]
    if len(set(comments)) != len(comments) or len(set(ids)) != len(ids):
        raise closed()


def format_list(threads, repo, number):
    if not threads:
        return [f"no unresolved threads on {repo}#{number}"]
    by_file = {}
    for t in threads:
        by_file.setdefault(t["path"], []).append(t)
    lines = []
    for path in sorted(by_file):
        lines.append("file: " + flat(path, 300))  # a fixed prefix: a path can never start a `::` workflow command
        for t in by_file[path]:
            lines.append(f"  {t['comment_id']} {flat(t['path'], 100)}:{flat(t['line'], 10)} "
                         f"{flat(t['author'], 60)}: {flat(t['body'], SHOW)}")
    lines.append(f"{len(threads)} unresolved thread(s) on {repo}#{number}")
    return lines


# -- the dispositions file -------------------------------------------------------------------------


def _check_rows(rows):
    """Validate a list of dispositions (already parsed) and return [(comment_id, action, body)]."""
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


def parse_dispositions(path, base):
    """Read and validate the dispositions file. Any problem is a refusal; nothing is run by this module."""
    data = read_bounded(path, FILE_LIMIT, base)  # 5 outside the base or not a regular file, 9 if missing
    return _check_rows(records.parse_value(data, FILE_LIMIT, FILE_DEPTH))  # 7 over a limit, 3 hostile JSON


# -- the plan --------------------------------------------------------------------------------------


def _read_request(repo, number):
    owner, name = repo.split("/", 1)
    return {"op": "read_threads", "method": "POST", "endpoint": "graphql", "query": QUERY,
            "fields": {"owner": owner, "name": name, "number": number}}


def build_plan(kind, host, repo, number, items=()):
    """The request plan as plain data: the thread read, then every reply, then every resolve."""
    requests = [_read_request(repo, number)]
    if kind == "apply":
        for cid, action, body in items:
            if action != "skip":
                requests.append({"op": "reply", "method": "POST", "comment_id": cid, "fields": {"body": body},
                                 "endpoint": f"repos/{repo}/pulls/{number}/comments/{cid}/replies"})
        for cid, action, _body in items:
            if action == "reply_resolve":
                requests.append({"op": "resolve", "method": "POST", "endpoint": "graphql", "query": MUTATION,
                                 "comment_id": cid, "fields": {}})
    return {"schema": "threads-plan", "schema_version": 1, "kind": kind, "host": host, "repo": repo,
            "pr": number, "requests": requests}


def render_plan(plan):
    return json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_plan(data):
    """Validate a plan read from a file. It must equal, key for key, the plan this module would have built."""
    plan = records.parse_value(data, PLAN_LIMIT, PLAN_DEPTH)
    try:
        kind, host, repo, number = plan["kind"], plan["host"], plan["repo"], plan["pr"]
        if kind not in KINDS or not isinstance(repo, str) or not valid_repo(repo):
            raise _rejected()
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise _rejected()
        if host is not None:
            config.check_host(host)
        rows = []
        resolves = set()
        for request in plan["requests"][1:]:
            if request["op"] == "reply":
                rows.append({"comment_id": request["comment_id"], "action": "reply",
                             "body": request["fields"]["body"]})
            elif request["op"] == "resolve":
                resolves.add(request["comment_id"])
            else:
                raise _rejected()
        for row in rows:
            if row["comment_id"] in resolves:
                row["action"] = "reply_resolve"
        if resolves - {row["comment_id"] for row in rows}:
            raise _rejected()
        items = _check_rows(rows) if rows else []
        if kind == "list" and items:
            raise _rejected()
        rebuilt = build_plan(kind, host, repo, number, items)
    except (KeyError, TypeError, AttributeError, IndexError):
        raise _rejected() from None
    if rebuilt != plan:
        raise _rejected()
    return plan


def request_args(plan, request, thread_id=None, cursor=None):
    """The `gh` arguments (without the program name) for one request of a validated plan. Data only."""
    args = ["api"]
    if plan["host"]:
        args += ["--hostname", plan["host"]]
    if request["op"] == "read_threads":
        fields = request["fields"]
        args += ["graphql", "-f", f"query={QUERY}", "-f", f"owner={fields['owner']}",
                 "-f", f"name={fields['name']}", "-F", f"number={fields['number']}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
    elif request["op"] == "reply":
        args += ["-X", "POST", request["endpoint"], "-f", f"body={request['fields']['body']}"]
    else:
        if not isinstance(thread_id, str) or not _THREAD_ID.fullmatch(thread_id):
            raise _rejected()
        args += ["graphql", "-f", f"query={MUTATION}", "-f", f"id={thread_id}"]
    return args


def resolved_ok(answer):
    """A resolve counts only when the answer says isResolved is true and carries no errors."""
    try:
        thread = answer["data"]["resolveReviewThread"]["thread"]
        return thread["isResolved"] is True and not answer.get("errors")
    except (KeyError, TypeError, AttributeError):
        return False


# -- command ---------------------------------------------------------------------------------------


def make_handler(cwd):
    def handler(args):
        repo, number = args.repo, args.pr
        if not valid_repo(repo) or number < 1:
            raise ShitError(USAGE)
        base = Path(cwd) if cwd is not None else Path.cwd()
        host = None
        if args.config is not None:
            host = config.read(base / args.config, base).get("tracker", {}).get("host")
        items = parse_dispositions(base / args.file, base) if args.action == "apply" else ()
        return _cli.OK, [render_plan(build_plan(args.action, host, repo, number, items))]

    return handler


def register(subparsers, cwd=None):
    parser = subparsers.add_parser("threads", help="print the request plan for pull request review threads")
    parser.add_argument("--config", default=None, help="configuration file (for the optional tracker host)")
    sub = parser.add_subparsers(dest="action", required=True, parser_class=_cli.SafeParser)
    for name, text in (("list", "plan for reading unresolved review threads"),
                       ("apply", "plan for replying and resolving from a file")):
        p = sub.add_parser(name, help=text)
        p.add_argument("repo", help="OWNER/NAME")
        p.add_argument("pr", type=int, help="pull request number")
        if name == "apply":
            p.add_argument("file", help="JSON file of dispositions, below the working directory")
    parser.set_defaults(func=make_handler(cwd))
    return parser


def _build(cwd):
    parser = _cli.SafeParser(prog="shit")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_cli.SafeParser)
    register(sub, cwd)
    return parser


def main(argv=None, cwd=None):
    args = ["threads", *sys.argv[1:]] if argv is None else argv
    return _cli.run(lambda: _build(cwd), args)


if __name__ == "__main__":
    raise SystemExit(main())
