"""Yardstick report computed from this project's own task log: `python3 -m shit.yardstick [--config FILE]`.

Baseline first. Counts of tasks, events, report outcomes and review verdicts are read from the verified
event log and recorded reports, and are labeled `measured`. "Effort points" are those event counts times
configurable weights (the `[yardstick]` table in config.toml) and are labeled `estimate`. Token numbers
appear only if an adapter supplies them; none does yet, so they print as `not supplied`. The report
holds this project's numbers only and never figures from any other project. Per-task rows are capped at
MAX_ROWS so the output size stays bounded. Output is JSON, ASCII-escaped; failures print one fixed line.
"""

import json
import os
import sys
from collections import Counter

from . import commands, config
from . import task as task_mod
from ._text import decode, read_bounded
from .cli_tasks import _Parser
from .errors import INTERNAL, MESSAGES, OK, USAGE, ContractError, ShitError
from .log import EventLog
from .project import Project, discover_root

MAX_ROWS = 100
DEFAULT_WEIGHTS = {name: 1 for name in config.WEIGHT_KEYS}


def _counts(counter):
    return dict(sorted(counter.items()))


def compute(project, weights=None):
    """Return the yardstick report for an open project. Raises ShitError if the log fails verification."""
    weights = dict(DEFAULT_WEIGHTS if weights is None else weights)
    log = EventLog(project)

    def run():
        with project.locked():
            task_mod._recover(project, log)
            events = log.verify()
            outcomes, verdicts = Counter(), Counter()
            first = {}  # (kind, record_id) -> first matching event, so each load is O(1), not a log scan
            for event in events:
                first.setdefault((event["event"], event.get("record_id")), event)
            for kind, tally, key in (("report", outcomes, "outcome"), ("review", verdicts, "verdict")):
                for event in events:
                    if event["event"] == commands.EVENT[kind]:
                        rid = event.get("record_id")  # a missing id is refused by _load with exit code 3
                        match = first[(event["event"], rid)]
                        tally[commands._load(project, [match], kind, rid)[key]] += 1
            return events, outcomes, verdicts

    events, outcomes, verdicts = task_mod._guarded(run)
    by_kind = Counter(e["event"] for e in events)
    state, per_task = {}, {}
    for event in events:
        tid = event["task_id"]
        if event["event"] == "task_created":
            state[tid] = "open"
        elif event["event"] == "state_transition":
            state[tid] = event["to_state"]
        row = per_task.setdefault(tid, Counter())
        row[event["event"]] += 1
    points = sum(weights.get(kind, 0) * n for kind, n in by_kind.items())
    rows = [{"task_id": tid, "state": state.get(tid, "open"), "events": sum(c.values()),
             "points": sum(weights.get(k, 0) * n for k, n in c.items())}
            for tid, c in sorted(per_task.items())]
    return {
        "schema": "yardstick",
        "basis": "this project's own task log",
        "tasks": {"label": "measured", "total": len(state), "by_state": _counts(Counter(state.values()))},
        "events": {"label": "measured", **_counts(by_kind)},
        "reports_by_outcome": {"label": "measured", **_counts(outcomes)},
        "reviews_by_verdict": {"label": "measured", **_counts(verdicts)},
        "effort_points": {"label": "estimate", "value": points, "weights": weights},
        "tokens": {"value": None, "label": "not supplied"},
        "per_task": rows[:MAX_ROWS],
        "per_task_truncated": len(rows) > MAX_ROWS,
    }


def build_parser():
    parser = _Parser(prog="shit-yardstick")
    parser.add_argument("--config")
    return parser


def main(argv=None, *, cwd=None, stdout=None, stderr=None):
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    try:
        ns = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
        root = discover_root(cwd or os.getcwd())
        weights = None
        if ns.config is not None:  # the file must lie inside the project root
            data = read_bounded(os.path.join(root, ns.config), config.MAX_BYTES, base=root)
            weights = config.parse(decode(data))["yardstick"]
        proj = Project.open(root)
        try:
            result = compute(proj, weights)
        finally:
            proj.close()
        out.write(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False) + "\n")
        return OK
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else USAGE
    except (ShitError, ContractError) as exc:
        code = exc.code if exc.code in MESSAGES else INTERNAL
        err.write(f"shit: {MESSAGES[code]}\n")
        return code
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError, MemoryError):
        err.write(f"shit: {MESSAGES[INTERNAL]}\n")
        return INTERNAL


if __name__ == "__main__":
    sys.exit(main())
