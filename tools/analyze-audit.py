#!/usr/bin/env python3
"""Re-runnable analyzer for ~/.claude/cache/router/audit.jsonl.

Prints distributions and top reasons that mirror the Phase 1 baseline
table used by the refactor plan, so the same picture can be re-measured
after deploy and the deltas read off directly.

Usage:
    python3 tools/analyze-audit.py [--audit PATH] [--overrides PATH] [--days N]

Outputs (in order):
    1. Volume + recency
    2. Distributions: model, tier, effort, band, source
    3. Confidence histogram + % >= 0.90
    4. Top 15 reasons
    5. Outcome-capture summary (delegated/failed/escalation rate,
       median wall_ms, median tokens) — uses the extended fields added
       by hooks/post-agent-audit.py.
    6. Ask-band overrides summary (if overrides.jsonl exists).
"""

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone


def load_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    return out


def parse_ts(ts):
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (ValueError, OSError):
            return None
    if isinstance(ts, str):
        try:
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            return datetime.fromisoformat(ts)
        except ValueError:
            return None
    return None


def filter_recent(rows, days):
    if not days:
        return rows
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for r in rows:
        ts = parse_ts(r.get("ts"))
        if ts is None or ts >= cutoff:
            out.append(r)
    return out


def section(title):
    print()
    print(title)
    print("-" * len(title))


def pct(n, total):
    return f"{(100 * n / total):.1f}%" if total else "n/a"


