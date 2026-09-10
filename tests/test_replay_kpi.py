#!/usr/bin/env python3
"""Unit tests for tools/replay_kpi.py — the offline counterfactual re-band.

Imports the REAL band_for from hooks/auto-router.py so the test catches any
drift between the replay and the live hook.
"""

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"),
)

import replay_kpi

BF = replay_kpi.import_band_for()


def _mk(conf, model, auto, ask):
    return {
        "confidence": conf,
        "model": model,
        "thresholds": {"auto": auto, "ask": ask},
        "band": BF(conf, auto, ask, model),
    }


def _check(name, cond):
    if cond:
        print(f"PASS  {name}")
        return 0
    print(f"FAIL  {name}")
    return 1


fail = 0

# Identity: replaying each row under its OWN recorded thresholds reproduces the
# recorded band distribution exactly (no drift from the live hook).
decs = [
    _mk(0.75, "sonnet", 0.75, 0.60),
    _mk(0.85, "opus", 0.75, 0.60),
    _mk(0.55, "haiku", 0.75, 0.60),
    _mk(0.95, "opus", 0.75, 0.60),
]
fail += _check(
    "identity: own-threshold replay == recorded distribution",
    replay_kpi.reband_with_own_thresholds(decs, BF)
    == replay_kpi.recorded_distribution(decs),
)

# Two bands only: the `ask` band is gone, so raising the auto threshold no
# longer strands a mid-confidence sonnet decision — it still routes, and the
# downhill-only injection gate (not the threshold) decides whether it fires.
one = [_mk(0.75, "sonnet", 0.75, 0.60)]
dist = replay_kpi.reband_distribution(one, 0.80, 0.60, BF)
fail += _check(
    "counterfactual: raising auto 0.75->0.80 keeps sonnet routable, no ask band",
    dist.get("auto") == 1 and dist.get("ask", 0) == 0,
)

# The per-model opus floor is inert for the same reason: routing UP is
# impossible now, so opus decisions band on confidence alone.
opus = [_mk(0.85, "opus", 0.75, 0.60)]
dist2 = replay_kpi.reband_distribution(opus, 0.75, 0.60, BF)
fail += _check("opus at 0.85 bands auto", dist2.get("auto") == 1)
opus_below = [_mk(0.80, "opus", 0.75, 0.60)]
dist2b = replay_kpi.reband_distribution(opus_below, 0.75, 0.60, BF)
fail += _check(
    "opus at 0.80 bands auto (0.85 floor no longer gates)",
    dist2b.get("auto") == 1,
)

# Below the route floor the hook still says nothing.
low = [_mk(0.55, "haiku", 0.75, 0.60)]
dist2c = replay_kpi.reband_distribution(low, 0.75, 0.60, BF)
fail += _check("0.55 confidence bands none", dist2c.get("none") == 1)

# Distribution counts sum to the number of decisions.
dist3 = replay_kpi.reband_distribution(decs, 0.75, 0.60, BF)
fail += _check("distribution counts sum to n", sum(dist3.values()) == len(decs))

print(f"--- replay_kpi: {'OK' if fail == 0 else f'{fail} FAILED'}")
sys.exit(1 if fail else 0)
