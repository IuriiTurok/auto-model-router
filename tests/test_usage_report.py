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
# Aliases price as the current 5.5 lineup: opus(1000,2000)=0.044 ; haiku=0.0011
USAGE_1K_2K = {"tokens_in": 1000, "tokens_out": 2000}
check(
    "pricing opus cost",
    abs(pricing.cost_usd("opus", USAGE_1K_2K) - 0.044) < 1e-9,
)
check(
    "pricing haiku cost",
    abs(pricing.cost_usd("haiku", USAGE_1K_2K) - 0.0011) < 1e-9,
)
check(
    "pricing saving haiku under an opus parent",
    abs(pricing.saving_vs_parent("haiku", "opus", USAGE_1K_2K) - 0.0429) < 1e-9,
)
check(
    "pricing saving opus under an opus parent is 0",
    pricing.saving_vs_parent("opus", "opus", USAGE_1K_2K) == 0.0,
)
check("model_family opus", pricing.model_family("claude-opus-4-8") == "opus")
check("model_family unknown -> None", pricing.model_family("gpt-4") is None)


# ── 3. End-to-end analyze() over a synthetic session ────────────────────────
ts = now_iso()
PROJECTS = os.environ["CC_ROUTER_PROJECTS_DIR"]
OPUS_55 = "claude-opus-5-5"
HAIKU_55 = "claude-haiku-5-5"


def assistant(msg_id, model, usage, content, stamp=ts):
    message = {"model": model, "usage": usage, "content": content}
    if msg_id:
        message["id"] = msg_id
    return {"type": "assistant", "timestamp": stamp, "message": message}


dispatch_block = {
    "type": "tool_use",
    "name": "Agent",
    "input": {"subagent_type": "router-haiku", "description": "x"},
}
session = [
    {
        "type": "user",
        "timestamp": ts,
        "message": {"content": "Please refactor the auth module"},
    },
    # One API response, written once per content block with the same id and
    # usage (output grows while streaming): must be counted once.
    assistant(
        "m1",
        OPUS_55,
        {"input_tokens": 1000, "output_tokens": 500},
        [{"type": "text", "text": "delegating"}],
    ),
    assistant(
        "m1",
        OPUS_55,
        {"input_tokens": 1000, "output_tokens": 2000},
        [dispatch_block],
    ),
    {
        "type": "user",
        "timestamp": ts,
        "message": {"content": "no, that's wrong, revert it"},
    },
    assistant(
        "m2",
        OPUS_55,
        {"input_tokens": 500, "output_tokens": 1000},
        [{"type": "text", "text": "done inline"}],
    ),
    {"type": "user", "timestamp": ts, "message": {"content": "perfect, thanks!"}},
]
write_jsonl(os.path.join(PROJECTS, "proj", "s1.jsonl"), session)

# A subagent transcript of session s1 — its tokens count toward totals (and are
# priced against s1's parent model) but it is NOT a session.
write_jsonl(
    os.path.join(PROJECTS, "proj", "s1", "subagents", "agent-x.jsonl"),
    [
        assistant(
            "w1",
            HAIKU_55,
            {
                "input_tokens": 300_000,
                "output_tokens": 400_000,
                "cache_read_input_tokens": 20_000_000,
            },
            [{"type": "text", "text": "sub work"}],
        )
    ],
)

data = ur.analyze(days=7, projects_dir=PROJECTS)

check(
    "tokens: opus in = 1500 (m1 counted once + m2)",
    data["tokens"]["opus"]["in"] == 1500,
)
check(
    "tokens: opus out = 3000 (m1's final 2000, not 2500, + m2's 1000)",
    data["tokens"]["opus"]["out"] == 3000,
)
check("tokens: haiku in = 300000 (subagent)", data["tokens"]["haiku"]["in"] == 300_000)
check("tokens: haiku out = 400000", data["tokens"]["haiku"]["out"] == 400_000)
check("sessions_scanned = 1 (subagent file excluded)", data["sessions_scanned"] == 1)
check("cohort router correction = 1", data["cohort"]["router"].get("correction") == 1)
check("cohort inline approval = 1", data["cohort"]["inline"].get("approval") == 1)
check(
    "initial request not counted as correction",
    sum(data["cohort"]["router"].values()) + sum(data["cohort"]["inline"].values())
    == 2,
)