def print_counter(c, total, limit=None):
    items = c.most_common(limit) if limit else c.most_common()
    for k, v in items:
        print(f"  {str(k):<32} {v:>5}  {pct(v, total):>7}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    home = os.path.expanduser("~")
    p.add_argument(
        "--audit",
        default=os.path.join(home, ".claude/cache/router/audit.jsonl"),
    )
    p.add_argument(
        "--overrides",
        default=os.path.join(home, ".claude/cache/router/overrides.jsonl"),
    )
    p.add_argument(
        "--days", type=int, default=0, help="restrict to last N days (0 = all)"
    )
    args = p.parse_args()

    rows = load_jsonl(args.audit)
    if args.days:
        rows = filter_recent(rows, args.days)

    if not rows:
        print(f"No rows in {args.audit}")
        return 0

    # ── Volume + recency ────────────────────────────────────────────────
    timestamps = [parse_ts(r.get("ts")) for r in rows]
    timestamps = [t for t in timestamps if t is not None]
    section("1. VOLUME")
    print(f"  rows           : {len(rows)}")
    if timestamps:
        print(f"  first ts       : {min(timestamps).isoformat()}")
        print(f"  last ts        : {max(timestamps).isoformat()}")
        span_days = (max(timestamps) - min(timestamps)).total_seconds() / 86400
        if span_days >= 0.1:
            print(f"  span           : {span_days:.1f} days")
            print(f"  rows/day (avg) : {len(rows) / max(span_days, 1):.1f}")
        per_day = Counter(t.date().isoformat() for t in timestamps)
        recent = sorted(per_day.items())[-7:]
        print("  last 7 dates   :")
        for d, n in recent:
            print(f"    {d}  {n}")

    # ── Decision rows vs outcome rows ───────────────────────────────────
    decisions = [r for r in rows if r.get("outcome") in ("injected", "silent")]
    outcomes = [
        r
        for r in rows
        if r.get("outcome") in ("delegated", "delegated_failed", "worker_failed")
    ]

    # Distributions (over decision rows)
    if decisions:
        section("2. DECISION DISTRIBUTIONS")
        total = len(decisions)
        print(f"  decisions: {total}")

        def get(r, *path, default=None):
            cur = r
            for k in path:
                if not isinstance(cur, dict):
                    return default
                cur = cur.get(k)
            return cur if cur is not None else default

        for label, key in [
            ("model", ("decision", "model")),
            ("tier", ("decision", "tier")),
            ("effort", ("decision", "effort")),
            ("band", ("decision", "band")),
            ("source", ("decision", "source")),
        ]:
            c = Counter(str(get(r, *key, default="<missing>")) for r in decisions)
            print()
            print(f"  by {label}:")
            print_counter(c, total)

        # ── Confidence histogram ────────────────────────────────────────
        section("3. CONFIDENCE")
        confs = []
        for r in decisions:
            v = get(r, "decision", "confidence")
            try:
                confs.append(float(v))
            except (TypeError, ValueError):
                pass
        if confs:
            buckets = Counter()
            for c in confs:
                b = min(int(c * 10), 9) / 10
                buckets[b] += 1
            for b in sorted(buckets):
                bar = "#" * max(1, int(40 * buckets[b] / max(buckets.values())))
                print(
                    f"  {b:.1f}-{b + 0.1:.1f}  {buckets[b]:>5}  "
                    f"{pct(buckets[b], len(confs)):>7}  {bar}"
                )
            hi = sum(1 for c in confs if c >= 0.90)
            print()
            print(f"  >= 0.90        : {hi} / {len(confs)}  ({pct(hi, len(confs))})")
            print(f"  median         : {statistics.median(confs):.2f}")

        # ── Top reasons ─────────────────────────────────────────────────
        section("4. TOP 15 REASONS")
        reasons = Counter(
            str(get(r, "decision", "reason", default="<missing>"))[:80]
            for r in decisions
        )
        print_counter(reasons, total, limit=15)

    # ── Outcome capture ─────────────────────────────────────────────────
    if outcomes:
        section("5. OUTCOMES (delegated subagent calls)")
        total = len(outcomes)
        print(f"  outcome rows: {total}")
        oc = Counter(r.get("outcome", "?") for r in outcomes)
        print_counter(oc, total)

        walls = [r["wall_ms"] for r in outcomes if isinstance(r.get("wall_ms"), int)]
        if walls:
            print()
            print(f"  wall_ms (n={len(walls)}):")
            print(f"    median       : {int(statistics.median(walls))}")
            print(
                f"    p90          : {int(statistics.quantiles(walls, n=10)[-1]) if len(walls) > 1 else walls[0]}"
            )

        tokens_in = [
            r["usage"]["tokens_in"]
            for r in outcomes
            if isinstance(r.get("usage"), dict)
            and isinstance(r["usage"].get("tokens_in"), int)
        ]
        tokens_out = [
            r["usage"]["tokens_out"]
            for r in outcomes
            if isinstance(r.get("usage"), dict)
            and isinstance(r["usage"].get("tokens_out"), int)
        ]
        if tokens_in or tokens_out:
            print()
            print("  tokens:")
            if tokens_in:
                print(
                    f"    in   median  : {int(statistics.median(tokens_in))}  "
                    f"sum: {sum(tokens_in)}"
                )
            if tokens_out:
                print(
                    f"    out  median  : {int(statistics.median(tokens_out))}  "
                    f"sum: {sum(tokens_out)}"
                )

        esc = Counter(r["escalation"] for r in outcomes if r.get("escalation"))
        if esc:
            print()
            print("  escalation signals:")
            print_counter(esc, total)

        # Per-model breakdown
        per_model = defaultdict(list)
        for r in outcomes:
            if isinstance(r.get("wall_ms"), int):
                per_model[r.get("model", "?")].append(r["wall_ms"])
        if per_model:
            print()
            print("  median wall_ms by model:")
            for m, ws in sorted(per_model.items()):
                print(
                    f"    {m:<10} n={len(ws):>4}  median={int(statistics.median(ws)):>6}"
                )

    # ── Ask-band overrides ──────────────────────────────────────────────
    overrides = load_jsonl(args.overrides) if args.overrides else []
    if args.days:
        overrides = filter_recent(overrides, args.days)
    if overrides:
        section("6. ASK-BAND USER OVERRIDES")
        total = len(overrides)
        print(f"  overrides logged: {total}")
        choices = Counter(o.get("user_choice", "?") for o in overrides)
        print()
        print("  by user_choice:")
        print_counter(choices, total)
        # Cross-tab suggested vs chosen
        ct = Counter(
            (o.get("suggested", "?"), o.get("user_choice", "?")) for o in overrides
        )
        print()
        print("  suggested -> chosen:")
        for (s, c), n in ct.most_common():
            print(f"    {s:<8} -> {c:<16} {n}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
