"""`shit audit export` and `shit audit verify`: the hash-chained audit export of the database store (docs/audit-export.md).

    audit export [--repo PATH] [--accept-database]
        Make `.shit/audit-export.txt` equal to the database. Refuses (exit code 8) a database that is older than,
        different from or has lost its export; `--accept-database` is the operator's decision that the database in
        front of them is the truth and the export is to be replaced.
    audit verify [--repo PATH] [--file PATH] [--database]
        Verify an export OFFLINE: the chains, every entry hash and the replay rules, from the export alone. With no
        `--file` it reads the project's export. `--database` also compares the export with the database (read-only).

Output is fixed text and counts (and the chain tips, which are hashes). It never prints a record, a path or an
exception text. Exit codes are those of docs/exit-codes.md: 0 ok, 2 usage, 5 unsafe file, 7 over a limit, 8 the export
or the database does not verify (a restore is among the causes), 9 missing export, store or repository.
"""

from . import _cli, audit_export, store
from .errors import FAILED_CLOSED, OK, PRECONDITION, ShitError
from .privacy import repo_root
from .project import Project

_STATES = {
    audit_export.CURRENT: "database matches the export",
    audit_export.BEHIND: "database is ahead of the export (a write whose export is not made yet)",
    audit_export.NONE: "no export and no entries",
    audit_export.MISSING: "database has entries but there is no export",
    audit_export.OLDER: "database is older than the export (restored or truncated)",
    audit_export.DIVERGED: "database differs from the export (restored or edited)",
}


def tips_lines(verified):
    return [f"tip events {verified.events.seq} {verified.events.hash}",
            f"tip queue {verified.queue.seq} {verified.queue.hash}"]


def export_command(repo, accept=False):
    project = Project.open(repo)
    try:
        conn = store.connect(project)
        try:
            wrote = audit_export.sync(project, conn, accept=accept)
        finally:
            conn.close()
        verified = audit_export.verify_bytes(audit_export.read_export(project) or b"")
    finally:
        project.close()
    head = "audit-export: written" if wrote else "audit-export: already current"
    return OK, [f"{head} (events {verified.events.seq}, queue {verified.queue.seq})", *tips_lines(verified)]


def verify_command(repo, file=None, database=False):
    project = None
    try:
        if file is None or database:
            project = Project.open(repo)
        if file is None:
            data = audit_export.read_export(project)
            if data is None:
                raise ShitError(PRECONDITION)
        else:
            data = audit_export.read_file(file)
        verified = audit_export.verify_bytes(data)
        lines = [f"audit-export: ok (events {verified.events.seq}, queue {verified.queue.seq})", *tips_lines(verified)]
        code = OK
        if database:
            conn = store.connect(project)
            try:
                with store.read_snapshot(conn):
                    found = audit_export.compare(
                        audit_export.Footer(verified.events, verified.queue, verified.body_sha256), conn)
            finally:
                conn.close()
            lines.append(f"database: {_STATES[found.state]}")
            if found.state in audit_export.REFUSED:
                code = FAILED_CLOSED
        return code, lines
    finally:
        if project is not None:
            project.close()


def _export(args):
    return export_command(repo_root(args.repo), args.accept_database)


def _verify(args):
    if args.file is not None and not args.database:
        return verify_command(None, args.file, False)  # offline: no repository needed
    return verify_command(repo_root(args.repo), args.file, args.database)


def register(subparsers):
    parser = subparsers.add_parser("audit", help="export and verify the hash-chained audit record of the store")
    sub = parser.add_subparsers(dest="action", required=True, parser_class=_cli.SafeParser)
    export = sub.add_parser("export", help="write the audit export from the database")
    export.add_argument("--repo", default=None, help="repository path (default: current directory)")
    export.add_argument("--accept-database", action="store_true",
                        help="replace an export that does not match this database (decided by the operator)")
    export.set_defaults(func=_export)
    verify = sub.add_parser("verify", help="verify an export offline (chains, hashes, replay rules)")
    verify.add_argument("--repo", default=None, help="repository path (default: current directory)")
    verify.add_argument("--file", default=None, help="an export file to verify (default: the project's export)")
    verify.add_argument("--database", action="store_true", help="also compare the export with the database, read-only")
    verify.set_defaults(func=_verify)
    return parser


def _build():
    parser = _cli.SafeParser(prog="shit")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_cli.SafeParser)
    register(sub)
    return parser


def main(argv=None):
    import sys

    return _cli.run(_build, ["audit", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())

