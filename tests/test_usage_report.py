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
from datetime import datetime, timedelta, timezone

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
    # skip rows: one canonical joining to the auto decision, one drifted
    # spelling on the ask decision (must normalize, must NOT count as auto).
    {
        "ts": now_iso(),
        "outcome": "continuity_inline",
        "model": "haiku",
        "decision_id": "r_a",
    },
    {
        "ts": now_iso(),
        "outcome": "inline_override",
        "model": "opus",
        "decision_id": "r_b",
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

# reconciliation: auto-band dispatch accounting + drifted-vocab normalization
rec = audit["reconciliation"]
check("recon auto_decisions = 1", rec["auto_decisions"] == 1)
check("recon dispatched_total = 2", rec["dispatched_total"] == 2)
check("recon auto_skip_logged = 1 (continuity joins r_a)", rec["auto_skip_logged"] == 1)
check("recon auto_unlogged = 0", rec["auto_unlogged"] == 0)
check(
    "recon drifted spelling normalized to inline_other",
    rec["skip_outcomes"].get("inline_other") == 1,
)
check(
    "recon ask-band drifted skip not counted as auto",
    rec["skip_outcomes"].get("continuity_inline") == 1 and rec["auto_skip_logged"] == 1,
)


# ── 5. assistant_router_dispatch: bare + namespaced subagent names ─────────
def _agent_dispatch_rec(subagent_type):
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Agent",
                    "input": {"subagent_type": subagent_type},
                }
            ]
        },
    }


check(
    "assistant_router_dispatch matches bare router-*",
    ur.assistant_router_dispatch(_agent_dispatch_rec("router-opus")),
)
check(
    "assistant_router_dispatch matches namespaced auto-model-router:router-*",
    ur.assistant_router_dispatch(_agent_dispatch_rec("auto-model-router:router-sonnet")),
)
check(
    "assistant_router_dispatch rejects a non-router subagent_type",
    not ur.assistant_router_dispatch(_agent_dispatch_rec("general-purpose")),
)


# ── 6. Realized rollup + CANONICAL_SKIPS outcome_hint fix ───────────────────
# Appended (not overwritten) onto the existing audit_rows fixture from section 4,
# so the earlier reconciliation numbers above stay valid and these assertions
# only need to reason about the additive effect of the new rows.
extra_audit_rows = [
    # auto-band decision, routed strictly cheaper than parent (haiku < opus),
    # and it DOES get a real delegated dispatch — follow-through counts it.
    {
        "ts": now_iso(),
        "outcome": "injected",
        "decision": {
            "band": "auto",
            "model": "haiku",
            "parent": "opus",
            "decision_id": "r_c",
        },
    },
    {
        "ts": now_iso(),
        "outcome": "delegated",
        "kind": "router",
        "model_actual": "claude-haiku-4-5-20251001",
        "decision_id": "r_c",
        "usage": {"tokens_in": 1000, "tokens_out": 500},
    },
    # auto-band decision, also routed cheaper than parent (sonnet < opus), but
    # never actually dispatched — drags follow-through below 100%.
    {
        "ts": now_iso(),
        "outcome": "silent",
        "decision": {
            "band": "auto",
            "model": "sonnet",
            "parent": "opus",
            "decision_id": "r_d",
        },
    },
    # a native (non-router-*) subagent dispatch with its own realized usage
    {
        "ts": now_iso(),
        "outcome": "native_dispatch",
        "kind": "native",
        "model_actual": "claude-sonnet-4-6-20260115",
        "usage": {"tokens_in": 2000, "tokens_out": 1000},
    },
    # a router dispatch that self-reported needing escalation
    {
        "ts": now_iso(),
        "outcome": "delegated",
        "kind": "router",
        "model_actual": "claude-opus-4-8",
        "usage": {"tokens_in": 100, "tokens_out": 100},
        "escalation": "Stopped: too complex",
    },
    # reconcile-outcomes.py's deterministic backstop row — different usage-dict
    # shape (input/output, not tokens_in/tokens_out) plus context_tokens.
    {
        "ts": now_iso(),
        "outcome": "auto_inline_unattributed",
        "decision_id": "r_f",
        "usage": {
            "model": "claude-haiku-4-5-20251001",
            "input": 400,
            "output": 200,
            "cache_read": 0,
            "cache_write": 0,
            "context_tokens": 12000,
        },
    },
    # auto-band decision that self-resolves via outcome_hint on the SAME row
    # (auto-router.py's downhill-only "silent" path) — must not show up in
    # auto_unlogged just because there's no separately joined skip row.
    {
        "ts": now_iso(),
        "outcome": "silent",
        "decision": {"band": "auto", "model": "opus", "decision_id": "r_g"},
        "outcome_hint": "same_or_higher_inline",
    },
]
with open(ur.AUDIT_LOG, "a") as f:
    for row in extra_audit_rows:
        f.write(json.dumps(row) + "\n")

