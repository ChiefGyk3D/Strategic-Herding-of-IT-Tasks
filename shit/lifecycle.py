"""Lifecycle transition table from docs/lifecycle.md. A pair absent from LEGAL is illegal."""

from .errors import ILLEGAL_TRANSITION, ShitError

STATES = ("open", "running", "review", "approved", "closed", "blocked", "failed")
STARTING_STATES = frozenset({"open"})
ACTORS = ("human", "wrapper", "model")

HW = frozenset({"human", "wrapper"})
H = frozenset({"human"})
W = frozenset({"wrapper"})

LEGAL = {
    ("open", "running"): W,
    ("open", "blocked"): HW,
    ("open", "failed"): HW,
    ("running", "review"): W,
    ("running", "blocked"): W,
    ("running", "failed"): W,
    ("review", "open"): H,
    ("review", "approved"): H,
    ("review", "blocked"): HW,
    ("review", "failed"): HW,
    ("approved", "closed"): HW,
    ("blocked", "open"): H,
    ("blocked", "running"): W,
    ("blocked", "failed"): HW,
    ("failed", "open"): H,
}


def check_transition(frm, to, actor):
    """Raise ShitError(4) unless `actor` may move a task from `frm` to `to`."""
    if frm not in STATES or to not in STATES or actor not in ACTORS:
        raise ShitError(ILLEGAL_TRANSITION)
    if actor not in LEGAL.get((frm, to), ()):
        raise ShitError(ILLEGAL_TRANSITION)
