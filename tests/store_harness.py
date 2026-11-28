"""A reusable concurrency and crash harness for the SQLite store (issue 65, C8). Test infrastructure, not a test.

Pieces:

- `Arena`: a real database in a temporary directory, with a few tasks, and a logical clock shared by every process.
- `run_workers`: N worker processes (`tests/store_worker.py`) doing random queue operations against one database for
  a bounded number of operations or seconds, with an optional schedule of SIGKILLs sent by this process at random
  times (an external timer, so a kill can land inside COMMIT as well as between statements).
- `check_invariants`: what must hold of any database that a crash left behind (see its docstring).
- `kill_rounds`: rounds of `run_workers` with kills, checking after every kill and after every round.
- `stage_sweep`: one operation killed at every fault point, deterministic, complementing the random timer.
- `mutated_root`: a copy of the package whose `shit/store.py` has one deliberate breakage, so the harness can be shown
  to bite.

Every random choice comes from one seed. A failure message carries it (`failure_note`); the same seed repeats the
operation stream of each worker and the kill schedule. The interleaving of processes is the operating system's and is
not repeated.

Settings come from environment variables (`Settings.from_env`), see docs/store.md ("Concurrency and crash harness").
"""

import hashlib
import json
import os
import random
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import store_worker
from _bootstrap import ROOT, make_git_dir

from shit import queue_records, records, store
from shit.errors import ShitError
from shit.log import GENESIS
from shit.project import Project

WORKER = str(Path(__file__).resolve().parent / "store_worker.py")
CLOCK_BASE = 1000  # logical seconds before the first worker tick, so a worker never stamps before the setup did
SETUP_TASKS = 8


def _int(name, default):
    value = os.environ.get(name)
    return default if value in (None, "") else int(value)


def _float(name, default):
    value = os.environ.get(name)
    return default if value in (None, "") else float(value)


@dataclass(frozen=True)
class Settings:
    seed: int
    workers: int
    ops: int
    seconds: float
    rounds: int
    round_seconds: float
    scale: int
    busy_ms: int
    model_seeds: int
    model_length: int
    long: bool

    @classmethod
    def from_env(cls):
        long = os.environ.get("SHIT_HARNESS_LONG") == "1"
        seed = os.environ.get("SHIT_HARNESS_SEED")
        return cls(
            seed=int(seed) if seed else secrets.randbelow(2 ** 31),
            workers=_int("SHIT_HARNESS_WORKERS", 6 if long else 3),
            ops=_int("SHIT_HARNESS_OPS", 1500 if long else 80),
            seconds=_float("SHIT_HARNESS_SECONDS", 120.0 if long else 20.0),
            rounds=_int("SHIT_HARNESS_ROUNDS", 25 if long else 4),
            round_seconds=_float("SHIT_HARNESS_ROUND_SECONDS", 4.0 if long else 1.5),
            scale=_int("SHIT_HARNESS_SCALE", 30),
            busy_ms=_int("SHIT_HARNESS_BUSY_MS", 30000),
            model_seeds=_int("SHIT_HARNESS_MODEL_SEEDS", 40 if long else 3),
            model_length=_int("SHIT_HARNESS_MODEL_LENGTH", 300 if long else 120),
            long=long)

    def failure_note(self):
        return (f"seed={self.seed}: repeat with SHIT_HARNESS_SEED={self.seed} (the operation stream of every worker and the kill "
                "schedule repeat; the interleaving of processes does not)")


class Arena:
    """One database in a temporary directory, created through the real store, and the logical clock of a run."""

    def __init__(self, settings, root=ROOT):
        self.settings, self.root = settings, Path(root)
        self._tmp = tempfile.TemporaryDirectory(prefix="shit-harness-")
        self.dir = Path(self._tmp.name).resolve()
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        make_git_dir(self.repo / ".git")
        self.project = Project.init(self.repo)
        store.init_store(self.project)
        self.conn = store.connect(self.project, busy_timeout_ms=settings.busy_ms)
        self.tasks = ["t" + str(n) for n in range(SETUP_TASKS)]
        for number, name in enumerate(self.tasks):
            stamp = store_worker.stamp(number)
            task = {"schema": "task", "schema_version": 1, "id": name, "title": "Title", "summary": "Summary",
                    "state": "open", "owner": "human", "created_at": stamp, "updated_at": stamp, "history": []}
            data = records.dump_task(task)
            store.create_task(self.conn, task, "human", hashlib.sha256(data).hexdigest(), len(data))
            if number < SETUP_TASKS // 2:
                store.queue_enqueue(self.conn, name, "human", stamp, config=store_worker.CONFIG)
        self.epoch = time.time()
        self.journals = []

    def now(self):
        return store_worker.stamp(CLOCK_BASE + int((time.time() - self.epoch) * self.settings.scale))

    def close(self):
        self.conn.close()
        self.project.close()
        self._tmp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@dataclass
