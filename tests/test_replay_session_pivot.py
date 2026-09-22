#!/usr/bin/env python3
"""Unit tests for tools/replay_session_pivot.py — the session-pivot offline gate."""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"),
)

import replay_session_pivot as rsp

PRICING = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "pricing.py"
)
pricing = rsp._load(PRICING, "pricing")


def _check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    return 0 if cond else 1


fail = 0

# --- classify_opus_turn ---
fail += _check(
    "classify: low-effort deep-verb reason -> benign",
    rsp.classify_opus_turn({"effort": "low", "reason": "deep-verb intent (plan/design)"}) == "benign",
)
fail += _check(
    "classify: high effort -> real",
    rsp.classify_opus_turn({"effort": "high", "reason": "deep-verb intent"}) == "real",
)
fail += _check(
    "classify: migration rule reason -> real",
    rsp.classify_opus_turn({"effort": "medium", "reason": "DB/schema migrations are high-blast-radius"}) == "real",
)
fail += _check(
    "classify: unknown reason -> real (conservative)",
    rsp.classify_opus_turn({"effort": "medium", "reason": "something else"}) == "real",
)

# --- norm_usage maps reconcile keys ---
nu = rsp.norm_usage({"input": 10, "output": 20, "cache_read": 5, "cache_write": 7})
fail += _check(
    "norm_usage maps input/output -> *_tokens",
    nu["input_tokens"] == 10 and nu["output_tokens"] == 20
    and nu["cache_read"] == 5 and nu["cache_write"] == 7,
)

# --- analyze() on a synthetic audit log ---
NOW = datetime.now(timezone.utc).isoformat()
PROJ = "/Users/x/Code/demo/.claude/router.json"
USAGE = {"model": "claude-opus-4-8", "input": 100, "output": 100,
         "cache_read": 100000, "cache_write": 100000}


def dec_row(sid, model, effort="medium", reason="x", proj=PROJ):
    return {"ts": NOW, "session_id": sid,
            "decision": {"model": model, "effort": effort, "reason": reason,
                         "project_config": proj}}


def usage_row(sid):
    return {"ts": NOW, "session_id": sid, "outcome": "auto_inline_unattributed",
            "usage": dict(USAGE)}


rows = [
    # S1 cheap, no opus turn -> regret_none
    dec_row("s1", "sonnet"), dec_row("s1", "sonnet"), usage_row("s1"),
    # S2 cheap, one BENIGN opus turn (low-effort deep-verb)
    dec_row("s2", "sonnet"),
    dec_row("s2", "opus", effort="low", reason="deep-verb intent (plan/design)"),
    usage_row("s2"),
    # S3 cheap, one REAL opus turn (high effort)
    dec_row("s3", "sonnet"),
    dec_row("s3", "opus", effort="high", reason="effort_score=4 (heavy)"),
    usage_row("s3"),
    # S4 opus-majority -> excluded from cheap_sessions
    dec_row("s4", "opus", effort="high"), dec_row("s4", "opus", effort="high"),
    usage_row("s4"),
]

with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "audit.jsonl")
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    res = rsp.analyze(path, days=30, candidate="sonnet", pricing=pricing)
    demo = res["projects"]["demo"]
    fail += _check("analyze: 4 sessions, 3 cheap",
                   demo["sessions"] == 4 and demo["cheap_sessions"] == 3)
    fail += _check("analyze: regret split none/benign/real == 1/1/1",
                   demo["regret_none"] == 1 and demo["regret_benign"] == 1
                   and demo["regret_real"] == 1)
    fail += _check("analyze: regret_real_rate == 33.3%",
                   demo["regret_real_rate_pct"] == 33.3)
    fail += _check("analyze: sonnet counterfactual saves money (save>0)",
                   demo["save_usd_per_wk"] > 0)

    # Identity: candidate == the realized model family -> cf == realized.
    res_id = rsp.analyze(path, days=30, candidate="opus", pricing=pricing)
    d_id = res_id["projects"]["demo"]
    fail += _check("identity: candidate=opus -> cf == realized (save 0)",
                   abs(d_id["save_usd_per_wk"]) < 1e-9)

print(f"--- replay_session_pivot: {'OK' if fail == 0 else f'{fail} FAILED'}")
sys.exit(1 if fail else 0)
