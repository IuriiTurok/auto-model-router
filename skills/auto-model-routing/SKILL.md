---
name: auto-model-routing
description: Use whenever a `<router-decision>` block is present in injected context (emitted by the auto-router UserPromptSubmit hook). Routes work to a model-specific subagent based on classifier confidence, or asks the user when ambiguous. Mandatory before doing inline work when the band is `auto` or `ask`.
---

# auto-model-routing

You are running as a **router parent**. The auto-model-router hook has
classified the user's prompt and emitted a `<router-decision>` block in
your injected context. Honour it before doing any work.

## Decision schema

```json
{
  "band": "auto" | "ask" | "none",
  "model": "haiku" | "sonnet" | "opus" | "fable",
  "effort": "low" | "medium" | "high" | "xhigh",
  "tier": "trivial" | "standard" | "complex" | "deep" | "override" | "optout",
  "confidence": 0.0-1.0,
  "reason": "one-sentence justification",
  "source": "heuristic" | "haiku" | "default" | "override" | "project_config",
  "plan_mode": true | false,
  "fanout": true | false,
  "fanout_hint": 3,
  "decision_id": "r_xxxxxxxxxx",
  "thresholds": {"auto": 0.75, "ask": 0.60},
  "continuity": true | false
}
```

`fanout` (with `fanout_hint` ≈ subtask count) means the hook detected a
**decomposable** prompt — multiple independent asks. Handle it with Branch E.
`plan_mode` is **best-effort and usually `false`** — the hook can't see plan
mode reliably. Trust your own context over this field (see Procedure).
`tier == "optout"` means the user or project config has opted out of routing
entirely for this prompt (`#noshift`, `#noroute`, or `"disabled": true`); treat
it as `band == "none"` and proceed inline.
`continuity: true` means the classifier detected that this prompt is a
follow-up on an in-progress task — prefer staying inline rather than
re-delegating to a worker (log outcome `continuity_inline`).

## Procedure

```
IN PLAN MODE?  → don't delegate; use `plan-with-models` (see below) — FIRST CHECK
SAME MODEL?    → suggested model == your session model → stay inline — SECOND CHECK
band == "auto" + fanout → Branch E: decompose + parallel dispatch
band == "auto" → Branch A: DELEGATE to one worker
band == "ask"  → Branch B: CONFIRM, then delegate (or fan out)
band == "none" → Branch C: IGNORE (do the work yourself)
```

**Plan mode is parent-authoritative.** Check it FIRST, before reading the
band. If a system reminder in your context says plan mode is active, route to
`plan-with-models` regardless of the `plan_mode` field in the decision block —
that field is best-effort and is almost always `false` even when you ARE in
plan mode (the hook can't see plan state). Do not trust it; trust your own
context.

**Same-model short-circuit (second check).** If the suggested model is the
model this session is already running on (an `opus` suggestion while you run
Opus or another opus-class model counts), dispatching saves nothing and adds
latency — stay inline. On `auto` band, log outcome `same_model_inline` (see
Outcome logging). On `ask` band, skip the question and log
`user_choice: "auto_inline_same_model"` to overrides.jsonl (Branch B step 3
format). Every recorded ask-band override to date picked Stay-on-current in
exactly this situation.

### Branch A — `band == "auto"`

1. Dispatch the user's task via the `Agent` tool with:
   - `subagent_type: "router-<model>"` (e.g. `router-sonnet`)
   - `description`: short (3-5 word) summary of the task
   - `prompt`: the user's verbatim request, plus any context they
     supplied in this turn (file paths they referenced, follow-up
     details from earlier turns, etc.). Frame it so the agent can act
     cold.
2. While the agent runs, don't duplicate work on the **same** subtask. (You
   **may** fan out across **independent** subtasks — that's Branch E, not a
   violation of this rule.)
3. When it returns, relay its result to the user in **1–2 sentences**
   (plus any direct artifact URLs). Do not paraphrase its entire output.
4. Do NOT hand-log the dispatch — the PostToolUse hook
   (`post-agent-audit.py`) records every `router-*` Agent call
   automatically (outcome `delegated`, plus tokens, wall time, and
   `group_id`). You write a row yourself only when you DON'T dispatch —
   see Outcome logging below.

### Branch B — `band == "ask"`

