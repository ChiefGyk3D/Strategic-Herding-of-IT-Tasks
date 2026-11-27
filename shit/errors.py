"""Stable exit codes from docs/exit-codes.md. Errors carry only a code and its fixed message."""

MESSAGES = {
    1: "internal error",
    2: "usage error",
    3: "record rejected",
    4: "illegal lifecycle transition",
    5: "confinement violation",
    7: "input exceeds limit",
    9: "precondition missing",
    10: "human approval required",
}

OK = 0
INTERNAL = 1
USAGE = 2
REJECTED = 3
ILLEGAL_TRANSITION = 4
CONFINEMENT = 5
INPUT_LIMIT = 7
PRECONDITION = 9
HUMAN_APPROVAL = 10


class ShitError(Exception):
    """An error that maps to one stable exit code. It never carries raw paths, content or OS text."""

    def __init__(self, code):
        super().__init__(MESSAGES[code])
        self.code = code
        self.message = MESSAGES[code]
