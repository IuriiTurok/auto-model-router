#!/usr/bin/env python3
"""Unit tests for the pure decision helpers in tools/router_loop.py.

The I/O-heavy adapter methods (execute/measure, which shell out to usage-report
and the fixtures) are exercised by the end-to-end check, not here.
"""

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"),
)

import router_loop as rl


def _mk(conf, model, band):
    return {"confidence": conf, "model": model, "band": band}


def _check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    return 0 if cond else 1


fail = 0

# --- distribution_gate ---
fail += _check(
    "distribution sane: balanced shift passes",
    rl.distribution_gate(
        {"auto": 40, "ask": 50, "none": 10}, {"auto": 50, "ask": 40, "none": 10}
    )
    is True,
)
fail += _check(
    "distribution insane: ~all-auto fails (auto share > ceiling)",
    rl.distribution_gate({"auto": 40, "ask": 50, "none": 10}, {"auto": 100}) is False,
)
fail += _check(
    "distribution insane: none-band grew fails",
    rl.distribution_gate(
        {"auto": 40, "ask": 50, "none": 10}, {"auto": 50, "ask": 30, "none": 20}
    )
    is False,
)

# --- propose_threshold ---
reclaimable = [_mk(0.72, "sonnet", "ask")] * 8 + [_mk(0.95, "sonnet", "auto")] * 2
fail += _check(
    "propose: reclaimable ask mass -> down-step to 0.70",
    rl.propose_threshold(0.75, reclaimable, step=0.05, floor=0.65, min_mass=5) == 0.70,
)
no_mass = [_mk(0.95, "sonnet", "auto")] * 10
fail += _check(
    "propose: no reclaimable mass -> None",
    rl.propose_threshold(0.75, no_mass, step=0.05, floor=0.65, min_mass=5) is None,
)
fail += _check(
    "propose: respects floor (would go below 0.65) -> None",
    rl.propose_threshold(0.66, reclaimable, step=0.05, floor=0.65, min_mass=5) is None,
)
fail += _check(
    "propose: opus ask mass is NOT reclaimable (0.90 floor) -> None",
    rl.propose_threshold(
        0.75, [_mk(0.72, "opus", "ask")] * 8, step=0.05, floor=0.65, min_mass=5
    )
    is None,
)

# --- thresholds_valid (structural safety net for forced/override candidates) ---
fail += _check("thresholds valid: 0.72 > 0.60 in-band", rl.thresholds_valid(0.72, 0.60) is True)
fail += _check("thresholds valid: at floor 0.65 ok", rl.thresholds_valid(0.65, 0.60) is True)
fail += _check("thresholds invalid: 0.30 below floor & < ask", rl.thresholds_valid(0.30, 0.60) is False)
fail += _check("thresholds invalid: auto <= ask inverts bands", rl.thresholds_valid(0.55, 0.60) is False)
fail += _check("thresholds invalid: above ceiling", rl.thresholds_valid(0.99, 0.60) is False)

print(f"--- router_loop helpers: {'OK' if fail == 0 else f'{fail} FAILED'}")
sys.exit(1 if fail else 0)
