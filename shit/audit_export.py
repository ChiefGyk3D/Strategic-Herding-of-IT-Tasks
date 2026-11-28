"""Hash-chained audit export of the database store (issue 63, epic 57; docs/audit-export.md).

The database is the authority. The export is a second copy of the two hash chains (the event log and the queue log),
written to its own file after each transaction commits, so that

- a whole-store restore is detected: a database older than its export, or one whose chain differs from the export's, is
  refused (exit code 8) before a write extends it;
- the chains can be verified offline, from the export alone, without the database (`verify_bytes`).

File format, ASCII, one record per line, LF:

    SHIT-AUDIT-EXPORT 1
    E <seq> <hash> <event line>        one per event, oldest first
    Q <seq> <hash> <queue entry line>  one per queue entry, oldest first
    TIP events <seq> <hash> queue <seq> <hash> sha256 <hex>

`<event line>` and `<queue entry line>` are the exact JSON lines each hash was computed over (`chain_hash(previous
hash, line)`, the first previous hash is 64 zeros). The last line carries the tip of each chain and the SHA-256 of every
byte before it. The file is rewritten whole with a temporary file, fsync and rename, mode 0600.

This module never opens the database itself: it reads the database only through `store.audit_*` and holds the write lock
through `store.write_transaction`.
"""

import collections
import hashlib
import os
import re
import stat

from . import queue_records, records, store
from .errors import (
    CONFINEMENT,
    FAILED_CLOSED,
    INPUT_LIMIT,
    PRECONDITION,
    USAGE,
    ContractError,
    ShitError,
)
from .log import GENESIS, EventLog, chain_hash

EXPORT_PATH = "audit-export.txt"
MAGIC = b"SHIT-AUDIT-EXPORT 1"
MAX_EXPORT_BYTES = 128 * 1024 * 1024
MAX_LINE_BYTES = records.MAX_RECORD_BYTES + 128
FOOTER_WINDOW = 1024
_HEX64 = re.compile(rb"[0-9a-f]{64}")
_NUMBER = re.compile(rb"(0|[1-9][0-9]{0,15})")
_FOOTER = re.compile(
    rb"TIP events (?P<eseq>0|[1-9][0-9]{0,15}) (?P<ehash>[0-9a-f]{64}) queue (?P<qseq>0|[1-9][0-9]{0,15}) "
    rb"(?P<qhash>[0-9a-f]{64}) sha256 (?P<body>[0-9a-f]{64})")
_PRIVATE = stat.S_IRUSR | stat.S_IWUSR
_READ_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

Tip = collections.namedtuple("Tip", "seq hash")
Footer = collections.namedtuple("Footer", "events queue body_sha256")
Verified = collections.namedtuple("Verified", "events queue body_sha256")
Status = collections.namedtuple("Status", "state events_behind queue_behind")

# What the comparison of an export with a database can say.
CURRENT = "current"  # the export is the database's tips
BEHIND = "behind"  # the export is a prefix of the database: a write whose export has not been made yet
NONE = "none"  # no export and an empty database: nothing has been written yet
MISSING = "missing"  # no export but the database has entries: the export was removed, or never written
OLDER = "database_older"  # the database has fewer entries than its export: restored or truncated
DIVERGED = "diverged"  # the database has a different entry where the export has one: restored or edited
REFUSED = (MISSING, OLDER, DIVERGED)


def _fault(stage):
    """Fault-injection seam: tests replace it to stop between the commit and the export (a crash)."""


def _fail():
    return ShitError(FAILED_CLOSED)


# -- the format ------------------------------------------------------------------------------------------------


