# auto-model-router — North-Star Goals

Every routing decision serves three goals. They are the lens for the usage
report (`/router-report`) and the framing for both routing skills. When a
change helps one goal at another's expense, that trade-off must be deliberate.

The spine that serves all three at once:
**decompose → cheapest sufficient model per task → run independent work in
parallel → synthesize.**

---

## 1. Cost efficiency — *spend less per result*

**Goal:** route work to the cheapest model that can do it well; don't burn
the session model's rate on work a cheaper model can do.

**Primary metric — estimated $ saved vs the parent session's model.**
For every sub-agent token (cache included) that ran on a model cheaper than the
session that spawned it, the saving is
`usage priced at the parent's model − usage priced at the worker's model`,
summed over the window. It is an **estimate** of a counterfactual (what the
same work would have cost on the parent's model), not a billed number. A worker
dearer than its parent (e.g. `#model=fable` under an Opus session) shows up as
a negative premium. The report splits the saving by agent type (each
subagent's `<agent>.meta.json`): only the router workers' share is the
router's win.

**Supporting metrics:** % of tokens by model; wall-clock saved by parallelism.

**Target direction:** ↑ $ saved without a rise in the correction rate (§2).

Prices (per MTok, list): Opus 5.5 $4 in / $20 out · Sonnet 5.5 $2 / $10 ·
Haiku 5.5 $0.10 / $0.50 · Fable 5.1 $10 / $50 (2.5x Opus). Cache reads are 5% of
input on Opus/Sonnet 5.5, 10% on Haiku 5.5, 2.5% on Fable 5.1; cache writes are
2x input (1-hour TTL). Older models still served keep their own prices (see
`hooks/pricing.py`).

---

## 2. Quality — *don't make the user fix our work*

**Goal:** routing to a cheaper or parallel path must not raise the rate at
which the user has to correct the assistant.

**Primary metric — user-correction rate, router vs non-router.**
The fraction of user turns that *correct* the assistant ("no, that's wrong",
"you shouldn't have", "revert", "that's not what I asked"), split by whether the
preceding work was router-delegated vs done inline. Harness events (task
notifications, scheduled-task runs, system / CI notices) are not user turns. **Lower is better, and the
router cohort must not be meaningfully worse than the inline cohort** — if it
is, the router is trading quality for cost and the tiers need adjusting.

**Supporting basket:**
- **escalation rate** — delegated workers returning `Stopped:` / `Done with caveats:`
- **approval rate** — explicit satisfaction ("perfect", "ship it", "works")
- **worker-failure rate** — `delegated_failed` ÷ delegated
- **rework rate** *(optional)* — a file re-edited right after a correction

**Target direction:** ↓ correction rate; router-cohort correction rate ≤
inline-cohort; ↓ escalation/failure; ↑ approval.

---

## 3. Time-to-results — *deliver faster*

**Goal:** finish the user's task in less wall-clock time, primarily by running
independent work in parallel.

**Primary metric — time-to-first-approval** (first user message → first
approval signal), with **session duration** (first user msg → last assistant
msg) as the coarse proxy.

**Supporting metric:** parallel-worker wall-clock saved (per session, the
summed length of worker activity segments minus the length of their union,
read from subagent transcripts; a background worker's dispatch `wall_ms` is
only its launch); peak concurrent workers.

**Target direction:** ↓ time-to-result on parallelizable tasks; fan-out
actually firing on multi-part prompts.

*Caveat:* router-heavy sessions are usually heavier tasks, so a raw
router-vs-inline duration comparison is **correlation, not causation** — the
report states this explicitly.

---

## How these are measured

`tools/usage-report.py` (run via `/router-report [7d|1d|yesterday]`) reads the
router audit log (`~/.claude/cache/router/audit.jsonl`) and session transcripts
(`~/.claude/projects/**`), joins them by `prompt_sha`, scores all three goals,
writes a dated report under `~/.claude/cache/router/reports/`, records lessons
learned, and offers concrete improvements. See `README.md` → "Goals &
reporting".
