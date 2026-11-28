"""Stable exit codes from docs/exit-codes.md, as two error types.

ShitError (storage, log, lifecycle) carries a code and its fixed message. ContractError (contract
validation) carries a code and a fixed message plus a field name, never the rejected value.
"""

MESSAGES = {
    1: "internal error",
    2: "usage error",
    3: "record rejected",
    4: "illegal lifecycle transition",
    5: "confinement violation",
    7: "input exceeds limit",
    8: "hook verification failed",
    9: "precondition missing",
    10: "human approval required",
    12: "lease refused",
    13: "store busy",
}

OK = 0
INTERNAL = 1
USAGE = 2
REJECTED = 3
ILLEGAL_TRANSITION = 4
CONFINEMENT = 5
INPUT_LIMIT = 7
FAILED_CLOSED = 8
PRECONDITION = 9
HUMAN_APPROVAL = 10
# Approved by the owner on 2026-11-27 (docs/queue-spec.md, OQ-1).
LEASE_REFUSED = 12
# The SQLite store could not take its write lock within the configured wait (docs/store.md). Retryable.
BUSY = 13


# Every stable exit code of docs/exit-codes.md except 0 (ok). The event log, the store and the tests read this one set.
STABLE_EXIT_CODES = tuple(range(1, 14))


class ShitError(Exception):
    """An error that maps to one stable exit code. It never carries raw paths, content or OS text."""

    def __init__(self, code):
        super().__init__(MESSAGES[code])
        self.code = code
        self.message = MESSAGES[code]

RECORD_REJECTED = 3
CONFINEMENT_VIOLATION = 5
INPUT_LIMIT = 7
PRECONDITION_MISSING = 9


class ContractError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def rejected(message):
    return ContractError(RECORD_REJECTED, message)