1. Call `AskUserQuestion` with one question:
   - **header**: `Model choice`
   - **question**: "Auto-router suggests `<model>` (confidence
     `<confidence>`). Use it, override, or stay on the current session
     model?"
   - **options**:
     - `Use <model> (Recommended)` — proceed via `router-<model>` agent.
     - `Use Opus` — proceed via `router-opus` agent.
     - `Stay on current` — handle the prompt inline on the current
       session model.
2. Wait for the answer, then execute the chosen branch.
3. **Log the user's choice** before doing the work, so the router has
   the labeled training data it needs to tighten heuristics later.
   Append one JSON line to `~/.claude/cache/router/overrides.jsonl`
   using a single Bash call:

   ```bash
   python3 -c "import json,sys,os,datetime; \
   p=os.path.expanduser('~/.claude/cache/router/overrides.jsonl'); \
   os.makedirs(os.path.dirname(p), exist_ok=True); \
   open(p,'a').write(json.dumps({'ts':datetime.datetime.now(datetime.timezone.utc).isoformat(),'decision_id':'<DECISION_ID>','suggested':'<SUGGESTED_MODEL>','user_choice':'<USER_CHOICE>'})+'\n')"
   ```

   Substitute the `decision_id` from the `<router-decision>` block, the
   suggested model the router proposed, and one of `use_suggested`,
   `use_opus`, or `stay_inline` for the user's pick (or
   `auto_inline_same_model` when the same-model short-circuit skipped
   the question). Never block on this — if the write fails, proceed
   with the work anyway.

### Branch C — `band == "none"` or no decision block

Proceed normally. Do the work yourself on the current session model.

### Branch D — plan mode (parent-authoritative)

If you are in plan mode (your context says so — don't rely on the
`plan_mode` field), invoke the `plan-with-models` skill and let it own the
plan structure. Do not delegate the _planning_ itself — planning is the
parent's job. `plan-with-models` then executes the plan as **parallel
waves** (independent steps run concurrently; see that skill's wave
procedure). This is where the user's "create the respective agents for
specific tasks" happens: each plan step becomes a `router-<model>` agent,
and disjoint-file steps in the same wave fire together.

### Branch E — `band == "auto"` (or "ask") **and** `fanout == true`

The prompt decomposes into independent subtasks (numbered list, "X and Y
and Z", "for each…"). Fan out instead of doing them serially:

1. **Decompose** the prompt into the smallest independent subtasks
   (the `fanout_hint` is the rough count). For each, name the files it will
   **write** (if any).
2. **Classify each subtask** to the cheapest sufficient model — most list
   items are `router-haiku` (a read, a one-file edit) or `router-sonnet`;
   reserve `router-opus` for the genuinely hard one. This is the
   token-efficiency win: small focused contexts at low tiers beat one big
   Opus context doing everything.
3. **Check non-interference** (same rule as `plan-with-models`, single
   source of truth):
   - Read-only subtasks → always safe to run together.
   - Writers → together only if their write-file sets are **disjoint**.
   - Overlapping writers → run in separate messages (serialize), or
     `isolation: "worktree"` + merge if they must be concurrent.
4. **Dispatch each conflict-free batch as ONE message** of multiple
   `Agent()` calls (the harness runs them concurrently). Prefix each
   `description` with `[grp:<id>]` (shared per batch) so outcome capture can
   measure the fan-out.
5. **Synthesize**: when the batch returns, integrate the results and report
   to the user in **1–2 sentences** total — not one summary per agent.
6. If `band == "ask"`, do Branch B's confirmation first, then fan out.

When **not** to fan out: the subtasks actually depend on each other
sequentially; or there's really just one task dressed up with conjunctions
("read the file **and** tell me what it does" is one task). When in doubt
on a borderline case, prefer a single dispatch.

## Outcome logging (mandatory)

Every `auto`-band decision must end as exactly ONE of:

- a real `router-*` dispatch — the PostToolUse hook logs `delegated`
  automatically; write nothing yourself — or
- a skip row you append, using ONLY this vocabulary:
  `skipped_trivial` | `same_model_inline` | `continuity_inline` |
  `worker_failed`.

```bash
echo "$(jq -nc --arg id "<decision_id>" --arg outcome "<OUTCOME>" --arg model "<model>" '{ts: (now|todate), decision_id: $id, outcome: $outcome, model: $model}')" >> ~/.claude/cache/router/audit.jsonl
```

- `ts` must be ISO-8601: `now|todate`, never bare `now` — an epoch float
  is invisible to the analyzers.
- Do not invent other outcome spellings (`inline`, `stayed_inline`,
  `inline_override`, …) — they pollute the stats.
- `skipped_trivial` is narrow: ONE read-only tool call answers the user
  with no synthesis (a single Read of a known path, one `git status`).
  Needs a second tool call or reasoning over the output? Dispatch.
- `continuity_inline` covers both the decision block's `continuity: true`
  flag and the "user is mid-iteration on this task" override — staying
  inline to preserve session context.
- If `jq` is missing, write the line with python; if the write fails,
  proceed with the work anyway.

## When to override the router

Trust the user's intent above the classifier:

- If the user explicitly said `do it yourself` / `don't delegate` /
  `stay on opus`, do that. The classifier is wrong in this case.
  Force-routing via `#model=opus|sonnet|haiku|fable` in the prompt
  is also honoured — the decision will have `band=auto, confidence=1.0`.
- If the user already started a task on the current model and is
  iterating (5+ messages deep on the same task), don't re-delegate —
  preserve session continuity. If the decision carries a continuity
  flag, prefer staying inline — the user is mid-iteration.
- If the dispatched subagent fails or returns garbage, retry once
  inline at the current model and report the failure.

## Failure modes to avoid

- **Don't paraphrase the agent's output.** Relay the substantive
  result, not a meta-summary of "I dispatched an agent and it did X."
- **Don't start work in parallel with the agent.** The whole point is
  letting the worker model do the task. Sitting idle while it runs is
  correct.
- **Don't delegate trivial single-tool calls** (one `Read` of a known
  path, one `git status`). The dispatch overhead exceeds the savings.
  Treat as `band == "none"` and log `skipped_trivial`.
