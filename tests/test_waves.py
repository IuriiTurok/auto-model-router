#!/usr/bin/env python3
"""Unit tests for hooks/waves.py — the wave + non-interference algorithm.

Pure-Python, no deps. Run: python3 tests/test_waves.py
Exit 0 if all pass; 1 otherwise.
"""

import os
import sys
import tempfile

# Isolate any router cache/audit writes from the real ~/.claude/cache/router/.
# waves.py is pure today, but this keeps the suite from touching the prod cache
# if it ever grows to exercise auto-router.py. Mirrors tests/run.sh.
os.environ.setdefault("CC_ROUTER_CACHE_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))

from waves import (  # noqa: E402
    compute_waves,
    partition_batches,
    plan_execution,
    parse_files,
    needs_worktree,
)

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}")


def ids(groups):
    return [[s["id"] for s in g] for g in groups]


# (a) read-only steps batch together
ro = [{"id": "a", "files": []}, {"id": "b", "files": "none"}, {"id": "c"}]
check(
    "a: read-only steps share one batch",
    ids(partition_batches(ro)) == [["a", "b", "c"]],
)

# (b) disjoint writers batch together
disjoint = [{"id": "a", "files": ["x.py"]}, {"id": "b", "files": ["y.py"]}]
check(
    "b: disjoint writers share one batch",
    ids(partition_batches(disjoint)) == [["a", "b"]],
)

# (c) overlapping writers land in separate batches
overlap = [{"id": "a", "files": ["x.py"]}, {"id": "b", "files": ["x.py", "z.py"]}]
check(
    "c: overlapping writers split into 2 batches",
    ids(partition_batches(overlap)) == [["a"], ["b"]],
)

# read-only mixed with disjoint writers all share batch 0; overlapping writer spills
mixed = [
    {"id": "r", "files": []},
    {"id": "w1", "files": ["x.py"]},
    {"id": "w2", "files": ["y.py"]},
    {"id": "w3", "files": ["x.py"]},  # conflicts with w1
]
check(
    "mixed: r+w1+w2 in batch0, w3 in batch1",
    ids(partition_batches(mixed)) == [["r", "w1", "w2"], ["w3"]],
)

# (d) depends_on pushes a step to a later wave
dep = [
    {"id": 1, "files": ["a.py"]},
    {"id": 2, "files": ["b.py"], "depends_on": [1]},
]
check("d: dependent step is in a later wave", ids(compute_waves(dep)) == [[1], [2]])

# (e) diamond DAG -> 3 waves (1 -> {2,3} -> 4)
diamond = [
    {"id": 1},
    {"id": 2, "depends_on": [1]},
    {"id": 3, "depends_on": [1]},
    {"id": 4, "depends_on": [2, 3]},
]
check(
    "e: diamond DAG yields 3 waves", ids(compute_waves(diamond)) == [[1], [2, 3], [4]]
)

# diamond full execution: wave1=[[1]], wave2=[[2,3]] (read-only => one batch), wave3=[[4]]
diamond_exec = [
    [[s["id"] for s in b] for b in wave] for wave in plan_execution(diamond)
]
check("e2: diamond plan_execution shape", diamond_exec == [[[1]], [[2, 3]], [[4]]])

# cycle detection
try:
    compute_waves([{"id": 1, "depends_on": [2]}, {"id": 2, "depends_on": [1]}])
    check("cycle raises ValueError", False)
except ValueError:
    check("cycle raises ValueError", True)

# unknown dependency
try:
    compute_waves([{"id": 1, "depends_on": [99]}])
    check("unknown depends_on raises ValueError", False)
except ValueError:
    check("unknown depends_on raises ValueError", True)

# parse_files normalisation
check("parse_files: comma string", parse_files("a.py, b.py") == ["a.py", "b.py"])
check("parse_files: none -> []", parse_files("none") == [])
check("parse_files: empty -> []", parse_files("") == [] and parse_files(None) == [])
check(
    "parse_files: list passthrough", parse_files(["a.py", " b.py "]) == ["a.py", "b.py"]
)

# needs_worktree signals overlapping writes within a wave
check("needs_worktree: overlap -> True", needs_worktree(overlap) is True)
check("needs_worktree: disjoint -> False", needs_worktree(disjoint) is False)

print("---")
print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
