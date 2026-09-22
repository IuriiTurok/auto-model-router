#!/usr/bin/env python3
"""Offline gate for the session-level model pivot.

Counterfactual: for sessions whose ROUTER CLASSIFICATION is cheap-majority
(sonnet/haiku/fable), what would the session have cost run entirely on a cheaper
candidate tier, vs. what it actually cost on its real parent model? Re-prices the
realized per-turn token footprints (reconcile-row `usage` blocks) at the candidate
tier, token counts held constant (first-order, conservative — a smaller model on a
smaller context would usually use fewer cache tokens, so this UNDERstates savings).

Regret surface: a cheap-majority session that contains an opus-classified turn
cannot be rescued by the downhill-only router if the whole session is pinned
cheaper. Each such turn is split:
  - benign: a low/medium-effort deep-verb rule hit (the regex over-firing on
    casual phrasing) — cosmetic; a cheaper pin would very likely have coped.
  - real:   high/xhigh effort, or a high-blast rule (migration / infra) — a
    cheaper pin would plausibly have under-served the turn.
`regret_real_rate` (share of cheap sessions with >=1 real opus turn) is the
go/no-go gate: pin only projects under the threshold (design default 10%).

Read-only. No behavior change. Mirrors tools/replay_kpi.py conventions.

CLI:
  python3 replay_session_pivot.py [--audit PATH] [--days 30] [--candidate sonnet]
                                  [--gate 0.10] [--table]
  -> JSON {window_days, candidate, projects:{...}, total:{...}}
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRICING = os.path.join(_PLUGIN_ROOT, "hooks", "pricing.py")
DEFAULT_AUDIT = os.path.expanduser(
    os.path.join(
        os.environ.get("CC_ROUTER_CACHE_DIR", "~/.claude/cache/router"),
        "audit.jsonl",
    )
)

CHEAP = {"sonnet", "haiku", "fable"}
HEAVY_EFFORT = {"high", "xhigh"}
# reason substrings that mark a genuinely high-blast opus verdict (not an over-fire)
_REAL_RULE = re.compile(r"migrat|infra|blast|high-stakes|root.?cause", re.I)
# the deep-verb rule's signature (the classic regex over-fire when effort is light)
_DEEP_VERB = re.compile(r"deep.?verb|plan/design", re.I)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def norm_usage(u: dict) -> dict:
    """Map reconcile-row usage keys -> keys pricing.cost_usd understands."""
    if not isinstance(u, dict):
        return {}
    return {
        "input_tokens": u.get("input", u.get("input_tokens", u.get("tokens_in", 0))) or 0,
        "output_tokens": u.get("output", u.get("output_tokens", u.get("tokens_out", 0))) or 0,
        "cache_read": u.get("cache_read", u.get("cache_read_input_tokens", 0)) or 0,
        "cache_write": u.get("cache_write", u.get("cache_creation_input_tokens", 0)) or 0,
    }


def proj_name(cfg: str | None) -> str:
    if not cfg:
        return "(global/none)"
    parts = cfg.split("/")
    try:
        return parts[parts.index(".claude") - 1]
    except ValueError:
        return os.path.basename(os.path.dirname(cfg))


def classify_opus_turn(dec: dict) -> str:
    """'real' | 'benign' for an opus-classified decision.

    Conservative: anything not clearly a light deep-verb over-fire is 'real'.
    """
    effort = (dec.get("effort") or "").lower()
    reason = dec.get("reason") or ""
    if effort in HEAVY_EFFORT:
        return "real"
    if _REAL_RULE.search(reason):
        return "real"
    if _DEEP_VERB.search(reason) and effort in ("low", "medium", ""):
        return "benign"
    return "real"  # unknown/other -> conservative


def analyze(audit_path: str, days: int, candidate: str, pricing) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    dec_by_session: dict[str, list[dict]] = defaultdict(list)
    proj_by_session: dict[str, str] = {}
    usage_by_session: dict[str, list[tuple]] = defaultdict(list)

    with open(audit_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = row.get("ts")
            if ts:
                try:
                    if datetime.fromisoformat(ts) < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
            sid = row.get("session_id")
            if not sid:
                continue
            dec = row.get("decision")
            if isinstance(dec, dict) and "model" in dec:
                dec_by_session[sid].append(dec)
                proj_by_session[sid] = proj_name(dec.get("project_config"))
            u = row.get("usage")
            if isinstance(u, dict):
                usage_by_session[sid].append((u.get("model"), norm_usage(u)))

    projects: dict[str, dict] = defaultdict(lambda: {
        "sessions": 0, "cheap_sessions": 0,
        "realized_usd": 0.0, "cf_usd": 0.0,
        "regret_none": 0, "regret_benign": 0, "regret_real": 0,
        "opus_turns_benign": 0, "opus_turns_real": 0,
    })

    for sid, decs in dec_by_session.items():
        proj = proj_by_session.get(sid, "(unknown)")
        p = projects[proj]
        p["sessions"] += 1
        maj = Counter(d["model"] for d in decs).most_common(1)[0][0]
        if maj not in CHEAP:
            continue
        p["cheap_sessions"] += 1
        # counterfactual cost over this session's realized usage
        for model_actual, u in usage_by_session.get(sid, []):
            p["realized_usd"] += pricing.cost_usd(model_actual, u)
            p["cf_usd"] += pricing.cost_usd(candidate, u)
        # regret split
        opus_decs = [d for d in decs if d["model"] == "opus"]
        kinds = [classify_opus_turn(d) for d in opus_decs]
        p["opus_turns_benign"] += kinds.count("benign")
        p["opus_turns_real"] += kinds.count("real")
        if not opus_decs:
            p["regret_none"] += 1
        elif "real" in kinds:
            p["regret_real"] += 1
        else:
            p["regret_benign"] += 1

    wk = 7.0 / days
    out_projects = {}
    tot = {"cheap_sessions": 0, "realized_wk": 0.0, "cf_wk": 0.0, "save_wk": 0.0,
           "regret_real": 0}
    for proj, p in projects.items():
        cheap = p["cheap_sessions"]
        save_wk = (p["realized_usd"] - p["cf_usd"]) * wk
        rrr = round(100.0 * p["regret_real"] / cheap, 1) if cheap else 0.0
        out_projects[proj] = {
            "sessions": p["sessions"],
            "cheap_sessions": cheap,
            "realized_usd_per_wk": round(p["realized_usd"] * wk, 2),
            "cf_usd_per_wk": round(p["cf_usd"] * wk, 2),
            "save_usd_per_wk": round(save_wk, 2),
            "regret_none": p["regret_none"],
            "regret_benign": p["regret_benign"],
            "regret_real": p["regret_real"],
            "regret_real_rate_pct": rrr,
            "opus_turns_benign": p["opus_turns_benign"],
            "opus_turns_real": p["opus_turns_real"],
        }
        tot["cheap_sessions"] += cheap
        tot["realized_wk"] += p["realized_usd"] * wk
        tot["cf_wk"] += p["cf_usd"] * wk
        tot["save_wk"] += save_wk
        tot["regret_real"] += p["regret_real"]
    total = {
        "cheap_sessions": tot["cheap_sessions"],
        "realized_usd_per_wk": round(tot["realized_wk"], 2),
        "cf_usd_per_wk": round(tot["cf_wk"], 2),
        "save_usd_per_wk": round(tot["save_wk"], 2),
        "regret_real_rate_pct": (
            round(100.0 * tot["regret_real"] / tot["cheap_sessions"], 1)
            if tot["cheap_sessions"] else 0.0
        ),
    }
    return {"window_days": days, "candidate": candidate,
            "projects": out_projects, "total": total}


def render_table(result: dict, gate: float) -> str:
    lines = []
    hdr = (f"{'project':<22}{'sess':>5}{'cheap':>6}{'reg_real%':>10}"
           f"{'benign':>7}{'real$/wk':>10}{'cf$/wk':>9}{'save$/wk':>10}{'gate':>6}")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    gate_pct = gate * 100
    rows = sorted(result["projects"].items(),
                  key=lambda kv: -kv[1]["save_usd_per_wk"])
    for proj, p in rows:
        verdict = "PASS" if (p["cheap_sessions"] and p["regret_real_rate_pct"] <= gate_pct
                             and p["save_usd_per_wk"] > 0) else "no"
        lines.append(
            f"{proj:<22}{p['sessions']:>5}{p['cheap_sessions']:>6}"
            f"{p['regret_real_rate_pct']:>9.0f}%{p['regret_benign']:>7}"
            f"{p['realized_usd_per_wk']:>10.2f}{p['cf_usd_per_wk']:>9.2f}"
            f"{p['save_usd_per_wk']:>10.2f}{verdict:>6}")
    t = result["total"]
    lines.append("-" * len(hdr))
    lines.append(f"{'TOTAL':<22}{'':>5}{t['cheap_sessions']:>6}"
                 f"{t['regret_real_rate_pct']:>9.0f}%{'':>7}"
                 f"{t['realized_usd_per_wk']:>10.2f}{t['cf_usd_per_wk']:>9.2f}"
                 f"{t['save_usd_per_wk']:>10.2f}")
    lines.append(f"\ngate: regret_real_rate <= {gate_pct:.0f}% AND save$/wk > 0. "
                 f"candidate tier = {result['candidate']}, window = {result['window_days']}d.")
    lines.append("NOTE: token counts held constant (first-order, conservative). "
                 "$ are pricing.py estimates. Regret split is audit-only "
                 "(effort+reason); a transcript correction pass is higher fidelity.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", default=DEFAULT_AUDIT)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--candidate", default="sonnet")
    ap.add_argument("--gate", type=float, default=0.10,
                    help="regret_real_rate go/no-go threshold (fraction)")
    ap.add_argument("--table", action="store_true", help="human-readable table")
    a = ap.parse_args(argv)
    pricing = _load(PRICING, "pricing")
    result = analyze(a.audit, a.days, a.candidate, pricing)
    if a.table:
        print(render_table(result, a.gate))
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
