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

Prepared = namedtuple("Prepared", "index line chain_line")


def chain_hash(previous_hex, line):
    return hashlib.sha256(previous_hex.encode("ascii") + b"\n" + line).hexdigest()


class EventLog:
    def __init__(self, project):
        self.project = project

    def _lines(self, rel):
        try:
            data = self.project.read(rel, limit=MAX_LOG_BYTES)
        except ShitError as exc:
            if exc.code != PRECONDITION:
                raise
            return []
        if data == b"":
            return []
        if not data.endswith(b"\n"):
            raise ShitError(REJECTED)
        return data[:-1].split(b"\n")

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
        return events

    def prepare(self, event):
        """Validate `event` against the verified log and compute its line and chain entry."""
        events = self.verify()
        line = records.dump_event(event)
        if any(e["id"] == event["id"] for e in events):
            raise ShitError(REJECTED)
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
            self.project.append(rel, line + b"\n")
        elif len(lines) != index + 1 or lines[index] != line:
            raise ShitError(REJECTED)

    def append(self, event):
        if not self.project.holds_lock:
            raise ShitError(INTERNAL)
        self.apply(self.prepare(event))
