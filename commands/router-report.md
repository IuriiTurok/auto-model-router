---
description: Generate the router usage report for a window (default 7 days; also 1d / yesterday), scoring the three north-star goals (cost saved, user-correction rate, time-to-results). Writes lessons learned and offers concrete router improvements. Usage `/router-report [7d|1d|yesterday] [--llm-judge]`.
---

# /router-report

Run the router's KPI usage report, write lessons learned, then offer
improvements. See `GOALS.md` for the metric definitions.

## Step 1 — Resolve the window

Parse the argument:
- `7d` / nothing / a number → `--days N` (default 7).
- `1d` / `today` → `--days 1`.
- `yesterday` → `--since yesterday`.
- `--llm-judge` anywhere → add it (sharper correction detection via Haiku;
  needs `ANTHROPIC_API_KEY`, degrades gracefully without).

## Step 2 — Run the report function (writes the dated report)

```bash
python3 ~/.claude/plugins/auto-model-router/tools/usage-report.py \
  --days <N> [--since yesterday] [--llm-judge] --write
```

`--write` saves `~/.claude/cache/router/reports/<YYYY-MM-DD>.md`. Also capture
the machine view for your own reasoning:

```bash
python3 ~/.claude/plugins/auto-model-router/tools/usage-report.py \
  --days <N> [--since yesterday] [--llm-judge] --json
```

Relay the report's headline numbers to the user in a few lines: estimated $
saved, % tokens by model, router-vs-inline correction rate, median
time-to-result. Don't paste the whole markdown — point to the file.

## Step 3 — Write Lessons Learned

Append a dated entry to
`~/.claude/cache/router/reports/lessons-learned.md` (create if missing).
Each entry: the date, the window, 2-4 bullet observations tied to the three
goals, and any tweak you're about to offer (so the 7-day anti-oscillation
guard below can see it). Keep it terse — this is the router's memory.

```bash
python3 - <<'PY'
import os, datetime
p = os.path.expanduser("~/.claude/cache/router/reports/lessons-learned.md")
os.makedirs(os.path.dirname(p), exist_ok=True)
entry = """## <DATE> (last <N>d)
- <observation tied to cost / quality / time>
- Offered tweak: <one line, or "none">
"""
open(p, "a").write(entry + "\n")
PY
```

## Step 4 — Offer improvements (offer, don't apply)

From the JSON, derive **2-4 concrete, data-driven** tweaks. Examples of the
*shape* (use the real numbers, don't invent):

- **Quality:** if the router cohort's correction rate is meaningfully higher
  than inline for a tier/model → "bump tier X from Sonnet to Opus" or "tighten
  the heuristic that routed these".
- **Cost:** if Haiku share is still low while many decisions are trivial
  lookups → "lower a project's `default_model`" or "add a `.claude/router.json`
  rule routing pattern Y to haiku".
- **Ask-band:** read `~/.claude/cache/router/overrides.jsonl`; if the user
  almost always overrides ask→opus for a pattern → "add a project rule so it
  auto-routes opus and skips the prompt".
- **Time:** if multi-part prompts aren't fanning out (parallel batches ≈ 0
  while fanout decisions > 0) → "the parent isn't following Branch E — clarify
  the skill" .

Before offering a tweak, check `lessons-learned.md`: **do not re-offer a tweak
logged in the last 7 days** (anti-oscillation).

Present them with `AskUserQuestion` (one question, the tweaks as options, plus
the implicit "none"). On acceptance: **draft the change and show it for
approval** — edit `.claude/router.json`, a heuristic in
`hooks/auto-router.py`, or a skill — but do not commit or apply without the
user's go-ahead. This command **offers**; it never silently changes routing.

## Notes

- Read-only until Step 4's accepted draft. The report and lessons writes go
  only under `~/.claude/cache/router/`.
- All figures are estimates; correction detection is heuristic unless
  `--llm-judge` ran. Router-vs-inline timing is correlation, not causation.
- Honors `CC_ROUTER_CACHE_DIR` (so it works against a test/throwaway dir too).
