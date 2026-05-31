#!/usr/bin/env python3
"""Counterfactual re-band of the router audit log.

Each audit row records the decision's ``confidence``, ``model``, ``band``, and
the ``thresholds`` in force at the time. Because the band is a pure function of
(confidence, thresholds, model) — ``band_for`` in hooks/auto-router.py — we can
project what the band distribution WOULD be under candidate thresholds without
re-classifying any prompt. This is the router loop's offline feasibility check.

CLI:
  python3 replay_kpi.py --auto 0.72 --ask 0.60 [--audit PATH] [--since ISO]
  -> JSON {n, recorded:{band:count}, candidate:{band:count}}
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from collections import Counter
from datetime import datetime

_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(_PLUGIN_ROOT, "hooks", "auto-router.py")
DEFAULT_AUDIT = os.path.expanduser(
    os.path.join(
        os.environ.get("CC_ROUTER_CACHE_DIR", "~/.claude/cache/router"),
        "audit.jsonl",
    )
)


def import_band_for(hook_path: str = HOOK):
    """Load the pure band_for() from the (hyphen-named) hook module via importlib."""
    spec = importlib.util.spec_from_file_location("auto_router_hook", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.band_for


def iter_decisions(audit_path: str, since: str | None = None):
    if not os.path.exists(audit_path):
        return
    cutoff = datetime.fromisoformat(since) if since else None
    with open(audit_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            dec = row.get("decision")
            if not dec or "confidence" not in dec:
                continue
            if cutoff:
                ts = row.get("ts")
                try:
                    if ts and datetime.fromisoformat(ts) < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
            yield dec


def recorded_distribution(decisions) -> dict:
    return dict(Counter(d.get("band") for d in decisions))


def reband_distribution(decisions, auto_threshold, ask_threshold, band_for) -> dict:
    c: Counter = Counter()
    for d in decisions:
        c[band_for(d["confidence"], auto_threshold, ask_threshold, d.get("model", ""))] += 1
    return dict(c)


def reband_with_own_thresholds(decisions, band_for) -> dict:
    """Re-band each decision under the thresholds it was recorded with. Should
    reproduce the recorded distribution exactly (the no-drift identity check)."""
    c: Counter = Counter()
    for d in decisions:
        th = d.get("thresholds") or {}
        if "auto" not in th or "ask" not in th:
            continue
        c[band_for(d["confidence"], th["auto"], th["ask"], d.get("model", ""))] += 1
    return dict(c)


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--audit", default=DEFAULT_AUDIT)
    p.add_argument("--auto", type=float, required=True)
    p.add_argument("--ask", type=float, required=True)
    p.add_argument("--since")
    a = p.parse_args(argv)
    band_for = import_band_for()
    decs = list(iter_decisions(a.audit, a.since))
    out = {
        "n": len(decs),
        "recorded": recorded_distribution(decs),
        "candidate": reband_distribution(decs, a.auto, a.ask, band_for),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
