"""Typed dependency graph between tasks (docs/queue-spec.md, Q-DEP-01 to Q-DEP-09).

Pure functions over a `dependency-graph` record: shape validation, cycle and missing-node detection,
and readiness. Nothing here touches the disk or the clock; `queue.py` stores the record and supplies the
`met` callback that says whether a prerequisite's milestone is reached, read from verified records.
"""

import json
from collections import deque

from . import records
from .errors import INPUT_LIMIT, PRECONDITION, REJECTED, ShitError

TYPES = ("draft", "apply", "review", "decision")
BLOCKING = ("draft", "review", "decision")  # an `apply` edge gates only the apply step (Q-DEP-02)
EDGE_KEYS = {"task_id", "needs_id", "type"}
GRAPH_KEYS = {"schema", "schema_version", "edges"}


def empty_graph():
    return {"schema": "dependency-graph", "schema_version": 1, "edges": []}


def _key(edge):
    return edge["task_id"], edge["needs_id"], edge["type"]


def validate_graph(obj):
    """Check shape only: strict keys and version, slugs, known types, no duplicate edge. Exit code 3 on failure."""
    if not isinstance(obj, dict):
        raise ShitError(REJECTED)
    records._keys(obj, GRAPH_KEYS, set())
    records._version(obj, "dependency-graph")
    edges = obj["edges"]
    if not isinstance(edges, list):
        raise ShitError(REJECTED)
    seen = set()
    for item in edges:
        if not isinstance(item, dict):
            raise ShitError(REJECTED)
        records._keys(item, EDGE_KEYS, set())
        records._slug(item, "task_id")
        records._slug(item, "needs_id")
        records._choice(item, "type", TYPES)
        if _key(item) in seen:
            raise ShitError(REJECTED)
        seen.add(_key(item))
    return obj


def load_graph(data):
    return validate_graph(records.parse(data))


def dump_graph(obj):
    validate_graph(obj)
    data = (json.dumps(obj, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
    if len(data) > records.MAX_RECORD_BYTES:
        raise ShitError(INPUT_LIMIT)
    return data


def find_cycle(edges):
    """True when the edges, over all types, contain a cycle or a self edge. Iterative (Kahn), no recursion."""
    needs, indegree = {}, {}
    for edge in edges:
        task, prerequisite = edge["task_id"], edge["needs_id"]
        needs.setdefault(prerequisite, []).append(task)
        indegree.setdefault(prerequisite, 0)
        indegree[task] = indegree.get(task, 0) + 1
    queue = deque(node for node, degree in indegree.items() if degree == 0)
    removed = 0
    while queue:
        node = queue.popleft()
        removed += 1
        for dependent in needs.get(node, ()):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
    return removed != len(indegree)


def missing_nodes(edges, known):
    """The edge endpoints that are not in `known` (a set of task ids)."""
    return {end for edge in edges for end in (edge["task_id"], edge["needs_id"]) if end not in known}


def _endpoints(task_id, needs_id, kind):
    probe = {"task_id": task_id, "needs_id": needs_id, "type": kind}
    validate_graph({"schema": "dependency-graph", "schema_version": 1, "edges": [probe]})
    return probe


def add_edge(graph, task_id, needs_id, kind, known):
    """Return a new graph with the edge added, or raise: 3 for a bad, duplicate or cycle-closing edge, 9 for an unknown task."""
    edge = _endpoints(task_id, needs_id, kind)
    if task_id not in known or needs_id not in known:
        raise ShitError(PRECONDITION)
    edges = [*graph["edges"], edge]
    out = validate_graph({**graph, "edges": edges})
    if find_cycle(edges):
        raise ShitError(REJECTED)
    return out


def remove_edge(graph, task_id, needs_id, kind):
    edge = _endpoints(task_id, needs_id, kind)
    if not any(_key(e) == _key(edge) for e in graph["edges"]):
        raise ShitError(PRECONDITION)
    return {**graph, "edges": [e for e in graph["edges"] if _key(e) != _key(edge)]}


def unmet_blocking(edges, task_id, met):
    """The blocking edges of `task_id` whose prerequisite milestone is not reached (`met(edge)` is False)."""
    return [e for e in edges if e["task_id"] == task_id and e["type"] in BLOCKING and not met(e)]


def apply_prerequisites(edges, task_id):
    """Ids of the tasks whose apply step must finish before `task_id` may apply (Q-SER-03)."""
    return [e["needs_id"] for e in edges if e["task_id"] == task_id and e["type"] == "apply"]
