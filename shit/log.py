"""Append-only JSON Lines event log with a SHA-256 hash chain kept in a sidecar file.

The version 1 event-log record has no hash field and unknown fields are rejected, so the chain
lives in `events.chain`: line N is sha256(previous_hex + "\\n" + log_line_N). Metadata only; no
prompts or model replies are stored. The chain detects edits, deletions, reordering and truncation
by anyone who does not also rewrite the chain file; it is not a signature (see the threat model).
"""

import hashlib
from collections import namedtuple

from . import records
from .errors import INPUT_LIMIT, INTERNAL, PRECONDITION, REJECTED, ShitError

LOG_PATH = "events.jsonl"
CHAIN_PATH = "events.chain"
MAX_LOG_BYTES = 64 * 1024 * 1024
GENESIS = "0" * 64
CHAIN_LINE_BYTES = 65  # a hex digest and its newline

Prepared = namedtuple("Prepared", "index line chain_line")


def chain_hash(previous_hex, line):
    return hashlib.sha256(previous_hex.encode("ascii") + b"\n" + line).hexdigest()


def _check_lifecycle(events):
    """Reject a log whose timestamps go backwards or whose transitions do not continue each task's history.

    Every state_transition must start from the state the task is in (its previous to_state, or the
    initial state "open" for the first event of a task). A replayed old transition therefore fails the
    continuity check even when its id is new. Timestamps are fixed-width UTC strings, so they compare as text.
    """
    last_stamp, state = "", {}
    for event in events:
        stamp = event["timestamp"]
        if stamp < last_stamp:
            raise ShitError(REJECTED)
        last_stamp = stamp
        task_id = event["task_id"]
        if event["event"] == "task_created":
            if task_id in state:
                raise ShitError(REJECTED)
            state[task_id] = "open"
        elif event["event"] == "state_transition":
            if event["from_state"] != state.get(task_id, "open"):
                raise ShitError(REJECTED)
            state[task_id] = event["to_state"]


class EventLog:
    def __init__(self, project):
        self.project = project

    def _split(self, rel):
        """Return (complete_lines, torn_tail). A torn tail is bytes after the last newline, or None."""
        try:
            data = self.project.read(rel, limit=MAX_LOG_BYTES)
        except ShitError as exc:
            if exc.code != PRECONDITION:
                raise
            return [], None
        cut = data.rfind(b"\n") + 1
        torn = data[cut:] or None
        return (data[:cut - 1].split(b"\n") if cut else []), torn

    def _lines(self, rel):
        lines, torn = self._split(rel)
        if torn is not None:
            raise ShitError(REJECTED)
        return lines

    @staticmethod
    def healed(lines, torn, index, expected):
        """The complete lines left after dropping a torn tail, or a rejection. Pure: nothing is written.

        The torn bytes must be a proper prefix of the full append (`expected`, newline included) and the
        file must hold exactly `index` complete lines."""
        if torn is None:
            return lines
        if len(lines) != index or not 0 < len(torn) < len(expected) or not expected.startswith(torn):
            raise ShitError(REJECTED)
        return lines

    def heal_torn_tail(self, rel, lines, torn, index, expected):
        """Drop a torn final line left by an interrupted append of the pending transaction at `index`.

        The file is rewritten atomically with only the complete lines, so the next append re-writes the whole
        entry. Anything else is refused (see `healed`)."""
        if torn is None:
            return lines
        self.healed(lines, torn, index, expected)
        self.project.write_atomic(rel, b"".join(line + b"\n" for line in lines), limit=MAX_LOG_BYTES)
        return lines

    def check_room(self, line_bytes, chain_bytes, *, data=LOG_PATH, chain=CHAIN_PATH):
        """Refuse (exit code 7) an append that would take the data or the chain file past the size cap."""
        if (self.project.size(data) + line_bytes > MAX_LOG_BYTES
                or self.project.size(chain) + chain_bytes > MAX_LOG_BYTES):
            raise ShitError(INPUT_LIMIT)

    def _raw(self):
        """Return (log_lines, chain_lines) with only line-shape checks."""
        log_lines = self._lines(LOG_PATH)
        for line in log_lines:
            if len(line) > records.MAX_RECORD_BYTES:
                raise ShitError(INPUT_LIMIT)
            if line == b"":
                raise ShitError(REJECTED)
        return log_lines, self._lines(CHAIN_PATH)

    def verify(self):
        """Validate every event and the whole chain; return the parsed events in order."""
        log_lines, chain_lines = self._raw()
        return self.verify_lines(log_lines, chain_lines)

    @staticmethod
    def verify_lines(log_lines, chain_lines):
        """`verify` over lines already read (for example the lines left after a torn tail is dropped). Pure."""
        if len(chain_lines) != len(log_lines):
            raise ShitError(REJECTED)
        previous, events, seen = GENESIS, [], set()
        for line, recorded in zip(log_lines, chain_lines):
            event = records.load_event(line)
            if event["id"] in seen:
                raise ShitError(REJECTED)
            seen.add(event["id"])
            previous = chain_hash(previous, line)
            if recorded.decode("ascii", "replace") != previous:
                raise ShitError(REJECTED)
            events.append(event)
        _check_lifecycle(events)
        return events

    def prepare(self, event):
        """Validate `event` against the verified log and compute its line and chain entry."""
        events = self.verify()
        line = records.dump_event(event)
        if any(e["id"] == event["id"] for e in events):
            raise ShitError(REJECTED)
        _check_lifecycle([*events, event])  # the complete sequence including the candidate: time never goes backwards
        self.check_room(len(line) + 1, CHAIN_LINE_BYTES)
        previous = GENESIS if not events else self._last_chain()
        return Prepared(len(events), line, chain_hash(previous, line))

    def _last_chain(self):
        return self._lines(CHAIN_PATH)[-1].decode("ascii")

    def apply(self, prepared, hook=None):
        """Write a prepared event and chain entry. Safe to repeat after a crash."""
        if not self.project.holds_lock:
            raise ShitError(INTERNAL)
        log_lines, chain_lines = self._raw()
        self._apply_one(LOG_PATH, log_lines, prepared.index, prepared.line)
        if hook:
            hook("event-appended")
        self._apply_one(CHAIN_PATH, chain_lines, prepared.index, prepared.chain_line.encode("ascii"))
        if hook:
            hook("chain-appended")

    def _apply_one(self, rel, lines, index, line):
        if len(lines) == index:
            self.project.append(rel, line + b"\n", limit=MAX_LOG_BYTES)
        elif len(lines) != index + 1 or lines[index] != line:
            raise ShitError(REJECTED)

    def append(self, event):
        if not self.project.holds_lock:
            raise ShitError(INTERNAL)
        self.apply(self.prepare(event))