rep = ur.build_report(data)
# The subagent ran on haiku under s1's opus parent: saved = what its usage
# (cache included) would have cost on opus 5.5 minus what it cost on haiku 5.5.
# opus: 300k*4 + 400k*20 + 20M*0.20 = 1.2+8+4 = 13.2 ; haiku: 0.03+0.2+0.2 = 0.43
check(
    "cost saved vs the opus parent = 12.77",
    rep["cost"]["est_saved_vs_parent_usd"] == 12.77
    and rep["cost"]["est_offload_saved_usd"] == 12.77
    and rep["cost"]["worker_premium_usd"] == 0.0,
)
check(
    "no all-Opus baseline key survives in the cost KPI",
    "est_saved_vs_opus_usd" not in rep["cost"]
    and "parent_model_premium_usd" not in rep["cost"],
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


# ── 3b. Savings are priced against each session's own parent ────────────────
SONNET_55 = "claude-sonnet-5-5"
FABLE_51 = "claude-fable-5-1"
WORKER_USAGE = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
parent_usage = {"input_tokens": 10, "output_tokens": 10}
with tempfile.TemporaryDirectory() as pdir:
    stamp = now_iso()
    # session on an opus parent, with a haiku worker and a (dearer) fable worker
    write_jsonl(
        os.path.join(pdir, "p", "s_opus.jsonl"),
        [assistant("o1", OPUS_55, parent_usage, [], stamp)],
    )
    write_jsonl(
        os.path.join(pdir, "p", "s_opus", "subagents", "agent-1.jsonl"),
        [assistant("h1", HAIKU_55, WORKER_USAGE, [], stamp)],
    )
    write_jsonl(
        os.path.join(pdir, "p", "s_opus", "subagents", "agent-3.jsonl"),
        [assistant("f1", FABLE_51, WORKER_USAGE, [], stamp)],
    )
    # session on a SONNET parent with the same haiku worker usage
    write_jsonl(
        os.path.join(pdir, "p", "s_sonnet.jsonl"),
        [assistant("s1", SONNET_55, parent_usage, [], stamp)],
    )
    write_jsonl(
        os.path.join(pdir, "p", "s_sonnet", "subagents", "agent-2.jsonl"),
        [assistant("h2", HAIKU_55, WORKER_USAGE, [], stamp)],
    )
    cost = ur.build_report(ur.analyze(days=7, projects_dir=pdir))["cost"]
    haiku_vs_opus = pricing.saving_vs_parent("haiku", "opus", WORKER_USAGE)
    haiku_vs_sonnet = pricing.saving_vs_parent("haiku", "sonnet", WORKER_USAGE)
    fable_vs_opus = pricing.saving_vs_parent("fable", "opus", WORKER_USAGE)
    check(
        "per-session parent: offload = haiku-vs-opus + haiku-vs-sonnet",
        abs(cost["est_offload_saved_usd"] - round(haiku_vs_opus + haiku_vs_sonnet, 2))
        < 0.006,
    )
    check(
        "per-session parent: a fable worker under opus is a negative premium",
        fable_vs_opus < 0
        and abs(cost["worker_premium_usd"] - round(fable_vs_opus, 2)) < 0.006,
    )
    check(
        "per-session parent: net = offload + premium",
        abs(
            cost["est_saved_vs_parent_usd"]
            - (cost["est_offload_saved_usd"] + cost["worker_premium_usd"])
        )
        < 0.006,
    )

with tempfile.TemporaryDirectory() as pdir:
    # A session's own (top-level) tokens never count as savings, even when the
    # session runs on a cheap model.
    write_jsonl(
        os.path.join(pdir, "p", "only_haiku.jsonl"),
        [assistant("x1", HAIKU_55, WORKER_USAGE, [], now_iso())],
    )
    cost = ur.build_report(ur.analyze(days=7, projects_dir=pdir))["cost"]
    check(
        "a cheap parent's own tokens save nothing",
        cost["est_saved_vs_parent_usd"] == 0.0 and cost["tokens_out"] == 1_000_000,
    )


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
    ur.assistant_router_dispatch(
        _agent_dispatch_rec("auto-model-router:router-sonnet")
    ),
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


# ── 8. Rotated audit files are read too ─────────────────────────────────────
before = ur.analyze_audit(days=7)
rotated_path = f"{ur.AUDIT_LOG}.20260101T000000Z"
write_jsonl(
    rotated_path,
    [
        {
            "ts": now_iso(),
            "outcome": "injected",
            "decision": {"band": "auto", "model": "haiku", "decision_id": "r_rot1"},
        },
        {
            "ts": now_iso(),
            "outcome": "silent",
            "decision": {"band": "none", "model": "sonnet", "decision_id": "r_rot2"},
        },
        {"ts": now_iso(), "outcome": "delegated", "model": "haiku", "wall_ms": 10},
    ],
)
# a stray file that merely shares the prefix is not a rotation
with open(f"{ur.AUDIT_LOG}.bak", "w") as f:
    f.write(json.dumps({"ts": now_iso(), "outcome": "silent", "decision": {}}) + "\n")
after = ur.analyze_audit(days=7)
check(
    "rotated audit file: its 2 decisions are counted",
    after["decisions"] == before["decisions"] + 2,
)
check(
    "rotated audit file: its dispatch row is counted",
    after["reconciliation"]["dispatched_total"]
    == before["reconciliation"]["dispatched_total"] + 1,
)
check(
    "a non-rotation file sharing the prefix (.bak) is ignored",
    after["decisions"] == before["decisions"] + 2,
)
check(
    "audit_paths: rotated first (oldest first), live log last",
    ur.audit_paths(ur.AUDIT_LOG) == [rotated_path, ur.AUDIT_LOG],
)
os.remove(rotated_path)
os.remove(f"{ur.AUDIT_LOG}.bak")


# ── 9. Background workers: usage arrives on the SubagentStop row ────────────
before_audit = ur.analyze_audit(days=7)
before = before_audit["realized"]
BG_USAGE = {"tokens_in": 1000, "tokens_out": 500, "cache_read": 10000, "cache_write": 0}
FG_USAGE = {"tokens_in": 200, "tokens_out": 100, "cache_read": 0, "cache_write": 0}
with open(ur.AUDIT_LOG, "a") as f:
    for row in [
        # background worker: the launch row has no usage, the stop row does
        {
            "ts": now_iso(),
            "outcome": "delegated",
            "kind": "router",
            "agent_id": "bg1",
            "model": "sonnet",
        },
        {
            "ts": now_iso(),
            "outcome": "worker_usage",
            "kind": "router",
            "agent_id": "bg1",
            "model_actual": "claude-sonnet-5-5",
            "usage": BG_USAGE,
        },
        # foreground worker: BOTH rows carry its usage; count it once
        {
            "ts": now_iso(),
            "outcome": "worker_usage",
            "kind": "router",
            "agent_id": "fg1",
            "model_actual": "claude-opus-5-5",
            "usage": FG_USAGE,
        },
        {
            "ts": now_iso(),
            "outcome": "delegated",
            "kind": "router",
            "agent_id": "fg1",
            "model": "opus",
            "model_actual": "claude-opus-5-5",
            "usage": FG_USAGE,
        },
    ]:
        f.write(json.dumps(row) + "\n")
after_audit = ur.analyze_audit(days=7)
after = after_audit["realized"]
exp_added = pricing.cost_usd("claude-sonnet-5-5", BG_USAGE) + pricing.cost_usd(
    "claude-opus-5-5", FG_USAGE
)
check(
    "worker_usage: background usage counted, foreground counted once",
    abs((after["total_cost_usd"] - before["total_cost_usd"]) - exp_added) < 1e-3,
)
check(
    "worker_usage: lands in the router/sonnet bucket with all four token kinds",
    after["by_kind_model"]["router/sonnet"]["tokens"]
    - before["by_kind_model"].get("router/sonnet", {"tokens": 0})["tokens"]
    == sum(BG_USAGE.values()),
)
check(
    "worker_usage rows are not dispatches (only the 2 delegated rows count)",
    after_audit["reconciliation"]["dispatched_total"]
    == before_audit["reconciliation"]["dispatched_total"] + 2,
)


# ── 10. A subagent whose session transcript is out of the window ────────────
with tempfile.TemporaryDirectory() as pdir:
    write_jsonl(
        os.path.join(pdir, "p", "top.jsonl"),
        [assistant("t1", OPUS_55, {"input_tokens": 50, "output_tokens": 50}, [])],
    )
    write_jsonl(
        os.path.join(pdir, "p", "gone", "subagents", "agent-9.jsonl"),
        [assistant("g1", HAIKU_55, WORKER_USAGE, [])],
    )
    check(
        "owner_transcript: subagent file -> <session>.jsonl, top-level -> itself",
        ur.owner_transcript(
            os.path.join(pdir, "p", "gone", "subagents", "agent-9.jsonl")
        )
        == os.path.join(pdir, "p", "gone.jsonl")
        and ur.owner_transcript(os.path.join(pdir, "p", "top.jsonl"))
        == os.path.join(pdir, "p", "top.jsonl"),
    )
    cost = ur.build_report(ur.analyze(days=7, projects_dir=pdir))["cost"]
    check(
        "orphan subagent is priced against the window's dominant parent (opus)",
        abs(
            cost["est_offload_saved_usd"]
            - round(pricing.saving_vs_parent("haiku", "opus", WORKER_USAGE), 2)
        )
        < 0.006,
    )


# ── 11. Dispatch follow-through counts only auto-band decisions ─────────────
# Band-none rows that happen to name a cheaper model than the parent (the
# haiku fallback, project_default_inline) can never be injected, so they are
# not follow-through candidates.
before_audit = ur.analyze_audit(days=7)
before_ft = before_audit["realized"]["dispatch_follow_through_pct"]
with open(ur.AUDIT_LOG, "a") as f:
    for row in [
        {
            "ts": now_iso(),
            "outcome": "silent",
            "decision": {
                "band": "none",
                "tier": "default",
                "model": "haiku",
                "parent": "opus",
                "decision_id": "r_none_haiku",
            },
        },
        {
            "ts": now_iso(),
            "outcome": "silent",
            "outcome_hint": "project_default_inline",
            "decision": {
                "band": "none",
                "tier": "project_default",
                "model": "sonnet",
                "parent": "opus",
                "decision_id": "r_none_default",
            },
        },
    ]:
        f.write(json.dumps(row) + "\n")
after_audit = ur.analyze_audit(days=7)
check(
    "follow-through baseline is a real number (r_c followed of r_c + r_d)",
    before_ft == 50.0,
)
check(
    "band-none cheaper-than-parent rows are counted as decisions...",
    after_audit["decisions"] == before_audit["decisions"] + 2,
)
check(
    "...but are not follow-through candidates (still 50.0, not 25.0)",
    after_audit["realized"]["dispatch_follow_through_pct"] == before_ft,
)


# ── 12. Harness events are not human turns ──────────────────────────────────
# A background worker's completion notice lands as the next "user" record
# after its dispatch. Classified, its "failed" / "instead of" scores as a user
# correction in the router cohort, and it eats the router flag so the user's
# real reply is filed as inline.
def user(text, stamp=ts):
    return {"type": "user", "timestamp": stamp, "message": {"content": text}}


router_dispatch = {
    "type": "tool_use",
    "name": "Agent",
    "input": {"subagent_type": "auto-model-router:router-sonnet", "description": "x"},
}
with tempfile.TemporaryDirectory() as pdir:
    write_jsonl(
        os.path.join(pdir, "p", "s_harness.jsonl"),
        [
            user("Build the export feature"),
            assistant("h1", OPUS_55, {"output_tokens": 10}, [router_dispatch]),
            user(
                "<task-notification>\n<status>completed</status>\n<summary>Worker "
                "done: one test failed, used csv instead of xlsx</summary>\n"
                "</task-notification>"
            ),
            assistant(
                "h2", OPUS_55, {"output_tokens": 10}, [{"type": "text", "text": "done"}]
            ),
            user("perfect, thanks"),
            user("<ci-monitor-event>CI failed on the PR</ci-monitor-event>"),
        ],
    )
    write_jsonl(
        os.path.join(pdir, "p", "s_scheduled.jsonl"),
        [
            user('<scheduled-task name="nightly">run the digest</scheduled-task>'),
            assistant(
                "h3", OPUS_55, {"output_tokens": 10}, [{"type": "text", "text": "ok"}]
            ),
            user("no, that's wrong, revert it"),
        ],
    )
    cohort = ur.analyze(days=7, projects_dir=pdir)["cohort"]
    check(
        "harness: the task-notification is skipped and keeps the router flag, "
        "so the user's reply is the router turn",
        cohort["router"] == {"approval": 1},
    )
    check(
        "harness: the ci-monitor-event is not an inline turn; a scheduled-task "
        "opener stands in for the initial request",
        cohort["inline"] == {"correction": 1},
    )


# ── 13. Worker cost is attributed by agent type ─────────────────────────────
# Offload from a native agent that happens to run on Sonnet is not the
# router's win; the split comes from the subagent's <agent>.meta.json.
def write_meta(path, agent_type):
    with open(path, "w") as f:
        json.dump({"agentType": agent_type, "description": "x"}, f)


with tempfile.TemporaryDirectory() as pdir:
    sub = os.path.join(pdir, "p", "s_kind", "subagents")
    write_jsonl(
        os.path.join(pdir, "p", "s_kind.jsonl"),
        [assistant("k0", OPUS_55, {"input_tokens": 10, "output_tokens": 10}, [])],
    )
    # router worker on sonnet: opus $24 - sonnet $12 = $12 saved
    write_jsonl(
        os.path.join(sub, "agent-r.jsonl"),
        [
            assistant(
                "k1",
                SONNET_55,
                {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
                [],
            )
        ],
    )
    write_meta(
        os.path.join(sub, "agent-r.meta.json"), "auto-model-router:router-sonnet"
    )
    # native agent on sonnet: opus $10 - sonnet $5 = $5 saved
    write_jsonl(
        os.path.join(sub, "agent-g.jsonl"),
        [assistant("k2", SONNET_55, {"output_tokens": 500_000}, [])],
    )
    write_meta(os.path.join(sub, "agent-g.meta.json"), "general-purpose")
    # no meta file -> other: opus $2.00 - haiku $0.05 = $1.95 saved
    write_jsonl(
        os.path.join(sub, "agent-n.jsonl"),
        [assistant("k3", HAIKU_55, {"output_tokens": 100_000}, [])],
    )
    cost = ur.build_report(ur.analyze(days=7, projects_dir=pdir))["cost"]
    check(
        "agent type: router offload = 12.0, other offload = 6.95, total = 18.95",
        cost.get("router_offload_saved_usd") == 12.0
        and cost.get("other_offload_saved_usd") == 6.95
        and cost["est_offload_saved_usd"] == 18.95,
    )
    spend = cost.get("worker_spend_by_kind_model", {})
    check(
        "agent type: worker spend by kind x family from the transcripts",
        spend.get("router/sonnet") == {"tokens": 2_000_000, "cost_usd": 12.0}
        and spend.get("other/sonnet") == {"tokens": 500_000, "cost_usd": 5.0}
        and spend.get("other/haiku") == {"tokens": 100_000, "cost_usd": 0.05},
    )

# Audit coverage: router workers seen in transcripts vs those with a usage row
# (a SubagentStop worker_usage row, or a foreground dispatch row with usage).
with tempfile.TemporaryDirectory() as pdir:
    sub = os.path.join(pdir, "p", "s_cov", "subagents")
    write_jsonl(
        os.path.join(pdir, "p", "s_cov.jsonl"),
        [assistant("c0", OPUS_55, {"output_tokens": 10}, [])],
    )
    for agent in ("cov_bg", "cov_fg", "cov_missing"):
        write_jsonl(
            os.path.join(sub, f"agent-{agent}.jsonl"),
            [assistant(f"c_{agent}", SONNET_55, {"output_tokens": 1000}, [])],
        )
        write_meta(
            os.path.join(sub, f"agent-{agent}.meta.json"),
            "auto-model-router:router-sonnet",
        )
    with open(ur.AUDIT_LOG, "a") as f:
        for row in [
            {
                "ts": now_iso(),
                "outcome": "worker_usage",
                "kind": "router",
                "agent_id": "cov_bg",
                "model_actual": SONNET_55,
                "usage": FG_USAGE,
            },
            {
                "ts": now_iso(),
                "outcome": "delegated",
                "kind": "router",
                "agent_id": "cov_fg",
                "model": "sonnet",
                "model_actual": SONNET_55,
                "usage": FG_USAGE,
            },
        ]:
            f.write(json.dumps(row) + "\n")
    realized = ur.build_report(ur.analyze(days=7, projects_dir=pdir))["realized"]
    check(
        "coverage: 3 router workers in transcripts, 2 with a usage row",
        realized.get("router_workers_in_transcripts") == 3
        and realized.get("router_workers_with_usage_row") == 2,
    )


# ── 14. Parallelism from overlapping worker activity ────────────────────────
# Background workers return at launch, so dispatch wall_ms (~1 s) says nothing
# about concurrency. Overlap of worker activity segments does; a segment runs
# from a prompt into the worker to its last record before the next prompt, so
# a worker idling between SendMessage continuations is not "running".
T0 = datetime.now(timezone.utc) - timedelta(days=1)


def at(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat()


def worker(path, spans):
    """spans: [(prompt_minute, last_minute), ...] — one segment per prompt."""
    recs = []
    for i, (start, end) in enumerate(spans):
        recs.append(user("brief" if i == 0 else "continue", at(start)))
        recs.append(
            assistant(
                f"{os.path.basename(path)}-{i}",
                SONNET_55,
                {"output_tokens": 1},
                [],
                at(end),
            )
        )
    write_jsonl(path, recs)


with tempfile.TemporaryDirectory() as pdir:
    sub = os.path.join(pdir, "p", "s_par", "subagents")
    write_jsonl(
        os.path.join(pdir, "p", "s_par.jsonl"),
        [user("go", at(0)), assistant("p0", OPUS_55, {"output_tokens": 1}, [], at(90))],
    )
    worker(os.path.join(sub, "agent-a.jsonl"), [(0, 10)])
    worker(os.path.join(sub, "agent-b.jsonl"), [(5, 20)])  # overlaps a by 5 min
    worker(os.path.join(sub, "agent-c.jsonl"), [(30, 31), (60, 61)])  # idle 31-60
    worker(os.path.join(sub, "agent-d.jsonl"), [(40, 45)])  # inside c's idle gap
    t = ur.build_report(ur.analyze(days=7, projects_dir=pdir))["time"]
    check(
        "overlap: 5.0 min saved (a/b); c's idle gap does not overlap d",
        t.get("worker_overlap_min_saved") == 5.0,
    )
    check(
        "overlap: peak 2 concurrent workers in 1 session",
        t.get("max_concurrent_workers") == 2
        and t.get("sessions_with_parallel_workers") == 1,
    )


print("---")
print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