audit2 = ur.analyze_audit(days=7)
rec2 = audit2["reconciliation"]
realized = audit2["realized"]

check(
    "recon auto_decisions grows to 4 (r_a, r_c, r_d, r_g)",
    rec2["auto_decisions"] == 4,
)
check(
    "recon auto_skip_logged grows to 2 (r_a via continuity row, r_g via hint)",
    rec2["auto_skip_logged"] == 2,
)
check("recon auto_unlogged = 2 (r_c, r_d have no skip row)", rec2["auto_unlogged"] == 2)
check(
    "same_or_higher_inline resolved via outcome_hint on the silent row itself",
    rec2["skip_outcomes"].get("same_or_higher_inline") == 1,
)
check(
    "auto_inline_unattributed counted as a canonical skip outcome",
    rec2["skip_outcomes"].get("auto_inline_unattributed") == 1,
)

exp_router_haiku_cost = pricing.cost_usd(
    "claude-haiku-4-5-20251001", {"tokens_in": 1000, "tokens_out": 500}
)
exp_native_sonnet_cost = pricing.cost_usd(
    "claude-sonnet-4-6-20260115", {"tokens_in": 2000, "tokens_out": 1000}
)
exp_router_opus_cost = pricing.cost_usd(
    "claude-opus-4-8", {"tokens_in": 100, "tokens_out": 100}
)
exp_inline_haiku_cost = pricing.cost_usd(
    "claude-haiku-4-5-20251001", {"tokens_in": 400, "tokens_out": 200}
)
exp_total_cost = (
    exp_router_haiku_cost
    + exp_native_sonnet_cost
    + exp_router_opus_cost
    + exp_inline_haiku_cost
)
check(
    "realized total_cost_usd matches hand-computed sum",
    abs(realized["total_cost_usd"] - round(exp_total_cost, 4)) < 1e-6,
)
check(
    "realized by_kind_model has router/haiku bucket",
    abs(
        realized["by_kind_model"]["router/haiku"]["cost_usd"]
        - round(exp_router_haiku_cost, 4)
    )
    < 1e-6,
)
check(
    "realized by_kind_model has native/sonnet bucket",
    realized["by_kind_model"]["native/sonnet"]["tokens"] == 3000,
)
check(
    "realized by_kind_model has inline/haiku bucket (normalized input/output usage)",
    realized["by_kind_model"]["inline/haiku"]["tokens"] == 600,
)
exp_opus_pct = round(100 * exp_router_opus_cost / exp_total_cost, 1)
check(
    f"realized opus_class_cost_pct ~= {exp_opus_pct}",
    abs(realized["opus_class_cost_pct"] - exp_opus_pct) < 0.1,
)
check(
    "realized dispatch_follow_through_pct = 50.0 (r_c followed, r_d did not)",
    realized["dispatch_follow_through_pct"] == 50.0,
)
check(
    "realized escalation_rate_pct = 25.0 (1 escalated / 4 delegated total)",
    realized["escalation_rate_pct"] == 25.0,
)
check(
    "realized median_context_tokens_per_turn = 12000.0",
    realized["median_context_tokens_per_turn"] == 12000.0,
)


# ── 7. analyze_audit(end=...) — prior-window support for --compare-days ────
far_past_end = datetime.now(timezone.utc) - timedelta(days=100)
audit_empty = ur.analyze_audit(days=1, end=far_past_end)
check(
    "analyze_audit(end=...) excludes rows outside the shifted window",
    audit_empty["decisions"] == 0,
)


print("---")
print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