def render(chains):
    """The export bytes for `{"events": [(seq, hash, line)], "queue": [...]}` as `store.audit_chains` returns them."""
    parts = [MAGIC + b"\n"]
    tips = {}
    for tag, name in ((b"E", "events"), (b"Q", "queue")):
        previous = GENESIS
        for seq, digest, line in chains[name]:
            if b"\n" in line or len(line) > MAX_LINE_BYTES:
                raise _fail()
            parts.append(tag + b" " + str(seq).encode("ascii") + b" " + digest.encode("ascii") + b" " + line + b"\n")
            previous = digest
        tips[name] = Tip(len(chains[name]), previous)
    body = b"".join(parts)
    footer = (b"TIP events %d %s queue %d %s sha256 %s\n" % (
        tips["events"].seq, tips["events"].hash.encode("ascii"), tips["queue"].seq, tips["queue"].hash.encode("ascii"),
        hashlib.sha256(body).hexdigest().encode("ascii")))
    if len(body) + len(footer) > MAX_EXPORT_BYTES:
        raise ShitError(INPUT_LIMIT)
    return body + footer


def _parse_footer(line):
    match = _FOOTER.fullmatch(line)
    if match is None:
        raise _fail()
    events = Tip(int(match["eseq"]), match["ehash"].decode("ascii"))
    queue = Tip(int(match["qseq"]), match["qhash"].decode("ascii"))
    for tip in (events, queue):
        if (tip.seq == 0) != (tip.hash == GENESIS):  # the empty chain's tip is the genesis hash, and only it
            raise _fail()
    return Footer(events, queue, match["body"].decode("ascii"))


def _record(line, tag, expected_seq, previous):
    """One `E` or `Q` line: its JSON text and hash after the checks of shape and chain. Returns (json, hash)."""
    parts = line.split(b" ", 3)
    if len(parts) != 4 or parts[0] != tag or not _NUMBER.fullmatch(parts[1]) or int(parts[1]) != expected_seq:
        raise _fail()
    if not _HEX64.fullmatch(parts[2]) or len(parts[3]) > records.MAX_RECORD_BYTES:
        raise _fail()
    if chain_hash(previous, parts[3]) != parts[2].decode("ascii"):
        raise _fail()
    return parts[3], parts[2].decode("ascii")


def _verify_lifecycle_effects(events, entries):
    """Require queue-implied lifecycle transitions and event transitions to agree in chain order."""
    first_queue = {}
    expected = []
    for entry in entries:
        if entry["kind"] == "graph_changed":
            continue
        task_id = entry["task_id"]
        first_queue.setdefault(task_id, entry["timestamp"])
        allowed = queue_records.EFFECTS.get(entry["kind"], {"life": ()})["life"]
        if entry["kind"] == "expired" and entry["state_after"] != "failed":
            allowed = ()
        if allowed:
            transitions = {
                (frm, to, entry["actor"] if actor == "entry" else actor)
                for frm, to, actor in allowed
            }
            expected.append((task_id, entry["timestamp"], transitions))

    found = 0
    for event in events:
        if event["event"] != "state_transition" or event["task_id"] not in first_queue:
            continue
        if event["timestamp"] < first_queue[event["task_id"]]:
            continue
        transition = (event["from_state"], event["to_state"], event["actor"])
        if transition == ("review", "approved", "human"):
            continue
        if found >= len(expected):
            raise _fail()
        task_id, timestamp, allowed = expected[found]
        if event["task_id"] != task_id or event["timestamp"] != timestamp or transition not in allowed:
            raise _fail()
        found += 1
    if found != len(expected):
        raise _fail()