- **Don't delegate when the user is mid-iteration on the parent.** The
  delegated agent lacks the conversation context.

## Retry / escalation policy

If a dispatched worker fails or returns an explicit escalation signal,
recover gracefully — don't surface the failure as a dead end.

### Escalation signals from workers

Each worker subagent ends its turn with one of these markers:

- `Done: …` — success; relay and stop.
- `Done with caveats: …` — success with a flagged concern; relay both.
- `Stopped: too complex for <model> tier. Reason: <why>. Suggest re-
dispatch to router-<higher>.` — explicit escalation.

When you see `Stopped:`, **don't ask the user**; just re-dispatch one
tier up (`router-haiku` → `router-sonnet` → `router-opus`). Pass the
worker's suggested refined prompt if it provided one; otherwise re-use
the original prompt prefixed with one line of context:
`Previous <model> attempt stopped: <reason>. Continue from there.`

### Implicit failure (tool error, empty result, garbage)

If the worker returned an error, an empty result, or output that
clearly doesn't address the task:

1. Log the failure: append a one-line JSON to
   `~/.claude/cache/router/audit.jsonl` with
   `outcome: "worker_failed"`, the original `decision_id`, and a
   short diagnostic.
2. Retry **exactly once** at the same tier with a clearer prompt
   (restate the user's request explicitly, even if the original was
   vague).
3. If the second attempt also fails, **escalate one tier up**.
4. If even Opus fails, fall back to handling the prompt inline on the
   current session model, and surface the failure to the user with the
   suggested manual next step.

Never silently swallow a worker failure. The user must know that
delegation happened and that it didn't pan out.

### Worker took too long

If a dispatched worker is taking unusually long (no result after 2-3
min for what should be a quick task), don't kill it — but do consider
that as a signal the tier may have been too low. After it returns,
note in the audit log whether the result quality matched the elapsed
time. Patterns of slow-and-mediocre at a low tier are a sign to bump
the project's `default_model` or add a `rules:` entry in
`.claude/router.json`.

## How to invoke the worker

Each tier maps to a pre-registered subagent type:

| Model  | subagent_type   | Use for                                                            |
| ------ | --------------- | ------------------------------------------------------------------ |
| haiku  | `router-haiku`  | Reads, lists, lookups, quick edits, classifications                             |
| sonnet | `router-sonnet` | Routine refactors, doc writing, spec edits, prototype iteration                 |
| opus   | `router-opus`   | Deep refactors, architecture, hard debugging, multi-file synthesis              |
| fable  | `router-fable`  | Frontier-stakes synthesis, irreversible high-blast-radius steps (manual/override-only) |

The dispatched agent inherits the parent's working directory and
project-level `CLAUDE.md` / `AGENTS.md` instructions.
