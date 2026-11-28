"""`shit check [--task T]`: verify that the records in `.shit/` are complete, valid and confined.

Read-only. Layout under `.shit/` (a missing file is exit 9, a present but invalid one is exit 3):

    tasks/<id>.json       task record (docs/schemas.md); every task is checked, or only --task
    briefs/<id>.md        work brief with its `issue:` line (shit/brief.py); needed once the task left `open`
    reports/<id>.md       report whose first block is the evidence header (shit/evidence.py);
                          needed from `review` on
    cleanup/<id>.md       cleanup report with non-empty `removed:` and `kept:` lines; needed from `review` on
    decisions/<id>.json   decision record by `human` with status accepted or rejected for this task;
                          needed from `approved` on (the human disposition)

The event log chain is verified too. Output is a fixed line per failed element, naming only the
validated task id. No record content, file name or exception text is ever printed (TM-08).
"""

import os
import re
import stat

from . import _cli, brief, config, evidence, queue, records
from ._text import check_text, decode
from .errors import (
    CONFINEMENT,
    FAILED_CLOSED,
    ILLEGAL_TRANSITION,
    INPUT_LIMIT,
    PRECONDITION,
    REJECTED,
    USAGE,
    ShitError,
)
from .log import EventLog
from .privacy import repo_root
from .project import Project, bounded_names

MAX_TEXT_BYTES = 16384
MAX_TASKS = 5000
NEEDS_BRIEF = {"running", "review", "approved", "closed", "blocked", "failed"}
NEEDS_REPORT = {"review", "approved", "closed"}
NEEDS_DISPOSITION = {"approved", "closed"}
PRIORITY = (CONFINEMENT, INPUT_LIMIT, REJECTED, FAILED_CLOSED, ILLEGAL_TRANSITION, PRECONDITION)
DISPOSITION_KEYS = {
    "schema", "schema_version", "id", "task_id", "created_at", "actor", "status", "question", "choice",
    "rationale", "supersedes_id",
}
DISPOSITION_REQUIRED = DISPOSITION_KEYS - {"supersedes_id"}
_FIELD = re.compile(r"(removed|kept): ?(\S.*)")
# Instruction files measured when `[limits] instruction_max_bytes` is set (docs/schemas.md). Fixed,
# vendor-neutral well-known names at the repository root; absent files are skipped.
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", ".github/copilot-instructions.md")
_ISSUE_LINE = re.compile(r"^issue:", re.MULTILINE)


class _Failures:
    def __init__(self):
        self.items = []  # (code, line)
        self.warnings = []

    def add(self, code, task_id, message):
        self.items.append((code, f"{task_id}: {message}"))

    def code(self):
        present = {code for code, _ in self.items}
        for code in PRIORITY:
            if code in present:
                return code
        return 1 if present else 0


_DIR = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
_FILE = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _same(a, b):
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _open_at(repo_fd, rel):
    """Open a regular, single-link file below the anchored repository descriptor; no symlink is followed."""
    dfd = os.dup(repo_fd)
    try:
        parts = rel.split("/")
        for part in parts[:-1]:
            nxt = os.open(part, _DIR | os.O_NOFOLLOW, dir_fd=dfd)
            os.close(dfd)
            dfd = nxt
        fd = os.open(parts[-1], _FILE, dir_fd=dfd)
    except FileNotFoundError:
        raise ShitError(PRECONDITION) from None
    except OSError:
        raise ShitError(CONFINEMENT) from None
    finally:
        os.close(dfd)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        os.close(fd)
        raise ShitError(CONFINEMENT)
    return fd


def _read_at(repo_fd, rel, limit):
    fd = _open_at(repo_fd, rel)
    with os.fdopen(fd, "rb") as fh:
        return fh.read(limit + 1)


