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


# -- per-task shards ---------------------------------------------------------
#
# The graph is stored as one `dependency-shard` record per dependent task (`deps/<task-id>.json`) that
# lists the tasks it needs. Each edge lives in exactly one shard. A shard is capped at MAX_NEEDS_PER_TASK
# edges (well under the 65,536-byte record limit) and the graph at MAX_TASKS shards.

MAX_NEEDS_PER_TASK = 200
MAX_TASKS = 2000
SHARD_KEYS = {"schema", "schema_version", "task_id", "needs"}
NEED_KEYS = {"needs_id", "type"}


def _sorted_needs(needs):
    return sorted(needs, key=lambda n: (n["needs_id"], n["type"]))


def validate_shard(obj, task_id):
    """Check one shard: strict keys, the id equals `task_id` (the file name), slugs, known types, no duplicate."""
    if not isinstance(obj, dict):
        raise ShitError(REJECTED)
    records._keys(obj, SHARD_KEYS, set())
    records._version(obj, "dependency-shard")
    if records._slug(obj, "task_id") != task_id:
        raise ShitError(REJECTED)
    needs = obj["needs"]
    if not isinstance(needs, list):
        raise ShitError(REJECTED)
    if len(needs) > MAX_NEEDS_PER_TASK:
        raise ShitError(INPUT_LIMIT)
    seen = set()
    for item in needs:
        if not isinstance(item, dict):
            raise ShitError(REJECTED)
        records._keys(item, NEED_KEYS, set())
        records._slug(item, "needs_id")
        records._choice(item, "type", TYPES)
        key = (item["needs_id"], item["type"])
        if key in seen:
            raise ShitError(REJECTED)
        seen.add(key)
    return obj


def load_shard(data, task_id):
    return validate_shard(records.parse(data), task_id)


def dump_shard(task_id, needs):
    """Deterministic bytes for a shard: needs sorted, ASCII-escaped. Over the per-task cap is exit code 7."""
    obj = {"schema": "dependency-shard", "schema_version": 1, "task_id": task_id, "needs": _sorted_needs(needs)}
    validate_shard(obj, task_id)
    data = (json.dumps(obj, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
    if len(data) > records.MAX_RECORD_BYTES:
        raise ShitError(INPUT_LIMIT)
    return data


def group_by_task(edges):
    """{dependent task id: [{needs_id, type}, ...]} for the edges."""
    out = {}
    for e in edges:
        out.setdefault(e["task_id"], []).append({"needs_id": e["needs_id"], "type": e["type"]})
    return {task: _sorted_needs(needs) for task, needs in out.items()}


def edges_of(task_id, needs):
    return [{"task_id": task_id, "needs_id": n["needs_id"], "type": n["type"]} for n in needs]


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


def add_edges(graph, additions, known):
    """Return a new graph with every `(task_id, needs_id, type)` added, or raise and change nothing.

    3 for a bad, duplicate or cycle-closing edge, 9 for an unknown task, 7 past the per-task or total cap.
    """
    edges = list(graph["edges"])
    for task_id, needs_id, kind in additions:
        edge = _endpoints(task_id, needs_id, kind)
        if task_id not in known or needs_id not in known:
            raise ShitError(PRECONDITION)
        edges.append(edge)
    out = validate_graph({**graph, "edges": edges})
    grouped = group_by_task(edges)
    if len(grouped) > MAX_TASKS or any(len(n) > MAX_NEEDS_PER_TASK for n in grouped.values()):
        raise ShitError(INPUT_LIMIT)
    if find_cycle(edges):
        raise ShitError(REJECTED)
    return out


def add_edge(graph, task_id, needs_id, kind, known):
    """Return a new graph with the edge added (see `add_edges` for the errors)."""
    return add_edges(graph, [(task_id, needs_id, kind)], known)


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