class Report:
    """What one `run_workers` call saw. `problems` is the list of everything that should not have happened."""

    problems: list
    counts: dict
    claims: list
    killed: list
    exit_codes: list

    def ok_kinds(self):
        return {key.split(":")[0] for key, n in self.counts.items() if key.endswith(":ok") and n}

    def total(self, suffix):
        return sum(n for key, n in self.counts.items() if key.endswith(":" + suffix))


def _read_journal(path):
    lines = []
    try:
        data = Path(path).read_text()
    except FileNotFoundError:
        return lines
    for text in data.splitlines():
        try:
            lines.append(json.loads(text))
        except ValueError:
            pass  # a worker killed in the middle of its last write
    return lines


def run_workers(arena, *, seed, round_no=0, workers=None, ops=None, seconds=None, kills=(), selfkill=False,
                check_every=3, root=None, watch=True):
    """Run worker processes against `arena` and return a `Report`.

    `kills` is a list of `(delay seconds, worker index)`: this process sends SIGKILL to that worker at that time after
    the start (a kill of a worker that already ended does nothing). With `watch`, every kill is followed by a snapshot
    check of the database while the surviving workers keep writing. `selfkill` makes each worker kill itself at a
    random fault point of one random operation."""
    settings = arena.settings
    workers = settings.workers if workers is None else workers
    ops = settings.ops if ops is None else ops
    seconds = settings.seconds if seconds is None else seconds
    root = arena.root if root is None else Path(root)
    rng = random.Random(seed)
    problems, procs, journals = [], [], []
    deadline = time.time() + seconds
    try:
        for index in range(workers):
            journal = arena.dir / f"journal-{round_no}-{index}.jsonl"
            journals.append(journal)
            spec = {"root": str(root), "repo": str(arena.repo), "seed": rng.randrange(2 ** 31), "index": index,
                    "round": round_no, "ops": ops, "deadline": deadline, "epoch": arena.epoch,
                    "clock_base": CLOCK_BASE, "scale": settings.scale, "busy_ms": settings.busy_ms,
                    "journal": str(journal), "tasks": arena.tasks, "check_every": check_every,
                    "selfkill": selfkill}
            procs.append(subprocess.Popen([sys.executable, "-I", WORKER, json.dumps(spec)],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        killed = _kill_on_schedule(arena, procs, sorted(kills), deadline, problems, watch)
        counts, exit_codes = {}, []
        for index, proc in enumerate(procs):
            try:
                out, err = proc.communicate(timeout=max(5.0, deadline - time.time() + 60))
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                problems.append(f"worker {index} did not finish")
            exit_codes.append(proc.returncode)
            if proc.returncode not in (0, -signal.SIGKILL):
                problems.append("worker {} exited {}: {}".format(index, proc.returncode, err[-300:].decode(errors="replace")))
            for line in out.decode().splitlines():
                for key, n in json.loads(line)["counts"].items():
                    counts[key] = counts.get(key, 0) + n
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    claims = []
    for journal in journals:
        for entry in _read_journal(journal):
            if entry["kind"] == "claim":
                claims.append(entry)
            elif entry["kind"] == "violation":
                problems.append("snapshot check in {}: {}".format(entry["worker"], entry["found"]))
            elif entry["kind"] == "suspicious":
                problems.append("{} got exit code {} from {}".format(entry["worker"], entry["code"], entry["op"]))
    return Report(problems, counts, claims, killed, exit_codes)


def _kill_on_schedule(arena, procs, plan, deadline, problems, watch):
    start, killed = time.time(), []
    while plan:
        delay, index = plan[0]
        wait = start + delay - time.time()
        if wait > 0:
            time.sleep(min(wait, 0.01))
            continue
        plan.pop(0)
        proc = procs[index % len(procs)]
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            killed.append((round(time.time() - start, 3), index))
            if watch:
                found = snapshot_problems(arena.conn)
                if found:
                    problems.append(f"after the kill of worker {index}: {found}")
        if time.time() > deadline + 30:
            break
    return killed


def snapshot_problems(conn):
    """`verify` and `verify_tasks` on one consistent snapshot: safe to run while other processes write."""
    with store.read_snapshot(conn):
        return store.verify(conn) + store.verify_tasks(conn)


def check_invariants(conn, claims=()):
    """Every problem a quiet database shows after a crash, as short strings (empty: the database is sound).

    - `store.verify` (deep, so `PRAGMA integrity_check` too) and `store.verify_tasks` report nothing;
    - `queue_records.replay` accepts the whole log;
    - the hash chain of the queue log is contiguous (numbers 1, 2, 3 ..., each entry's previous hash is the hash of the
      one before; the digests themselves are recomputed for the events table by `verify_tasks`, not for the queue log,
      whose line the harness cannot rebuild byte for byte);
    - every claim has one winner: per task the claim generations are 1, 2, 3 ... with no repeat, no lease id repeats,
      and each claim a worker was told succeeded (`claims`, from the journals) is in the log exactly once and no two
      workers were told the same generation of one task;
    - a lease row exists exactly when the replayed log says a lease is live, with the same lease id.
    """
    found = []
    with store.read_snapshot(conn):
        found += ["verify: " + name for name in store.verify(conn, deep=True)]
        found += ["verify_tasks: " + name for name in store.verify_tasks(conn)]
        entries = store.queue_entries(conn)
        rows = conn.execute("SELECT seq, prev_hash, hash FROM queue_entries ORDER BY seq").fetchall()
        leases = {row[0]: row[1] for row in conn.execute("SELECT task_id, lease_id FROM leases").fetchall()}
    try:
        replayed = queue_records.replay(entries)
    except ShitError as exc:
        return found + [f"replay refused the log (exit {exc.code})"]
    previous = GENESIS
    for number, (seq, prev_hash, digest) in enumerate(rows, start=1):
        if seq != number or prev_hash != previous:
            found.append(f"queue hash chain broken at entry {number}")
            break
        previous = digest
    generations, lease_ids = {}, set()
    for entry in entries:
        if entry["kind"] != "claimed":
            continue
        seen = generations.setdefault(entry["task_id"], [])
        if entry["generation"] != len(seen) + 1:
            found.append("claim generation {} of {} follows {} claims".format(entry["generation"], entry["task_id"], len(seen)))
        seen.append(entry["generation"])
        if entry["lease_id"] in lease_ids:
            found.append("lease id {} claimed twice".format(entry["lease_id"]))
        lease_ids.add(entry["lease_id"])
    told = {}
    for claim in claims:
        key = (claim["task"], claim["generation"])
        if key in told and told[key] != claim["lease_id"]:
            found.append(f"two winners for {key}")
        told[key] = claim["lease_id"]
        matches = [e for e in entries if e["kind"] == "claimed" and e["task_id"] == claim["task"]
                   and e["generation"] == claim["generation"] and e["lease_id"] == claim["lease_id"]]
        if len(matches) != 1:
            found.append(f"a claim the worker was told succeeded is in the log {len(matches)} times: {key}")
    for task_id, queued in replayed.items():
        lease = queued["lease"]
        if (lease is None) != (task_id not in leases) or (lease is not None and lease["lease_id"] != leases[task_id]):
            found.append(f"lease row of {task_id} differs from the log")
    for task_id in leases:
        if task_id not in replayed:
            found.append(f"lease row of {task_id} with no queue entry")
    return found


def probe_writable(arena):
    """A dead writer must hold no lock: one more write goes through at once. Returns the problems, if any."""
    started = time.time()
    try:
        store.queue_maintain(arena.conn, arena.now(), config=store_worker.CONFIG)
    except ShitError as exc:
        return [f"a write after the kill was refused with exit code {exc.code}"]
    if time.time() - started > 10:
        return [f"a write after the kill waited {time.time() - started:.1f} seconds"]
    return []


def kill_rounds(settings, root=ROOT, rounds=None):
    """Rounds of workers with random SIGKILLs, then invariants after every kill (snapshot) and every round.

    Odd rounds also make each worker kill itself at a random fault point of one random operation. Returns
    `(problems, summary)`; `summary` counts what happened so a caller can assert the run did something."""
    rng = random.Random(settings.seed)
    problems, summary = [], {"rounds": 0, "killed": 0, "ops_ok": 0, "claims": 0, "selfkilled": 0}
    claims = []
    with Arena(settings, root) as arena:
        for round_no in range(settings.rounds if rounds is None else rounds):
            plan = [(rng.uniform(0.05, settings.round_seconds * 0.9), rng.randrange(settings.workers))
                    for _ in range(rng.randint(1, settings.workers))]
            report = run_workers(arena, seed=rng.randrange(2 ** 31), round_no=round_no, ops=10 ** 6,
                                 seconds=settings.round_seconds, kills=plan, selfkill=round_no % 2 == 1)
            claims += report.claims
            problems += [f"round {round_no}: {p}" for p in report.problems]
            problems += [f"round {round_no}: {p}" for p in check_invariants(arena.conn, claims)]
            problems += [f"round {round_no}: {p}" for p in probe_writable(arena)]
            summary["rounds"] += 1
            summary["killed"] += len(report.killed)
            summary["ops_ok"] += report.total("ok")
            summary["claims"] += len(report.claims)
            summary["selfkilled"] += report.exit_codes.count(-signal.SIGKILL) - len(report.killed)
    return problems, summary


# -- the deterministic sweep: one operation, killed at every fault point --------------------------------------

SWEEP_CHILD = """
import json, os, signal, sys
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["root"])
from shit import store
from shit.project import Project
conn = store.connect(Project.open(spec["repo"]))
seen = []
def fault(stage):
    seen.append(stage)
    if len(seen) - 1 == spec["kill_at"]:
        os.kill(os.getpid(), signal.SIGKILL)
store._fault = fault
args = spec["args"]
getattr(store, spec["op"])(conn, *args, config=spec["config"])
print(json.dumps(seen))
"""


def stage_sweep(settings, root=ROOT):
    """Kill a claim, a renewal and a submit at every fault point they reach; the database must be sound each time.

    Returns `(problems, stages)`: `stages` maps each swept operation to the fault points it reaches."""
    problems, stages = [], {}
    config = store_worker.CONFIG
    with Arena(settings, root) as arena:
        conn, tick = arena.conn, [0]

        def stamp():
            tick[0] += 1
            return store_worker.stamp(500 + tick[0])

        def present(task):
            lease = store.queue_state(conn, task)["lease"]
            return {"task_id": task, "lease_id": lease["lease_id"], "generation": lease["generation"]}

        for label in ("claim", "renew", "submit"):
            kill_at = 0
            while True:
                # a fresh state for every attempt: a task claimed (and started, for a submit) by this process
                name = f"sw-{label}-{kill_at}"
                _new_task(conn, name, stamp())
                if label != "claim":
                    request = {"task_id": name, "step": "draft", "worker": "w1", "worker_kind": "ai"}
                    store.queue_claim(conn, request, stamp(), config=config)
                    if label == "submit":
                        store.queue_start(conn, present(name), stamp(), config=config)
                if label == "claim":
                    op, args = "queue_claim", [{"task_id": name, "step": "draft", "worker": "w1", "worker_kind": "ai"},
                                              stamp()]
                else:
                    op, args = "queue_" + label, [present(name), stamp()]
                spec = {"root": str(arena.root), "repo": str(arena.repo), "kill_at": kill_at, "op": op, "args": args,
                        "config": config}
                proc = subprocess.run([sys.executable, "-I", "-c", SWEEP_CHILD, json.dumps(spec)],
                                      capture_output=True, timeout=120, check=False)
                if proc.returncode == 0:
                    stages[label] = json.loads(proc.stdout.decode())
                    break
                if proc.returncode != -signal.SIGKILL:
                    problems.append("{} sweep child exited {}: {}".format(label, proc.returncode,
                                                                          proc.stderr[-300:].decode(errors="replace")))
                    break
                found = check_invariants(conn)
                problems += [f"{label} killed at fault point {kill_at}: {p}" for p in found]
                kill_at += 1
                if kill_at > 40:
                    problems.append(f"{label} sweep did not end")
                    break
    return problems, stages


def _new_task(conn, name, stamp):
    task = {"schema": "task", "schema_version": 1, "id": name, "title": "Title", "summary": "Summary",
            "state": "open", "owner": "human", "created_at": stamp, "updated_at": stamp, "history": []}
    data = records.dump_task(task)
    store.create_task(conn, task, "human", hashlib.sha256(data).hexdigest(), len(data))
    store.queue_enqueue(conn, name, "human", stamp, config=store_worker.CONFIG)


# -- mutations: deliberate breakages of a copy of the store ----------------------------------------------------


@dataclass(frozen=True)
class Mutation:
    name: str
    old: str  # text that occurs exactly once in shit/store.py
    new: str
    detector: str  # which part of the harness must notice: "processes", "sweep" or "model"


MUTATIONS = (
    Mutation("drop-begin-immediate", 'try:\n        conn.execute("BEGIN IMMEDIATE")\n    except sqlite3.Error as exc:\n        raise map_error(exc) from None\n    try:\n        yield conn',
             'try:\n        conn.execute("BEGIN")\n    except sqlite3.Error as exc:\n        raise map_error(exc) from None\n    try:\n        yield conn', "processes"),
    Mutation("ignore-busy-timeout", "    timeout = _timeout(busy_timeout_ms)\n", "    timeout = 0\n", "processes"),
    Mutation("skip-lease-row", "def _insert_lease(conn, lease):\n    _require_transaction(conn)\n",
             "def _insert_lease(conn, lease):\n    _require_transaction(conn)\n    return\n", "processes"),
    Mutation("drop-lease-before-slots",
             '    conn.execute("DELETE FROM apply_slots WHERE task_id = ?", (task_id,))\n    _fault("slots-dropped")\n'
             '    conn.execute("DELETE FROM leases WHERE task_id = ?", (task_id,))\n    _fault("lease-dropped")',
             '    conn.execute("DELETE FROM leases WHERE task_id = ?", (task_id,))\n    _fault("slots-dropped")\n'
             '    conn.execute("DELETE FROM apply_slots WHERE task_id = ?", (task_id,))\n    _fault("lease-dropped")',
             "model"),
    Mutation("commit-between-entry-and-lease-row",
             '    _require_transaction(conn)\n    conn.execute("INSERT INTO leases (task_id,',
             '    _require_transaction(conn)\n    conn.execute("COMMIT")\n    conn.execute("BEGIN IMMEDIATE")\n'
             '    conn.execute("INSERT INTO leases (task_id,', "sweep"),
)


def mutated_root(directory, mutation):
    """A copy of the package under `directory` with `mutation` applied to its `store.py`. Returns the new root.

    The mutation must match exactly once: a breakage that silently changes nothing would prove nothing."""
    text = (ROOT / "shit" / "store.py").read_text()
    if text.count(mutation.old) != 1:
        raise AssertionError(f"mutation {mutation.name} matches {text.count(mutation.old)} times")
    root = Path(directory) / mutation.name
    shutil.copytree(ROOT / "shit", root / "shit", ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
    (root / "shit" / "store.py").write_text(text.replace(mutation.old, mutation.new))
    return root


def run_model_in_process(root, seeds, length):
    """The model comparison (`tests/store_model.py`) run as a child that imports `shit` from `root`."""
    spec = {"root": str(root), "seeds": list(seeds), "length": length}
    proc = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve().parent / "store_model.py"),
                           json.dumps(spec)], capture_output=True, timeout=600, check=False)
    if proc.returncode != 0:
        return ["the model run exited {}: {}".format(proc.returncode, proc.stderr[-300:].decode(errors="replace"))], {}
    result = json.loads(proc.stdout.decode().splitlines()[-1])
    return result["problems"], result["seen"]


def detect(settings, mutation, directory):
    """The problems the harness reports against a copy of the package carrying `mutation` (empty: it did not notice)."""
    root = mutated_root(directory, mutation)
    if mutation.detector == "model":
        return run_model_in_process(root, range(1, 4), 120)[0]
    if mutation.detector == "sweep":
        return stage_sweep(settings, root)[0]
    with Arena(settings, root) as arena:
        report = run_workers(arena, seed=settings.seed, ops=60, seconds=20.0, check_every=1)
        return report.problems + check_invariants(arena.conn, report.claims)