def _root_unchanged(repo, repo_fd, project, failures):
    """The repository path, the anchored root and the opened store must still be one tree."""
    try:
        ok = (_same(os.stat(repo), os.fstat(repo_fd))
              and _same(os.stat(".shit", dir_fd=repo_fd, follow_symlinks=False), os.fstat(project._fd)))
    except OSError:
        ok = False
    if not ok:
        failures.add(CONFINEMENT, "repository", "root changed during check")
    return ok


def _check_limits(repo_fd, failures):
    """Instruction-file bytes and listed-role count against the optional `[limits]` of config.toml.

    No config.toml means no limits. Over a limit is a warning, or a failure when on_exceed is "fail".
    Only fixed file names and fixed words are printed, never file content. Every read goes through
    the repository descriptor opened once at the start of the check.
    """
    try:
        cfg = config.parse(decode(_read_at(repo_fd, "config.toml", config.MAX_BYTES)))
    except _cli.EXPECTED as exc:
        if _code(exc) != PRECONDITION:  # a missing file is simply "no limits"
            failures.add(_code(exc), "config", "rejected")
        return
    limits = cfg["limits"]
    strict = limits["on_exceed"] == "fail"

    def over(name, message):
        if strict:
            failures.add(INPUT_LIMIT, name, message)
        else:
            failures.warnings.append(f"warning: {name}: {message}")

    roles_max = limits.get("listed_roles_max")
    if roles_max is not None and len(cfg["roles"]["names"]) > roles_max:
        over("roles", "listed roles over limit")
    size_max = limits.get("instruction_max_bytes")
    if size_max is None:
        return
    for name in INSTRUCTION_FILES:
        try:
            fd = _open_at(repo_fd, name)
        except _cli.EXPECTED as exc:
            if _code(exc) != PRECONDITION:
                failures.add(_code(exc), name, "instruction file rejected")
            continue
        try:
            size = os.fstat(fd).st_size
        finally:
            os.close(fd)
        if size > size_max:
            over(name, "instruction file over size limit")


def _code(exc):
    return _cli.error_code(exc)


def _read(project, rel, limit):
    return project.read(rel, limit=limit)


def _validate_disposition(task_id, data):
    obj = records.parse(data)
    keys = set(obj)
    if not DISPOSITION_REQUIRED <= keys <= DISPOSITION_KEYS:
        raise ShitError(REJECTED)
    ok = (
        obj["schema"] == "decision" and type(obj["schema_version"]) is int and obj["schema_version"] == 1
        and obj["task_id"] == task_id and obj["actor"] == "human" and obj["status"] in ("accepted", "rejected")
        and isinstance(obj["id"], str) and records.SLUG.fullmatch(obj["id"])
        and isinstance(obj["created_at"], str) and records.TIMESTAMP.fullmatch(obj["created_at"])
        and all(isinstance(obj[k], str) and obj[k] for k in ("question", "choice", "rationale"))
        and "supersedes_id" not in obj
    )
    if not ok:
        raise ShitError(REJECTED)


def _validate_cleanup(data):
    if len(data) > MAX_TEXT_BYTES:
        raise ShitError(INPUT_LIMIT)
    text = decode(data)
    check_text(text)
    seen = {m.group(1) for m in map(_FIELD.fullmatch, text.split("\n")) if m}
    if seen != {"removed", "kept"}:
        raise ShitError(REJECTED)


def _check_element(failures, task_id, label, fn):
    try:
        fn()
    except _cli.EXPECTED as exc:  # a fixed line per element; the exception text is dropped
        code = _code(exc)
        failures.add(code, task_id, f"{label} {'missing' if code == PRECONDITION else 'rejected'}")
        return False
    return True


