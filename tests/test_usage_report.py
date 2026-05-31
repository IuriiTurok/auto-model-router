#!/usr/bin/env python3
"""Unit tests for tools/usage-report.py — KPI scoring over synthetic data.

Pure-Python, no network. Isolates all router writes/reads to temp dirs.
Run: python3 tests/test_usage_report.py
Exit 0 if all pass; 1 otherwise.
"""

import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

# Isolate cache + projects BEFORE importing the module (paths bind at import).
_TMP = tempfile.mkdtemp()
os.environ["CC_ROUTER_CACHE_DIR"] = os.path.join(_TMP, "cache")
os.environ["CC_ROUTER_PROJECTS_DIR"] = os.path.join(_TMP, "projects")
os.makedirs(os.environ["CC_ROUTER_CACHE_DIR"], exist_ok=True)
os.makedirs(os.environ["CC_ROUTER_PROJECTS_DIR"], exist_ok=True)

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_ROOT, "hooks"))
import pricing  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "usage_report", os.path.join(_ROOT, "tools", "usage-report.py")
)
ur = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ur)

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


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ── 1. Classifier (labeled cases) ───────────────────────────────────────────
CASES = [
    ("no, that is wrong, revert it", "correction"),
    ("perfect, thanks!", "approval"),
    ("no problem, take your time", "neutral"),
    ("can you also add a test", "neutral"),
    ("thanks but that broke the build", "correction"),
    ("actually do it the other way", "correction"),
    ("looks good, ship it", "approval"),
    ("that is not what I asked for", "correction"),
    ("no worries", "neutral"),
    ("hmm let me think", "neutral"),
]
for text, want in CASES:
    check(f"classify {text!r}->{want}", ur.classify_user_turn(text) == want)


# ── 2. Pricing math ─────────────────────────────────────────────────────────
# opus(1000,2000)=0.055 ; haiku(1000,2000)=0.011 ; saved=0.044
check("pricing opus cost", abs(pricing.cost("opus", 1000, 2000) - 0.055) < 1e-9)
check("pricing haiku cost", abs(pricing.cost("haiku", 1000, 2000) - 0.011) < 1e-9)
check(
    "pricing saving haiku vs opus",
    abs(pricing.counterfactual_saving("haiku", 1000, 2000) - 0.044) < 1e-9,
)
check(
    "pricing saving opus vs opus is 0",
    pricing.counterfactual_saving("opus", 9, 9) == 0.0,
)
check("model_family opus", pricing.model_family("claude-opus-4-8") == "opus")
check("model_family unknown -> None", pricing.model_family("gpt-4") is None)


# ── 3. End-to-end analyze() over a synthetic session ────────────────────────
ts = now_iso()
session = [
    {
        "type": "user",
        "timestamp": ts,
        "message": {"content": "Please refactor the auth module"},
    },
    {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "model": "claude-haiku-4-5-20251001",
            "usage": {"input_tokens": 1000, "output_tokens": 2000},
            "content": [
                {"type": "text", "text": "delegating"},
                {
                    "type": "tool_use",
                    "name": "Agent",
                    "input": {"subagent_type": "router-haiku", "description": "x"},
                },
            ],
        },
    },
    {
        "type": "user",
        "timestamp": ts,
        "message": {"content": "no, that's wrong, revert it"},
    },
    {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "model": "claude-opus-4-8",
            "usage": {"input_tokens": 500, "output_tokens": 1000},
            "content": [{"type": "text", "text": "done inline"}],
        },
    },
    {"type": "user", "timestamp": ts, "message": {"content": "perfect, thanks!"}},
]
write_jsonl(
    os.path.join(os.environ["CC_ROUTER_PROJECTS_DIR"], "proj", "s1.jsonl"), session
)

# a subagent transcript — its tokens should count toward totals but NOT as a session
write_jsonl(
    os.path.join(
        os.environ["CC_ROUTER_PROJECTS_DIR"], "proj", "subagents", "agent-x.jsonl"
    ),
    [
        {
            "type": "assistant",
            "timestamp": ts,
            "message": {
                "model": "claude-haiku-4-5-20251001",
                "usage": {"input_tokens": 300, "output_tokens": 400},
                "content": [{"type": "text", "text": "sub work"}],
            },
        }
    ],
)

data = ur.analyze(days=7, projects_dir=os.environ["CC_ROUTER_PROJECTS_DIR"])

check(
    "tokens: haiku in = 1300 (session 1000 + sub 300)",
    data["tokens"]["haiku"]["in"] == 1300,
)
check("tokens: haiku out = 2400", data["tokens"]["haiku"]["out"] == 2400)
check("tokens: opus in = 500", data["tokens"]["opus"]["in"] == 500)
check("sessions_scanned = 1 (subagent file excluded)", data["sessions_scanned"] == 1)
check("cohort router correction = 1", data["cohort"]["router"].get("correction") == 1)
check("cohort inline approval = 1", data["cohort"]["inline"].get("approval") == 1)
check(
    "initial request not counted as correction",
    sum(data["cohort"]["router"].values()) + sum(data["cohort"]["inline"].values())
    == 2,
)

rep = ur.build_report(data)
# saved: haiku(1300,2400) vs opus + sub already folded; opus slice saves 0.
expected_saved = round(pricing.counterfactual_saving("haiku", 1300, 2400), 2)
check(
    f"cost saved ~= {expected_saved}",
    abs(rep["cost"]["est_saved_vs_opus_usd"] - expected_saved) < 0.01,
)
check(
    "quality router correction rate = 100%",
    rep["quality"]["router"]["correction_rate_pct"] == 100.0,
)
check(
    "quality inline approval rate = 100%",
    rep["quality"]["inline"]["approval_rate_pct"] == 100.0,
)
check("time: 1 session measured", rep["time"]["sessions"] == 1)


# ── 4. analyze_audit() join + parallelism ───────────────────────────────────
audit_rows = [
    {
        "ts": now_iso(),
        "outcome": "injected",
        "decision": {"band": "auto", "model": "haiku", "decision_id": "r_a"},
    },
    {
        "ts": now_iso(),
        "outcome": "injected",
        "decision": {"band": "ask", "model": "opus", "decision_id": "r_b"},
    },
    {
        "ts": now_iso(),
        "outcome": "delegated",
        "model": "haiku",
        "group_id": "g1",
        "wall_ms": 1000,
    },
    {
        "ts": now_iso(),
        "outcome": "delegated",
        "model": "haiku",
        "group_id": "g1",
        "wall_ms": 3000,
    },
]
write_jsonl(ur.AUDIT_LOG, audit_rows)
audit = ur.analyze_audit(days=7)
check(
    "audit by_band auto=1 ask=1",
    audit["by_band"].get("auto") == 1 and audit["by_band"].get("ask") == 1,
)
check("audit parallel batch detected", audit["parallel"]["batches"] >= 1)
check(
    "audit wall saved = 1000 (sum 4000 - max 3000)",
    audit["parallel"]["wall_ms_saved"] == 1000,
)


print("---")
print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
