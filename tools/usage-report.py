#!/usr/bin/env python3
"""Router usage report — scores the three north-star GOALS over a window.

Reads the router audit log (~/.claude/cache/router/audit.jsonl) and Claude Code
session transcripts (~/.claude/projects/**), and reports:

  - descriptive: models used, % of tokens by model, band, share
  - GOAL 1 cost efficiency:  estimated $ saved vs all-Opus
  - GOAL 2 quality:          user-correction rate, router vs inline cohort
  - GOAL 3 time-to-results:  session duration, time-to-first-approval, parallelism

Deterministic and read-only. Correction detection is heuristic by default;
`--llm-judge` adds a Haiku pass over ambiguous user turns (needs ANTHROPIC_API_KEY,
degrades gracefully to heuristic-only without one).

Usage:
    python3 tools/usage-report.py [--days N] [--since yesterday|today]
                                  [--json] [--llm-judge] [--write]

See GOALS.md for the metric definitions.
"""

import argparse
import glob
import hashlib
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))
from pricing import PRICES, cost_usd, counterfactual_saving, model_family  # noqa: E402

CACHE_DIR = os.path.expanduser(
    os.environ.get("CC_ROUTER_CACHE_DIR", "~/.claude/cache/router")
)
AUDIT_LOG = os.path.join(CACHE_DIR, "audit.jsonl")
REPORTS_DIR = os.path.join(CACHE_DIR, "reports")
PROJECTS_DIR = os.path.expanduser(
    os.environ.get("CC_ROUTER_PROJECTS_DIR", "~/.claude/projects")
)

# ── User-turn classification ────────────────────────────────────────────────
# Correction = user pushing back on what the assistant just did. Precedence:
# a correction match wins even if approval words are also present ("thanks, but
# that's wrong"). Traps like "no problem" are excluded before the bare-"no" rule.
_CORRECTION = re.compile(
    r"\b(that'?s (?:not|wrong|incorrect)|that is (?:not|wrong|incorrect)|"
    r"not what i (?:asked|wanted|meant)|don'?t do that|you shouldn'?t|"
    r"should ?n'?t have|why did you|that'?s bad|revert|undo|roll ?back|"
    r"put it back|i didn'?t ask|i said|i meant|instead of|rather than|"
    r"that'?s the wrong|the other way|do it differently|"
    r"broke|broken|does ?n'?t work|did ?n'?t work|not working|failed|stop)\b",
    re.I,
)
_NO_TRAP = re.compile(
    r"\bno (?:problem|worries|need|rush|thanks|thank you|biggie)\b", re.I
)
_LEADING_NO = re.compile(r"^\s*(?:no|nope|nah)\b[,. ]", re.I)
_APPROVAL = re.compile(
    r"\b(perfect|great|excellent|awesome|lgtm|looks good|ship it|exactly|"
    r"that works|works (?:great|now|perfectly)|love it|nicely done|"
    r"well done|thank you|thanks)\b",
    re.I,
)


def classify_user_turn(text: str) -> str:
    """-> 'correction' | 'approval' | 'neutral'."""
    if not text:
        return "neutral"
    t = text.strip()
    is_correction = bool(_CORRECTION.search(t))
    if not is_correction and _LEADING_NO.search(t) and not _NO_TRAP.search(t):
        is_correction = True
    if is_correction:
        return "correction"
    if _APPROVAL.search(t):
        return "approval"
    return "neutral"


def ambiguous_turn(text: str) -> bool:
    """A turn worth sending to the LLM judge: short negations / mixed signals
    the regex is least sure about."""
    t = (text or "").strip().lower()
    if _NO_TRAP.search(t):
        return False
    return bool(_LEADING_NO.search(t)) or ("thanks" in t and "but" in t)


