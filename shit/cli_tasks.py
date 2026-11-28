"""Command line for the task commands: `python3 -m shit.cli_tasks <command> ...`.

Every command works by hand with no AI tool and no network. Success prints the record as JSON
(ASCII-escaped, so no terminal control sequence can reach the screen). Failure prints one fixed line
to stderr and exits with the stable code from docs/exit-codes.md; it never echoes input, paths or
operating-system text. A shared entry point can call `register(subparsers)` to add these commands.

    init
    task create ID --title T --summary S --owner ACTOR --actor ACTOR
    task show ID
    task move ID STATE --actor ACTOR --reason R [--blocked-reason R --blocking-item I]
    evidence add TASK ID --path REL --description D --actor ACTOR
    report add TASK ID --outcome O --summary S [--evidence ID ...] --actor ACTOR
    review add TASK ID --report ID --verdict V --summary S --actor ACTOR
    decision add TASK ID --status S --question Q --choice C --rationale R --actor ACTOR [--supersedes ID]
    <evidence|report|review|decision> show ID
    close TASK --actor ACTOR --reason R
"""

import argparse
import json
import os
import sys
from pathlib import Path

from . import (
    artifacts,
    audit,
    backend,
    backend_name,
    check,
    commands,
    file_backend_state,
    store,
)
from . import project as project_mod
from .errors import INTERNAL, MESSAGES, OK, PRECONDITION, USAGE, ShitError
from .lifecycle import ACTORS, STATES

FILE_BACKEND_ARTIFACTS = file_backend_state.FILE_BACKEND_ARTIFACTS


