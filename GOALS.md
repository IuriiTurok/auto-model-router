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
Opus rates on Haiku-tier work.

**Primary metric — estimated $ saved vs all-Opus.**
For every token that ran on a cheaper model (Haiku/Sonnet), the saving is
`tokens × (Opus_rate − actual_rate)`, summed over the window. It is an
**estimate** of a counterfactual (what the same work would have cost on Opus),
not a billed number.

**Supporting metrics:** % of tokens by model (Haiku share ↑ is the signal the
router is working); wall-clock saved by parallelism.

**Target direction:** ↑ $ saved; Haiku share recovering toward ~15-20% (it had
collapsed to 3.7% pre-round-1).

Prices (per MTok, approx): Opus $5 in / $25 out · Sonnet $3 / $15 ·
Haiku $1 / $5.

---

## 2. Quality — *don't make the user fix our work*

**Goal:** routing to a cheaper or parallel path must not raise the rate at
which the user has to correct the assistant.

**Primary metric — user-correction rate, router vs non-router.**
The fraction of user turns that *correct* the assistant ("no, that's wrong",
"you shouldn't have", "revert", "that's not what I asked"), split by whether the
preceding work was router-delegated vs done inline. **Lower is better, and the
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

**Supporting metric:** parallelism wall-clock saved (`Σ wall_ms − max wall_ms`
per concurrent batch); fan-out width.

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