def _check_task(project, task_id, failures):
    try:
        task = records.load_task(project.read(f"tasks/{task_id}.json"))
    except _cli.EXPECTED as exc:
        failures.add(_code(exc), task_id, "task record " + ("missing" if _code(exc) == PRECONDITION else "rejected"))
        return
    if task["id"] != task_id:
        failures.add(REJECTED, task_id, "task record rejected")
        return
    state = task["state"]

    if state in NEEDS_BRIEF:
        def check_brief():
            data = project.read(f"briefs/{task_id}.md", limit=brief.MAX_BYTES)
            text = decode(data)
            try:
                brief.parse(text)
            except _cli.EXPECTED:
                if not _ISSUE_LINE.search(text):
                    failures.add(REJECTED, task_id, "brief is missing its issue")
                    return
                raise
        _check_element(failures, task_id, "brief", check_brief)

    if state in NEEDS_REPORT:
        _check_element(failures, task_id, "review header",
                       lambda: evidence.validate_header(decode(project.read(f"reports/{task_id}.md", limit=evidence.MAX_BYTES * 4))))
        _check_element(failures, task_id, "cleanup report",
                       lambda: _validate_cleanup(project.read(f"cleanup/{task_id}.md", limit=MAX_TEXT_BYTES + 1)))

    if state in NEEDS_DISPOSITION:
        _check_element(failures, task_id, "human disposition",
                       lambda: _validate_disposition(task_id, project.read(f"decisions/{task_id}.json")))


def _task_ids(project):
    """Task ids from tasks/*.json. A name that is not a valid id is reported without being printed."""
    fd = project._walk(("tasks",))
    try:
        names = sorted(bounded_names(fd))
    finally:
        os.close(fd)
    ids, bad = [], False
    for name in names:
        if name.startswith(".tmp-"):
            continue
        stem = name[:-5] if name.endswith(".json") else ""
        if stem and records.SLUG.fullmatch(stem):
            ids.append(stem)
        else:
            bad = True
    if len(ids) > MAX_TASKS:
        raise ShitError(INPUT_LIMIT)
    return ids, bad


def run_check(repo, task=None):
    """Return (exit code, lines). `task` is a task id or None for every task."""
    if task is not None and not records.SLUG.fullmatch(task):
        raise ShitError(USAGE)
    repo_fd = os.open(repo, _DIR)
    try:
        project = Project.open(repo)
    except BaseException:
        os.close(repo_fd)
        raise
    try:
        st = os.fstat(project._fd)
        if not stat.S_ISDIR(st.st_mode):
            raise ShitError(CONFINEMENT)
        failures = _Failures()
        queue_line = None
        ids, bad_names = _task_ids(project)
        if bad_names:
            failures.add(REJECTED, "tasks", "unexpected entry")
        if task is not None:
            if task not in ids:
                raise ShitError(PRECONDITION)
            ids = [task]
        for task_id in ids:
            _check_task(project, task_id, failures)
        events_ok = False
        try:
            EventLog(project).verify()
            events_ok = True
        except _cli.EXPECTED as exc:
            failures.add(_code(exc), "events", "log rejected")
        if _root_unchanged(repo, repo_fd, project, failures):
            _check_limits(repo_fd, failures)
            _root_unchanged(repo, repo_fd, project, failures)
        if project.exists("pending.json"):
            failures.add(REJECTED, "store", "unfinished commit; run any task command to recover")
        findings, queue_line = queue.diagnose(project, events_ok)  # read-only: the queue state and any queue pending record
        for code, text in findings:
            failures.add(code, "queue", text)
    finally:
        project.close()
        os.close(repo_fd)
    code = failures.code()
    lines = [line for _, line in failures.items] + failures.warnings
    if queue_line:
        lines.append(queue_line)
    lines.append(f"check: {'failed' if code else 'ok'} ({len(ids)} task{'s' if len(ids) != 1 else ''})")
    return code, lines


def handler(args):
    return run_check(repo_root(args.repo), args.task)


def register(subparsers):
    parser = subparsers.add_parser("check", help="verify the records in .shit/ are complete and valid")
    parser.add_argument("--repo", default=None, help="repository path (default: current directory)")
    parser.add_argument("--task", default=None, help="check one task id only")
    parser.set_defaults(func=handler)
    return parser


def _build():
    parser = _cli.SafeParser(prog="shit")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_cli.SafeParser)
    register(sub)
    return parser


def main(argv=None):
    import sys

    return _cli.run(_build, ["check", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