class _Parser(argparse.ArgumentParser):
    """An argument parser that fails with the fixed usage error instead of echoing the input."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise ShitError(USAGE)


def _add_sub(parent, name, **kw):
    return parent.add_parser(name, **kw)


def _actor(p):
    p.add_argument("--actor", required=True, choices=ACTORS)


def register(subparsers):
    """Add the task commands to an argparse subparsers object; each sets `func`."""
    p = _add_sub(subparsers, "init", help="create the .shit store")
    p.add_argument("--backend", default=None)
    p.set_defaults(func=_init)

    p = _add_sub(subparsers, "task", help="create, show or move a task")
    sub = p.add_subparsers(dest="action", required=True, parser_class=_Parser)
    c = _add_sub(sub, "create")
    c.add_argument("id")
    c.add_argument("--title", required=True)
    c.add_argument("--summary", required=True)
    c.add_argument("--owner", required=True, choices=ACTORS)
    _actor(c)
    c.set_defaults(func=_task_create)
    c = _add_sub(sub, "show")
    c.add_argument("id")
    c.set_defaults(func=_task_show)
    c = _add_sub(sub, "move")
    c.add_argument("id")
    c.add_argument("state", choices=STATES)
    c.add_argument("--reason", required=True)
    c.add_argument("--blocked-reason")
    c.add_argument("--blocking-item")
    _actor(c)
    c.set_defaults(func=_task_move)

    for kind in artifacts.KINDS:
        p = _add_sub(subparsers, kind, help=f"add or show a {kind} record")
        sub = p.add_subparsers(dest="action", required=True, parser_class=_Parser)
        a = _add_sub(sub, "add")
        a.add_argument("task")
        a.add_argument("id")
        _actor(a)
        _ADD_ARGS[kind](a)
        a.set_defaults(func=_ADD_RUN[kind])
        s = _add_sub(sub, "show")
        s.add_argument("id")
        s.set_defaults(func=_show, kind=kind)

    p = _add_sub(subparsers, "close", help="close an approved task")
    p.add_argument("task")
    p.add_argument("--reason", required=True)
    _actor(p)
    p.set_defaults(func=_close)

    check.register(subparsers)
    audit.register(subparsers)


def _evidence_args(a):
    a.add_argument("--path", required=True)
    a.add_argument("--description", required=True)


def _report_args(a):
    a.add_argument("--outcome", required=True, choices=artifacts.OUTCOMES)
    a.add_argument("--summary", required=True)
    a.add_argument("--evidence", action="append", default=[])
    a.add_argument("--log-reference")


def _review_args(a):
    a.add_argument("--report", required=True)
    a.add_argument("--verdict", required=True, choices=artifacts.VERDICTS)
    a.add_argument("--summary", required=True)


def _decision_args(a):
    a.add_argument("--status", required=True, choices=artifacts.STATUSES)
    a.add_argument("--question", required=True)
    a.add_argument("--choice", required=True)
    a.add_argument("--rationale", required=True)
    a.add_argument("--supersedes")


_ADD_ARGS = {
    "evidence": _evidence_args,
    "report": _report_args,
    "review": _review_args,
    "decision": _decision_args,
}


def _root(ns):
    return project_mod.discover_root(ns.cwd)


def _open(ns):
    return project_mod.Project.open(_root(ns))


def _init(ns):
    if ns.backend == "files":
        error = ShitError(USAGE)
        error.message = "file protocol was removed; start a new store"
        raise error
    if ns.backend not in (None, backend_name.SQLITE):
        raise ShitError(USAGE)
    proj = project_mod.Project.init(_root(ns))
    try:
        configured = backend.resolve(proj.repo)
        if ns.backend is not None and ns.backend != configured.name:
            raise ShitError(USAGE)
        found = file_backend_state.present(proj)
        if found:
            error = ShitError(PRECONDITION)
            error.message = (
                f"found file-protocol state ({', '.join(found)}); "
                "file protocol was removed; start a new store"
            )
            raise error
        store.init_store(proj, configured.busy_timeout_ms)
    finally:
        proj.close()
    return {"initialized": True}


def _with_project(ns, fn):
    proj = _open(ns)
    try:
        return fn(proj)
    finally:
        proj.close()


def _backend(ns):
    return backend.resolve(Path(_root(ns)))


def _task_create(ns):
    selected = _backend(ns)
    return _with_project(
        ns,
        lambda p: selected.records.new_task(
            p,
            task_id=ns.id,
            title=ns.title,
            summary=ns.summary,
            owner=ns.owner,
            actor=ns.actor,
            **_timeout(selected),
        ),
    )


def _task_show(ns):
    selected = _backend(ns)
    return _with_project(
        ns, lambda p: selected.tasks.get_task(p, ns.id, **_timeout(selected))
    )


def _task_move(ns):
    selected = _backend(ns)
    return _with_project(
        ns,
        lambda p: selected.tasks.transition(
            p,
            ns.id,
            ns.state,
            ns.actor,
            commands._text(ns.reason),
            blocked_reason=None
            if ns.blocked_reason is None
            else commands._text(ns.blocked_reason),
            blocking_item=None
            if ns.blocking_item is None
            else commands._text(ns.blocking_item),
            **_timeout(selected),
        ),
    )


def _timeout(selected):
    return (
        {}
        if selected.busy_timeout_ms is None
        else {"busy_timeout_ms": selected.busy_timeout_ms}
    )


def _evidence_run(ns):
    selected = _backend(ns)
    return _with_project(
        ns,
        lambda p: selected.records.add_evidence(
            p,
            task_id=ns.task,
            evidence_id=ns.id,
            path=ns.path,
            description=ns.description,
            actor=ns.actor,
            **_timeout(selected),
        ),
    )


def _report_run(ns):
    selected = _backend(ns)
    return _with_project(
        ns,
        lambda p: selected.records.add_report(
            p,
            task_id=ns.task,
            report_id=ns.id,
            outcome=ns.outcome,
            summary=ns.summary,
            evidence_ids=ns.evidence,
            actor=ns.actor,
            log_reference=ns.log_reference,
            **_timeout(selected),
        ),
    )


def _review_run(ns):
    selected = _backend(ns)
    return _with_project(
        ns,
        lambda p: selected.records.add_review(
            p,
            task_id=ns.task,
            review_id=ns.id,
            report_id=ns.report,
            verdict=ns.verdict,
            summary=ns.summary,
            actor=ns.actor,
            **_timeout(selected),
        ),
    )


def _decision_run(ns):
    selected = _backend(ns)
    return _with_project(
        ns,
        lambda p: selected.records.add_decision(
            p,
            task_id=ns.task,
            decision_id=ns.id,
            status=ns.status,
            question=ns.question,
            choice=ns.choice,
            rationale=ns.rationale,
            actor=ns.actor,
            supersedes_id=ns.supersedes,
            **_timeout(selected),
        ),
    )


_ADD_RUN = {
    "evidence": _evidence_run,
    "report": _report_run,
    "review": _review_run,
    "decision": _decision_run,
}


def _show(ns):
    selected = _backend(ns)
    return _with_project(
        ns, lambda p: selected.records.show(p, ns.kind, ns.id, **_timeout(selected))
    )


def _close(ns):
    selected = _backend(ns)
    return _with_project(
        ns,
        lambda p: selected.records.close_task(
            p, ns.task, ns.actor, commands._text(ns.reason), **_timeout(selected)
        ),
    )


def build_parser():
    parser = _Parser(prog="shit-tasks")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    register(sub)
    return parser


def main(argv=None, *, cwd=None, stdout=None, stderr=None):
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    try:
        ns = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
        ns.cwd = cwd or os.getcwd()
        if hasattr(ns, "repo") and ns.repo is None:
            ns.repo = ns.cwd
        result = ns.func(ns)
        if isinstance(result, tuple) and len(result) == 2:
            code, lines = result
            for line in lines:
                out.write(line + "\n")
            if code:
                err.write(f"shit: {MESSAGES[code]}\n")
            return code
        out.write(
            json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
        )
        return OK
    except SystemExit as exc:  # argparse --help
        return exc.code if isinstance(exc.code, int) else USAGE
    except ShitError as exc:
        err.write(f"shit: {exc.message}\n")
        return exc.code
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
        RecursionError,
        MemoryError,
    ):
        # Fixed output only: never an exception text, path or value.
        err.write(f"shit: {MESSAGES[INTERNAL]}\n")
        return INTERNAL


if __name__ == "__main__":
    sys.exit(main())
