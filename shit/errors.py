"""The one error type for contract validation, carrying a stable exit code from docs/exit-codes.md.

Messages are fixed strings plus a field name. They never contain the rejected value, so a secret or
private term that lands in a rejected input is not copied into a log or a report.
"""

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