def verify_bytes(data):
    """Verify an export from its bytes alone. Returns `Verified`; any failure is exit code 8 (7 when over the limit).

    Checks the magic line, the footer and the SHA-256 it records over the body, then each chain: sequence numbers,
    hashes recomputed from the lines, every record validated by the same code that wrote it, ids that follow the
    sequence, and the replay rules (`EventLog.verify_lines` for events, `queue_records.replay` for the queue)."""
    if not isinstance(data, bytes):
        raise ShitError(USAGE)
    if len(data) > MAX_EXPORT_BYTES:
        raise ShitError(INPUT_LIMIT)
    try:
        lines = data.split(b"\n")
        if lines.pop() != b"" or len(lines) < 2 or lines[0] != MAGIC or any(len(line) > MAX_LINE_BYTES for line in lines):
            raise _fail()
        data.decode("ascii")
        footer_line = lines.pop()
        footer = _parse_footer(footer_line)
        body = data[:len(data) - len(footer_line) - 1]
        if hashlib.sha256(body).hexdigest() != footer.body_sha256:
            raise _fail()
        event_lines, event_hashes, queue_lines, queue_hashes = [], [], [], []
        previous = {"E": GENESIS, "Q": GENESIS}
        for line in lines[1:]:
            tag = line[:1].decode("ascii")
            if tag not in previous or (tag == "E" and queue_lines):
                raise _fail()  # a record of another kind, or an event after the first queue entry
            target, hashes = (event_lines, event_hashes) if tag == "E" else (queue_lines, queue_hashes)
            text, digest = _record(line, tag.encode("ascii"), len(target) + 1, previous[tag])
            target.append(text)
            hashes.append(digest)
            previous[tag] = digest
        events = EventLog.verify_lines(event_lines, [h.encode("ascii") for h in event_hashes])
        for number, event in enumerate(events, start=1):
            if event["id"] != "event-" + format(number, "04d") or records.dump_event(event) != event_lines[number - 1]:
                raise _fail()
        entries = [queue_records.load_entry(text) for text in queue_lines]
        for text, entry in zip(queue_lines, entries, strict=True):
            if queue_records.dump_entry(entry) != text:
                raise _fail()
        queue_records.replay(entries)
        created = {
            event["task_id"]: event["timestamp"]
            for event in events
            if event["event"] == "task_created"
        }
        if any(
            entry.get("task_id") is not None
            and (entry["task_id"] not in created or created[entry["task_id"]] > entry["timestamp"])
            for entry in entries
        ):
            raise _fail()
        _verify_lifecycle_effects(events, entries)
        found = Footer(Tip(len(event_lines), previous["E"]), Tip(len(queue_lines), previous["Q"]), footer.body_sha256)
        if found != footer:
            raise _fail()
        return Verified(found.events, found.queue, found.body_sha256)
    except ShitError as exc:
        if exc.code == INPUT_LIMIT:
            raise
        raise _fail() from None
    except (ContractError, ValueError, TypeError, IndexError, RecursionError, UnicodeError):
        raise _fail() from None


# -- the file ---------------------------------------------------------------------------------------------------


def _open_private(project):
    """The export's descriptor and stat result below `.shit`, or None when it does not exist.

    No symlink is followed; the file must be a regular, single-link file of this user whose mode is within 0600."""
    try:
        fd = os.open(EXPORT_PATH, _READ_FLAGS, dir_fd=project._fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise ShitError(CONFINEMENT) from None
    st = os.fstat(fd)
    if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) & ~_PRIVATE):
        os.close(fd)
        raise ShitError(CONFINEMENT)
    if st.st_size > MAX_EXPORT_BYTES:
        os.close(fd)
        raise ShitError(INPUT_LIMIT)
    return fd, st


def read_footer(project):
    """The footer of the export file (a bounded read of its last bytes), or None when there is no file."""
    opened = _open_private(project)
    if opened is None:
        return None
    fd, st = opened
    try:
        with os.fdopen(fd, "rb") as handle:  # closes the descriptor
            handle.seek(max(0, st.st_size - FOOTER_WINDOW))
            tail = handle.read(FOOTER_WINDOW)
    except OSError:
        raise _fail() from None
    if not tail.endswith(b"\n"):
        raise _fail()
    return _parse_footer(tail[:-1].rsplit(b"\n", 1)[-1])


def read_export(project):
    """The whole export file's bytes (bounded by `MAX_EXPORT_BYTES`), or None when there is no file."""
    opened = _open_private(project)
    if opened is None:
        return None
    fd, st = opened
    try:
        chunks, total = [], 0
        while total <= st.st_size:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    except OSError:
        raise _fail() from None
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) != st.st_size:
        raise _fail()  # the file changed while it was read
    return data


