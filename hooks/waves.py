#!/usr/bin/env python3
"""Wave + non-interference batching for the auto-model-router.

A plan (or a decomposed prompt) is a list of steps with dependencies and
declared write-file sets. This module turns that into an execution shape
the parent can dispatch safely:

    steps  --compute_waves-->  waves            (dependency levels)
    wave   --partition_batches-->  batches      (conflict-free groups)
    steps  --plan_execution-->  list[wave[batch[step]]]

Rules (mirrors skills/plan-with-models/SKILL.md, "Non-interference"):
  1. read-only ∥ anything            -> safe
  2. write ∥ write                   -> safe iff write-file sets disjoint
  3. overlapping writes              -> different batches (serialize)
  4. a step depends_on an earlier    -> lands in a later wave
  5. read-only steps never conflict  -> they share the first batch

A `step` is a plain dict. Recognised keys:
  id          : hashable, unique. Required.
  depends_on  : list of step ids that must finish first. Default [].
  files       : list of paths the step WRITES. Default []. Empty => read-only.
  read_only   : optional explicit bool; overrides files-based inference.

Only `id` is required; everything else defaults. The functions never mutate
the input steps.
"""

from __future__ import annotations


def parse_files(value) -> list:
    """Normalise a `Files:` field into a list of write paths.

    Accepts a list (returned as-is, stripped) or a comma-separated string.
    The literal "none" (any case) and empty values yield []. Used by both
    the analyzer and any caller turning plan text into step dicts.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        text = str(value).strip()
        if not text or text.lower() == "none":
            return []
        items = [p.strip() for p in text.split(",")]
    return [p for p in items if p and p.lower() != "none"]


def is_read_only(step: dict) -> bool:
    if "read_only" in step:
        return bool(step["read_only"])
    return len(parse_files(step.get("files"))) == 0


def _conflicts(a: dict, b: dict) -> bool:
    """Two steps conflict iff both write and share at least one write path."""
    if is_read_only(a) or is_read_only(b):
        return False
    fa = set(parse_files(a.get("files")))
    fb = set(parse_files(b.get("files")))
    return not fa.isdisjoint(fb)


def compute_waves(steps: list) -> list:
    """Topologically sort steps into dependency waves.

    Wave 0 = steps with no unmet dependency; wave N = steps all of whose
    deps live in waves < N. Returns list[list[step]] preserving input order
    within each wave. Raises ValueError on a dependency cycle or a
    depends_on referencing an unknown id.
    """
    by_id = {}
    for s in steps:
        if "id" not in s:
            raise ValueError("every step needs an 'id'")
        if s["id"] in by_id:
            raise ValueError(f"duplicate step id: {s['id']!r}")
        by_id[s["id"]] = s

    level: dict = {}

    def resolve(sid, trail: tuple):
        if sid in level:
            return level[sid]
        if sid in trail:
            cycle = " -> ".join(str(x) for x in (*trail, sid))
            raise ValueError(f"dependency cycle: {cycle}")
        step = by_id.get(sid)
        if step is None:
            raise ValueError(f"unknown depends_on id: {sid!r}")
        deps = step.get("depends_on") or []
        lvl = 0 if not deps else 1 + max(resolve(d, (*trail, sid)) for d in deps)
        level[sid] = lvl
        return lvl

    for s in steps:
        resolve(s["id"], ())

    max_level = max(level.values(), default=-1)
    waves = [[] for _ in range(max_level + 1)]
    for s in steps:  # input order preserved within each wave
        waves[level[s["id"]]].append(s)
    return waves


def partition_batches(steps: list) -> list:
    """Split a set of co-runnable steps into conflict-free batches.

    Greedy first-fit: each step joins the earliest batch whose members it
    does not conflict with. Read-only steps conflict with nothing, so they
    (and the first writer of each disjoint group) land in batch 0; an
    overlapping writer spills to a later batch. Returns list[list[step]].
    """
    batches: list = []
    for step in steps:
        placed = False
        for batch in batches:
            if all(not _conflicts(step, other) for other in batch):
                batch.append(step)
                placed = True
                break
        if not placed:
            batches.append([step])
    return batches


def plan_execution(steps: list) -> list:
    """Full shape: waves (by dependency), each split into conflict-free
    batches. Returns list[wave] where wave = list[batch], batch = list[step].
    Each batch is what the parent dispatches as one message of concurrent
    Agent() calls.
    """
    return [partition_batches(wave) for wave in compute_waves(steps)]


def needs_worktree(steps: list) -> bool:
    """True if any wave contains writers that must serialize (i.e. the wave
    splits into >1 batch). Signals the executor that overlapping writes
    exist — the point where `isolation: "worktree"` becomes the alternative
    to serializing.
    """
    for wave in compute_waves(steps):
        if len(partition_batches(wave)) > 1:
            return True
    return False
