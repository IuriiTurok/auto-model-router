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
from pricing import PRICES, counterfactual_saving, model_family  # noqa: E402

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


def assistant_router_dispatch(rec: dict) -> bool:
    """True if this assistant record dispatched a router-* subagent."""
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
            if isinstance(st, str) and st.startswith("router-"):
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


# ── Core analysis ───────────────────────────────────────────────────────────
def analyze(days: int, projects_dir: str = PROJECTS_DIR, judge=None) -> dict:
    """Walk transcripts in the window; return the full KPI dict.

    `judge`, if given, is a callable(text)->'correction'|'approval'|'neutral'
    applied to ambiguous turns (the --llm-judge path); otherwise heuristic only.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
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
            if ts is not None and ts < cutoff:
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
            if ts is not None and ts < cutoff:
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
        "audit": analyze_audit(days),
    }


def analyze_audit(days: int) -> dict:
    """Band/source/model distribution + parallelism from the router's own log.

    Reuses analyze-audit.py::find_parallel_batches via importlib (the module
    filename is hyphenated, so it can't be a normal import)."""
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

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    by_band, by_model = Counter(), Counter()
    outcomes = []
    for r in rows:
        ts = parse_ts(r.get("ts"))
        if ts is not None and ts < cutoff:
            continue
        oc = r.get("outcome")
        if oc in ("injected", "silent"):
            d = r.get("decision") or {}
            by_band[d.get("band", "?")] += 1
            by_model[d.get("model", "?")] += 1
            out["decisions"] += 1
        elif oc in ("delegated", "delegated_failed"):
            outcomes.append(r)

    out["by_band"] = dict(by_band)
    out["by_model"] = dict(by_model)

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
    saved = 0.0
    by_model_pct = {}
    for fam, v in tokens.items():
        saved += counterfactual_saving(fam, v["in"], v["out"]) if fam in PRICES else 0.0
        share = (v["in"] + v["out"]) / total_tok if total_tok else 0.0
        by_model_pct[fam] = round(100 * share, 1)
    return {
        "tokens_in": total_in,
        "tokens_out": total_out,
        "pct_tokens_by_model": by_model_pct,
        "est_saved_vs_opus_usd": round(saved, 2),
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


def _median(xs):
    return round(statistics.median(xs), 1) if xs else None


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
    }


# ── Rendering ───────────────────────────────────────────────────────────────
def render_markdown(rep: dict, today: str) -> str:
    c, q, t, a = rep["cost"], rep["quality"], rep["time"], rep["router_activity"]
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
        f"- **Estimated $ saved vs all-Opus: ${c['est_saved_vs_opus_usd']}** "
        "(counterfactual estimate; mostly from output tokens — see note)",
        f"- Uncached input: {c['tokens_in']:,} · output: {c['tokens_out']:,} "
        "(cache-read/creation tokens excluded; with prompt caching most input "
        "is cached, so uncached input is small by design)",
        "- % below is share of (uncached-input + output) tokens by model — the "
        "parent session model dominates; the cheaper-model share is the routed slice.",
        f"- Parallelism wall-clock saved: {a['parallel'].get('wall_ms_saved', 0)} ms "
        f"across {a['parallel'].get('batches', 0)} batch(es)",
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
    args = p.parse_args()

    days = 1 if args.since in ("yesterday", "today") else args.days
    judge = make_haiku_judge() if args.llm_judge else None
    data = analyze(days, judge=judge)
    rep = build_report(data)

    if args.json:
        print(json.dumps(rep, indent=2))
        return 0

    today = args.today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    md = render_markdown(rep, today)
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