def read_file(path):
    """Read an export copy from any path (offline use). A regular file, no symlink, at most `MAX_EXPORT_BYTES`."""
    if not isinstance(path, str) or "\x00" in path:
        raise ShitError(USAGE)
    try:
        fd = os.open(path, _READ_FLAGS)
    except FileNotFoundError:
        raise ShitError(PRECONDITION) from None
    except OSError:
        raise ShitError(CONFINEMENT) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ShitError(CONFINEMENT)
        if st.st_size > MAX_EXPORT_BYTES:
            raise ShitError(INPUT_LIMIT)
        data = b""
        while len(data) <= st.st_size:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            data += chunk
        if len(data) != st.st_size:
            raise _fail()
        return data
    except OSError:
        raise _fail() from None
    finally:
        os.close(fd)


# -- comparing with the database ----------------------------------------------------------------------------------


def compare(footer, conn):
    """How the export's tips relate to the database. Read-only; call it inside `store.read_snapshot` or a write
    transaction so the database tips are one commit. `footer` was read from the file BEFORE that snapshot began: an
    export is written after its commit, so a database read later always holds everything the export holds."""
    tips = store.audit_tips(conn)
    if footer is None:
        return Status(NONE if all(seq == 0 for seq, _ in tips.values()) else MISSING, 0, 0)
    behind, found = {}, set()
    for name, tip in (("events", footer.events), ("queue", footer.queue)):
        seq, _digest = tips[name]
        if tip.seq > seq:
            found.add(OLDER)
        elif tip.seq > 0 and store.audit_hash_at(conn, name, tip.seq) != tip.hash:
            found.add(DIVERGED)
        behind[name] = max(0, seq - tip.seq)
    if DIVERGED in found:
        state = DIVERGED
    elif OLDER in found:
        state = OLDER
    else:
        state = BEHIND if behind["events"] or behind["queue"] else CURRENT
    return Status(state, behind["events"], behind["queue"])


def status(project, conn):
    """The comparison, read-only (exit code 8 when the file cannot be read as an export, 5 for an unsafe file)."""
    footer = read_footer(project)
    with store.read_snapshot(conn):
        return compare(footer, conn)


def _verified_footer(project):
    """Read and verify the complete bounded export, returning its footer shape."""
    data = read_export(project)
    if data is None:
        return None
    verified = verify_bytes(data)
    return Footer(verified.events, verified.queue, verified.body_sha256)


def guard(project, conn):
    """For a connection that only reads: refuse (exit code 8) when the database is provably not the one the export
    was made from (older than it, or with a different entry). A missing export is not proof, so it is not refused
    here; a writer (`sync`) refuses it."""
    found = status(project, conn)
    if found.state in (OLDER, DIVERGED):
        raise _fail()
    return found


def sync(project, conn, *, accept=False):
    """Make the export equal to the database. Returns True when the file was written.

    Refuses (exit code 8) a database that is older than, different from or has lost its export, unless `accept` is
    true (the operator's explicit "this database is the truth now", `shit audit export --accept-database`). The
    write lock of the database is held while the file is written, so two writers cannot make the file go back."""
    try:
        footer = _verified_footer(project)
        with store.read_snapshot(conn):
            if compare(footer, conn).state == CURRENT:
                return False
    except ShitError as exc:
        if not (accept and exc.code == FAILED_CLOSED):  # an unreadable export may be replaced only on request
            raise
    with store.write_transaction(conn):
        try:
            found = compare(_verified_footer(project), conn)
        except ShitError as exc:
            if not (accept and exc.code == FAILED_CLOSED):
                raise
            found = Status(DIVERGED, 0, 0)
        if found.state == CURRENT:
            return False
        if found.state in REFUSED and not accept:
            raise _fail()
        data = render(store.audit_chains(conn))
        _fault("before-write")
        project.write_atomic(EXPORT_PATH, data, limit=MAX_EXPORT_BYTES)
        _fault("written")
    return True


def after_commit(project, conn):
    """Best effort after a write has committed: make the export current. A failure here does not undo or hide the
    write (the database is the authority); the next writer's `sync` finds the export behind and repairs it, and
    `shit check` reports it until then."""
    try:
        sync(project, conn)
    except ShitError:
        return False
    return True