# ── Transcript helpers ──────────────────────────────────────────────────────
def parse_ts(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def block_text(content) -> str:
    """Flatten a message.content (str or list of blocks) to plain text. Only
    real text blocks — ignores tool_use / tool_result / thinking."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(b.get("text", ""))
            elif isinstance(b, str):
                out.append(b)
        return "\n".join(out)
    return ""


def is_human_turn(rec: dict) -> bool:
    """A genuine user message (not a tool_result envelope, not meta)."""
    if rec.get("type") != "user":
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip() != ""
    if isinstance(content, list):
        has_text = any(isinstance(b, dict) and b.get("type") == "text" for b in content)
        has_tool_result = any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
        return has_text and not has_tool_result
    return False


# Bare `router-<model>` or namespaced `auto-model-router:router-<model>` (the
# plugin-qualified form the Agent tool sees when dispatched by name) — mirrors
# post-agent-audit.py's ROUTER_TYPE_RE.
_ROUTER_SUBAGENT_RE = re.compile(r"^(?:auto-model-router:)?router-")


def assistant_router_dispatch(rec: dict) -> bool:
    """True if this assistant record dispatched a router-* subagent (bare or
    namespaced auto-model-router:router-*)."""
    if rec.get("type") != "assistant":
        return False
    content = (rec.get("message") or {}).get("content") or []
    if not isinstance(content, list):
        return False
    for b in content:
        if (
            isinstance(b, dict)
            and b.get("type") == "tool_use"
            and b.get("name") == "Agent"
        ):
            st = (b.get("input") or {}).get("subagent_type", "")
            if isinstance(st, str) and _ROUTER_SUBAGENT_RE.match(st):
                return True
    return False


def usage_of(rec: dict):
    """-> (family, tokens_in, tokens_out) or None for an assistant record."""
    if rec.get("type") != "assistant":
        return None
    msg = rec.get("message") or {}
    usage = msg.get("usage") or {}
    fam = model_family(msg.get("model") or rec.get("model"))
    tin = usage.get("input_tokens") or 0
    tout = usage.get("output_tokens") or 0
    if not (tin or tout):
        return None
    return fam, int(tin), int(tout)


def _median(xs):
    return round(statistics.median(xs), 1) if xs else None


# ── Core analysis ───────────────────────────────────────────────────────────
def analyze(days: int, projects_dir: str = PROJECTS_DIR, judge=None, end=None) -> dict:
    """Walk transcripts in the window; return the full KPI dict.

    `judge`, if given, is a callable(text)->'correction'|'approval'|'neutral'
    applied to ambiguous turns (the --llm-judge path); otherwise heuristic only.
    `end`, if given, is the window's upper-bound datetime (default: now) — lets
    callers (e.g. --compare-days) walk a prior window instead of "now minus days".
    """
    end_dt = end or datetime.now(timezone.utc)
    cutoff = end_dt - timedelta(days=days)
    cutoff_epoch = cutoff.timestamp()

    tokens = defaultdict(lambda: {"in": 0, "out": 0})  # family -> tokens
    cohort = {  # router-delegated vs inline
        "router": Counter(),  # correction/approval/neutral counts
        "inline": Counter(),
    }
    durations = []  # seconds, per session
    ttfa = []  # time-to-first-approval, seconds
    sessions_scanned = 0

    files = []
    for path in glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(path) < cutoff_epoch:
                continue
        except OSError:
            continue
        files.append(path)

    for path in files:
        is_subagent = f"{os.sep}subagents{os.sep}" in path
        records = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError:
            continue

        # Token totals: fold every in-window assistant usage (incl. subagents).
        for rec in records:
            ts = parse_ts(rec.get("timestamp"))
            if ts is not None and (ts < cutoff or ts > end_dt):
                continue
            u = usage_of(rec)
            if u:
                fam, tin, tout = u
                key = fam or "other"
                tokens[key]["in"] += tin
                tokens[key]["out"] += tout

        if is_subagent:
            continue  # session-level metrics only from top-level transcripts

        # Session-level: duration, cohort corrections, time-to-approval.
        first_ts = last_ts = first_approval_ts = None
        pending_router = False
        seen_first_user = False
        for rec in records:
            ts = parse_ts(rec.get("timestamp"))
            if ts is not None and (ts < cutoff or ts > end_dt):
                continue
            rtype = rec.get("type")
            if ts is not None:
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                # last activity of ANY kind — a trailing user "perfect!" is part
                # of the session, and using last-assistant would let
                # time-to-approval exceed the duration.
                if last_ts is None or ts > last_ts:
                    last_ts = ts
            if rtype == "assistant":
                if assistant_router_dispatch(rec):
                    pending_router = True
                continue
            if is_human_turn(rec):
                text = block_text((rec.get("message") or {}).get("content"))
                if not seen_first_user:
                    seen_first_user = True  # initial request: not a correction
                    pending_router = False
                    continue
                label = classify_user_turn(text)
                if judge is not None and label == "neutral" and ambiguous_turn(text):
                    label = judge(text) or "neutral"
                bucket = "router" if pending_router else "inline"
                cohort[bucket][label] += 1
                if label == "approval" and first_approval_ts is None and ts:
                    first_approval_ts = ts
                pending_router = False

        if first_ts and last_ts and last_ts >= first_ts:
            durations.append((last_ts - first_ts).total_seconds())
            sessions_scanned += 1
            if first_approval_ts:
                ttfa.append((first_approval_ts - first_ts).total_seconds())

    return {
        "window_days": days,
        "sessions_scanned": sessions_scanned,
        "files_in_window": len(files),
        "tokens": {k: dict(v) for k, v in tokens.items()},
        "cohort": {k: dict(v) for k, v in cohort.items()},
        "durations_s": durations,
        "ttfa_s": ttfa,
        "audit": analyze_audit(days, end=end_dt),
    }


# Inline-skip outcome vocabulary. The canonical set is the skill's mandated
# spelling; aliases are drifted spellings seen in the wild that the analyzers
# used to drop — fold them into `inline_other` so they're counted, not lost.
# `auto_inline_unattributed` (reconcile-outcomes.py's deterministic backstop),
# `same_or_higher_inline` and `followup_inline` (auto-router.py's outcome_hint
# on a "silent" row — see below) are terminal explanations too; without them
# here, auto_unlogged double-counts decisions that were already accounted for.
CANONICAL_SKIPS = {
    "same_model_inline",
    "continuity_inline",
    "skipped_trivial",
    "worker_failed",
    "auto_inline_unattributed",
    "same_or_higher_inline",
    "followup_inline",
}
SKIP_ALIASES = {
    "inline_override": "inline_other",
    "override_inline": "inline_other",
    "stayed_inline": "inline_other",
    "inline": "inline_other",
}

# Cost rank, cheapest first — mirrors auto-router.py's TIER_RANK/MODEL_TIERS
# order (haiku, sonnet, opus, fable) without importing the hook module.
_FAMILY_RANK = {
    fam: i
    for i, fam in enumerate(
        sorted(PRICES, key=lambda f: PRICES[f]["in"] + PRICES[f]["out"])
    )
}


def _normalize_usage_keys(usage: dict) -> dict:
    """The audit log carries two usage-dict shapes: post-agent-audit.py's
    tokens_in/tokens_out/cache_read/cache_write, and reconcile-outcomes.py's
    input/output/cache_read/cache_write (plus context_tokens). Normalize both
    to the short keys pricing.cost_usd() understands."""
    return {
        "tokens_in": usage.get("tokens_in", usage.get("input", 0)) or 0,
        "tokens_out": usage.get("tokens_out", usage.get("output", 0)) or 0,
        "cache_read": usage.get("cache_read", 0) or 0,
        "cache_write": usage.get("cache_write", 0) or 0,
    }


def _realized_add(bucket: dict, totals: dict, kind: str, model_actual, usage) -> None:
    """Accumulate one dispatch/inline row's real usage into `bucket` (keyed
    "<kind>/<family>") and `totals` ({"cost": .., "opus_cost": ..})."""
    if not usage:
        return
    norm = _normalize_usage_keys(usage)
    tok = sum(norm.values())
    if not tok:
        return
    cost = cost_usd(model_actual, norm)
    fam = model_family(model_actual) or "other"
    key = f"{kind}/{fam}"
    entry = bucket.setdefault(key, {"tokens": 0, "cost_usd": 0.0})
    entry["tokens"] += tok
    entry["cost_usd"] += cost
    totals["cost"] += cost
    if fam in ("opus", "fable"):
        totals["opus_cost"] += cost


def analyze_audit(days: int, end=None) -> dict:
    """Band/source/model distribution + parallelism from the router's own log,
    plus the "Realized" rollup — actual $ spent (by kind x model_actual family),
    dispatch follow-through, escalation rate, and median live-context size —
    computed from the audit rows' real usage rather than the all-Opus
    counterfactual in cost_kpi().

    Reuses analyze-audit.py::find_parallel_batches via importlib (the module
    filename is hyphenated, so it can't be a normal import).

    `end`, if given, is the window's upper-bound datetime (default: now) — see
    analyze()'s docstring.
    """
    import importlib.util

    out = {"decisions": 0, "by_band": {}, "by_model": {}, "parallel": {}}
    rows = []
    try:
        with open(AUDIT_LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return out

    end_dt = end or datetime.now(timezone.utc)
    cutoff = end_dt - timedelta(days=days)
    by_band, by_model = Counter(), Counter()
    outcomes = []
    auto_ids = set()
    skip_outcomes = Counter()
    skip_ids_auto = 0
    dispatched = 0
    escalations = 0
    dispatch_ids = set()
    cheaper_decisions = []  # (decision_id, model, parent) where model < parent rank
    realized_bucket = {}
    realized_totals = {"cost": 0.0, "opus_cost": 0.0}
    ctx_tokens = []

    for r in rows:
        ts = parse_ts(r.get("ts"))
        if ts is not None and (ts < cutoff or ts > end_dt):
            continue
        oc = r.get("outcome")
        hint = r.get("outcome_hint")
        if oc in ("injected", "silent"):
            d = r.get("decision") or {}
            band = d.get("band", "?")
            by_band[band] += 1
            by_model[d.get("model", "?")] += 1
            out["decisions"] += 1
            did = d.get("decision_id")
            model, parent = d.get("model"), d.get("parent")
            if did and model in _FAMILY_RANK and parent in _FAMILY_RANK:
                if _FAMILY_RANK[model] < _FAMILY_RANK[parent]:
                    cheaper_decisions.append(did)
            if band == "auto" and did:
                auto_ids.add(did)
                # Some skip explanations live as outcome_hint on this same
                # "silent" row rather than a separate joined row (see
                # auto-router.py::audit_decision) — resolve them right here.
                if hint in CANONICAL_SKIPS or hint in SKIP_ALIASES:
                    skip_outcomes[SKIP_ALIASES.get(hint, hint)] += 1
                    skip_ids_auto += 1
        elif oc in ("delegated", "delegated_failed"):
            outcomes.append(r)
            dispatched += 1
            if r.get("escalation"):
                escalations += 1
            did = r.get("decision_id")
            if did:
                dispatch_ids.add(did)
            _realized_add(
                realized_bucket,
                realized_totals,
                r.get("kind") or "router",
                r.get("model_actual"),
                r.get("usage"),
            )
        elif oc == "native_dispatch":
            did = r.get("decision_id")
            if did:
                dispatch_ids.add(did)
            _realized_add(
                realized_bucket,
                realized_totals,
                r.get("kind") or "native",
                r.get("model_actual"),
                r.get("usage"),
            )
        elif oc in CANONICAL_SKIPS or oc in SKIP_ALIASES:
            skip_outcomes[SKIP_ALIASES.get(oc, oc)] += 1
            if r.get("decision_id") in auto_ids:
                skip_ids_auto += 1
            if oc == "auto_inline_unattributed":
                usage = r.get("usage") or {}
                _realized_add(
                    realized_bucket,
                    realized_totals,
                    "inline",
                    usage.get("model"),
                    usage,
                )
                if usage.get("context_tokens") is not None:
                    ctx_tokens.append(usage["context_tokens"])

    out["by_band"] = dict(by_band)
    out["by_model"] = dict(by_model)
    out["reconciliation"] = {
        "auto_decisions": len(auto_ids),
        "dispatched_total": dispatched,
        "auto_skip_logged": skip_ids_auto,
        "auto_unlogged": max(0, len(auto_ids) - skip_ids_auto),
        "skip_outcomes": dict(skip_outcomes),
    }

    followed = sum(1 for did in cheaper_decisions if did in dispatch_ids)
    out["realized"] = {
        "by_kind_model": {
            k: {"tokens": v["tokens"], "cost_usd": round(v["cost_usd"], 4)}
            for k, v in sorted(realized_bucket.items())
        },
        "total_cost_usd": round(realized_totals["cost"], 4),
        "opus_class_cost_pct": (
            round(100 * realized_totals["opus_cost"] / realized_totals["cost"], 1)
            if realized_totals["cost"]
            else None
        ),
        "dispatch_follow_through_pct": (
            round(100 * followed / len(cheaper_decisions), 1)
            if cheaper_decisions
            else None
        ),
        "escalation_rate_pct": (
            round(100 * escalations / dispatched, 1) if dispatched else None
        ),
        "median_context_tokens_per_turn": _median(ctx_tokens),
    }

    spec_path = os.path.join(os.path.dirname(__file__), "analyze-audit.py")
    try:
        spec = importlib.util.spec_from_file_location("analyze_audit", spec_path)
        aa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(aa)
        batches = aa.find_parallel_batches(outcomes)
        saved = sum(
            sum(w for w in b if isinstance(w, int))
            - max((w for w in b if isinstance(w, int)), default=0)
            for b in batches
            if sum(1 for w in b if isinstance(w, int)) >= 2
        )
        out["parallel"] = {
            "batches": len(batches),
            "max_width": max((len(b) for b in batches), default=0),
            "wall_ms_saved": saved,
        }
    except Exception:
        out["parallel"] = {"batches": 0, "max_width": 0, "wall_ms_saved": 0}
    return out


# ── KPI rollups ─────────────────────────────────────────────────────────────
def cost_kpi(tokens: dict) -> dict:
    total_in = sum(v["in"] for v in tokens.values())
    total_out = sum(v["out"] for v in tokens.values())
    total_tok = total_in + total_out
    offload = 0.0  # savings from families cheaper than baseline (Haiku/Sonnet)
    premium = 0.0  # extra spend from pricier-than-baseline families (Fable) — not router-attributable
    by_model_pct = {}
    for fam, v in tokens.items():
        if fam in PRICES:
            delta = counterfactual_saving(fam, v["in"], v["out"])
            if delta >= 0:
                offload += delta
            else:
                premium += delta
        share = (v["in"] + v["out"]) / total_tok if total_tok else 0.0
        by_model_pct[fam] = round(100 * share, 1)
    return {
        "tokens_in": total_in,
        "tokens_out": total_out,
        "pct_tokens_by_model": by_model_pct,
        "est_offload_saved_usd": round(offload, 2),
        "parent_model_premium_usd": round(premium, 2),
        "est_saved_vs_opus_usd": round(offload + premium, 2),
    }


def quality_kpi(cohort: dict) -> dict:
    def rate(c, label):
        total = sum(c.values())
        return round(100 * c.get(label, 0) / total, 1) if total else None

    return {
        "router": {
            "turns": sum(cohort["router"].values()),
            "correction_rate_pct": rate(cohort["router"], "correction"),
            "approval_rate_pct": rate(cohort["router"], "approval"),
        },
        "inline": {
            "turns": sum(cohort["inline"].values()),
            "correction_rate_pct": rate(cohort["inline"], "correction"),
            "approval_rate_pct": rate(cohort["inline"], "approval"),
        },
    }


def time_kpi(durations, ttfa) -> dict:
    return {
        "sessions": len(durations),
        "median_duration_min": round(_median(durations) / 60, 1) if durations else None,
        "median_time_to_first_approval_min": (
            round(_median(ttfa) / 60, 1) if ttfa else None
        ),
        "approvals_observed": len(ttfa),
    }


def build_report(data: dict) -> dict:
    return {
        "window_days": data["window_days"],
        "sessions_scanned": data["sessions_scanned"],
        "cost": cost_kpi(data["tokens"]),
        "quality": quality_kpi(data["cohort"]),
        "time": time_kpi(data["durations_s"], data["ttfa_s"]),
        "router_activity": data["audit"],
        "realized": data["audit"].get("realized", {}),
    }


# ── Rendering ───────────────────────────────────────────────────────────────
def _fmt_delta(cur, prior, pct=False) -> str:
    if cur is None or prior is None:
        return "n/a"
    d = cur - prior
    sign = "+" if d >= 0 else ""
    return f"{sign}{d:.1f}{'pp' if pct else ''}"


def render_markdown(
    rep: dict, today: str, compare: dict | None = None, compare_days: int | None = None
) -> str:
    c, q, t, a = rep["cost"], rep["quality"], rep["time"], rep["router_activity"]
    rc = a.get("reconciliation", {})
    r = rep["realized"]
    lines = [
        "---",
        f"date: {today}",
        f"window_days: {rep['window_days']}",
        "kind: router-usage-report",
        f"est_saved_usd: {c['est_saved_vs_opus_usd']}",
        "---",
        "",
        f"# Router Usage Report — last {rep['window_days']}d",
        "",
        f"Sessions scanned: {rep['sessions_scanned']} · "
        f"router decisions: {a['decisions']}",
        "",
        "## Descriptive — models / tokens / band",
        "",
        "% of tokens by model:",
    ]
    for fam, pct in sorted(c["pct_tokens_by_model"].items(), key=lambda x: -x[1]):
        lines.append(f"  - {fam:<7} {pct:>5}%")
    lines += [
        "",
        f"Router decisions by band: {a['by_band']}",
        f"Router decisions by model: {a['by_model']}",
        "",
        "## GOAL 1 — Cost efficiency",
        "",
        f"- **Cheaper-model offload saved: ${c['est_offload_saved_usd']}** "
        "(Haiku/Sonnet tokens vs the all-Opus baseline — the router-attributable win)",
        f"- **Parent-model premium: ${c['parent_model_premium_usd']}** "
        "(Fable parent-session tokens cost >Opus — a session-model choice, not routing)",
        f"- Net vs all-Opus: ${c['est_saved_vs_opus_usd']} (offload + premium; "
        "counterfactual estimate, mostly output tokens — see note)",
        f"- Uncached input: {c['tokens_in']:,} · output: {c['tokens_out']:,} "
        "(cache-read/creation tokens excluded; with prompt caching most input "
        "is cached, so uncached input is small by design)",
        "- % below is share of (uncached-input + output) tokens by model — the "
        "parent session model dominates; the cheaper-model share is the routed slice.",
        f"- Parallelism wall-clock saved: {a['parallel'].get('wall_ms_saved', 0)} ms "
        f"across {a['parallel'].get('batches', 0)} batch(es)",
        f"- Dispatch reconciliation (auto band): {rc.get('auto_decisions', 0)} decisions → "
        f"{rc.get('dispatched_total', 0)} dispatched (all bands — delegated rows carry no "
        f"decision_id, so not separable by band), {rc.get('auto_skip_logged', 0)} skip-logged, "
        f"{rc.get('auto_unlogged', 0)} with no skip row (dispatched-from-auto or silent inline)",
        f"  - skip outcomes, drifted spellings folded into inline_other: "
        f"{rc.get('skip_outcomes', {})}",
        "",
        "## Realized — actual $ from audit-row usage (not the all-Opus counterfactual)",
        "",
        f"- Total realized spend: ${r.get('total_cost_usd', 0)} "
        "(router/native dispatch rows + inline-unattributed usage, real tokens x real price)",
        f"- Opus-class (opus + fable) cost share: {r.get('opus_class_cost_pct')}%",
        f"- Dispatch follow-through: {r.get('dispatch_follow_through_pct')}% "
        "(of decisions routed to a strictly cheaper model than the parent, the "
        "% that produced a real delegated/native dispatch row)",
        f"- Escalation rate: {r.get('escalation_rate_pct')}% "
        "(router dispatches whose response flagged a re-dispatch-one-tier-up)",
        f"- Median live context per turn (inline, unattributed usage): "
        f"{r.get('median_context_tokens_per_turn')} tokens",
        "- $ / tokens by kind x model family:",
    ]
    by_kind_model = r.get("by_kind_model", {})
    if by_kind_model:
        for key, v in sorted(by_kind_model.items()):
            lines.append(f"    - {key:<14} {v['tokens']:>7,} tok  ${v['cost_usd']}")
    else:
        lines.append("    - (no dispatch/inline usage rows in window)")
    lines += [
        "",
        "## GOAL 2 — Quality (user-correction rate)",
        "",
        f"- Router-delegated turns: {q['router']['turns']} · "
        f"correction {q['router']['correction_rate_pct']}% · "
        f"approval {q['router']['approval_rate_pct']}%",
        f"- Inline turns: {q['inline']['turns']} · "
        f"correction {q['inline']['correction_rate_pct']}% · "
        f"approval {q['inline']['approval_rate_pct']}%",
        "- Lower correction rate is better; router cohort should not exceed inline.",
        "",
        "## GOAL 3 — Time-to-results",
        "",
        f"- Median session duration: {t['median_duration_min']} min "
        f"({t['sessions']} sessions)",
        f"- Median time-to-first-approval: "
        f"{t['median_time_to_first_approval_min']} min "
        f"({t['approvals_observed']} approvals observed)",
        "",
        "_Estimates. Quality detection is heuristic unless --llm-judge was used. "
        "Router-vs-inline timing is correlation, not causation._",
        "",
    ]

    if compare is not None:
        pc, pt, pr = compare["cost"], compare["time"], compare["realized"]
        pa = compare["router_activity"]
        lines += [
            f"## Comparison — prior {compare_days}d "
            f"(window ending {rep['window_days']}d ago)",
            "",
            f"- Router decisions: {a['decisions']} vs {pa['decisions']} "
            f"({_fmt_delta(a['decisions'], pa['decisions'])})",
            f"- Net vs all-Opus: ${c['est_saved_vs_opus_usd']} vs "
            f"${pc['est_saved_vs_opus_usd']} "
            f"({_fmt_delta(c['est_saved_vs_opus_usd'], pc['est_saved_vs_opus_usd'])})",
            f"- Realized total spend: ${r.get('total_cost_usd', 0)} vs "
            f"${pr.get('total_cost_usd', 0)} "
            f"({_fmt_delta(r.get('total_cost_usd'), pr.get('total_cost_usd'))})",
            f"- Opus-class cost share: {r.get('opus_class_cost_pct')}% vs "
            f"{pr.get('opus_class_cost_pct')}% "
            f"({_fmt_delta(r.get('opus_class_cost_pct'), pr.get('opus_class_cost_pct'), pct=True)})",
            f"- Dispatch follow-through: {r.get('dispatch_follow_through_pct')}% vs "
            f"{pr.get('dispatch_follow_through_pct')}% "
            f"({_fmt_delta(r.get('dispatch_follow_through_pct'), pr.get('dispatch_follow_through_pct'), pct=True)})",
            f"- Escalation rate: {r.get('escalation_rate_pct')}% vs "
            f"{pr.get('escalation_rate_pct')}% "
            f"({_fmt_delta(r.get('escalation_rate_pct'), pr.get('escalation_rate_pct'), pct=True)})",
            f"- Median session duration: {t['median_duration_min']} min vs "
            f"{pt['median_duration_min']} min "
            f"({_fmt_delta(t['median_duration_min'], pt['median_duration_min'])})",
            "",
        ]

    return "\n".join(lines)


# ── LLM judge (optional) ────────────────────────────────────────────────────
def make_haiku_judge():
    """Return a callable(text)->label using Haiku, or None if no API key."""
    import urllib.error
    import urllib.request

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    system = (
        "Classify the user's message as exactly one word: correction (they are "
        "pushing back on or correcting the assistant's prior work), approval "
        "(they are satisfied), or neutral. Reply with only that word."
    )

    def judge(text: str):
        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 5,
            "system": system,
            "messages": [{"role": "user", "content": text[:1000]}],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode(),
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                body = json.loads(resp.read())
            word = body["content"][0]["text"].strip().lower()
            return word if word in ("correction", "approval", "neutral") else "neutral"
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError):
            return None  # graceful: caller keeps heuristic label

    return judge


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--since", choices=["yesterday", "today"])
    p.add_argument("--json", action="store_true", help="emit JSON to stdout")
    p.add_argument("--llm-judge", action="store_true")
    p.add_argument("--write", action="store_true", help="write md to reports dir")
    p.add_argument(
        "--today", help="YYYY-MM-DD stamp for the report file (default: derived)"
    )
    p.add_argument(
        "--compare-days",
        type=int,
        default=None,
        help="also roll up the N days immediately before the current window "
        "and print deltas next to the current numbers",
    )
    args = p.parse_args()

    days = 1 if args.since in ("yesterday", "today") else args.days
    judge = make_haiku_judge() if args.llm_judge else None
    now = datetime.now(timezone.utc)
    data = analyze(days, judge=judge, end=now)
    rep = build_report(data)

    compare = None
    if args.compare_days:
        prior_end = now - timedelta(days=days)
        compare = build_report(
            analyze(args.compare_days, judge=judge, end=prior_end)
        )

    if args.json:
        out = dict(rep)
        if compare is not None:
            out["compare"] = compare
        print(json.dumps(out, indent=2))
        return 0

    today = args.today or now.strftime("%Y-%m-%d")
    md = render_markdown(rep, today, compare=compare, compare_days=args.compare_days)
    if args.write:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        out_path = os.path.join(REPORTS_DIR, f"{today}.md")
        with open(out_path, "w") as f:
            f.write(md)
        print(f"wrote {out_path}")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
