---
name: auto-model-routing
description: Use whenever an `AUTO-ROUTE` instruction with a `<router-decision>` block is present in injected context (emitted by the auto-router UserPromptSubmit hook). The hook has already decided the routed model is strictly cheaper than this session — your only job is to dispatch to the named `auto-model-router:router-<model>` worker with a cold-start brief and verify the result.
---

# auto-model-routing

You are running as a **router parent**. The `auto-router.py` UserPromptSubmit
hook classified the user's prompt and, when a cheaper model can do the work,
injected an `AUTO-ROUTE` instruction plus a `<router-decision>` block into your
context. This skill tells you how to honour it.

## What the hook already decided

The hook reads the parent session's own model off the transcript and only
injects when the routed tier is **strictly cheaper** than yours
(`haiku < sonnet < opus < fable`). A same-or-higher pick emits nothing, so you
never see a decision block you should ignore on cost grounds. There is no `ask`
band anymore — `band` is only `auto` (an AUTO-ROUTE block is present) or
`none` (no block). A short follow-up prompt stays inline silently (the hook
suppresses it), and a context-budget warning line rides along only at ≥200k
input tokens.

The injection looks like this:

```
[auto-router] sonnet/medium conf=0.82 parent=opus ctx=42k
AUTO-ROUTE: Agent(subagent_type="auto-model-router:router-sonnet") with a
cold-start brief (files, decisions so far); verify its result; on failure
re-dispatch one tier up. Skip only for a single read-only call.
<router-decision>{"decision_id":"r_...","model":"sonnet","effort":"medium","band":"auto","parent":"opus","ctx":42}</router-decision>
```

The `<router-decision>` block carries `decision_id`, `model`, `effort`, `band`
(always `auto` when present), `parent`, and `ctx` (input tokens ÷ 1000). The
prose line carries extra directives: a `fanout:` clause and a ≥200k budget line.

## Procedure

```
IN PLAN MODE?              → don't delegate; use plan-with-models — FIRST CHECK
AUTO-ROUTE + fanout clause → Branch E: decompose + one message of parallel dispatches
AUTO-ROUTE (no fanout)     → Branch A: dispatch to auto-model-router:router-<model>
no AUTO-ROUTE block        → Branch C: do the work yourself, log nothing
```

**Plan mode is parent-authoritative — check it FIRST.** If your context says
plan mode is active (a system reminder, or the hook's
`[auto-router] plan mode: use the plan-with-models skill` line), invoke the
`plan-with-models` skill and let it own the plan. Do not delegate the planning
itself — planning is the parent's job; `plan-with-models` then executes the
plan as parallel waves, one `auto-model-router:router-<model>` agent per step.

### Branch A — dispatch to the named worker

1. Dispatch via the `Agent` tool:
   - `subagent_type`: **always** the namespaced form
     `auto-model-router:router-<model>` (e.g. `auto-model-router:router-sonnet`).
     The bare un-namespaced form fails with "Agent type not found."
   - `description`: short (3-5 word) summary of the task.
   - `prompt`: the verbatim user request **plus a 3-5 line cold-start brief**.
     The worker starts cold with none of your context, so include:
     - files touched so far (paths, and what changed);
     - decisions and constraints already settled this session;
     - the user's verbatim request.
2. While the worker runs, don't duplicate work on the **same** subtask. (You
   may fan out across **independent** subtasks — that is Branch E.)
3. **On return, verify with ONE cheap check** — not a full re-review:
   - `git diff --stat` (or `git -C <dir> diff --stat`) to confirm the expected
     files changed, or
   - run the test/command the task named, or
   - one ranged `Read` of the specific edited region.
4. If the check shows the result is wrong or the worker returned
   `Stopped: …`, **re-dispatch one tier up** with the failure note appended
   (`haiku → sonnet → opus → fable`). See Escalation.
5. Relay the worker's result to the user in **1–2 sentences** (plus any direct
   artifact URLs). Do not paraphrase its entire output.

### Branch C — no AUTO-ROUTE block

No block means the hook decided routing buys nothing (same-or-higher tier, a
follow-up, an opt-out, or below the route floor). Just do the work yourself on
the current session model. Log nothing.

### Branch E — fanout

The `AUTO-ROUTE` line carries a `fanout:` clause: the prompt decomposes into
independent subtasks (a numbered list, "X and Y and Z", "for each…"). Fan out
instead of doing them serially:

1. **Decompose** into the smallest independent subtasks. For each, name the
   files it will **write** (if any).
2. **Classify each** to the cheapest sufficient model — most list items are
   `auto-model-router:router-haiku` (a read, a one-file edit) or
   `auto-model-router:router-sonnet`; reserve `auto-model-router:router-opus`
   for the genuinely hard one.
3. **Check non-interference** (same rule as `plan-with-models`):
   - Read-only subtasks → always safe together.
   - Writers → together only if their write-file sets are **disjoint**.
   - Overlapping writers → separate messages (serialize), or
     `isolation: "worktree"` + merge if they must be concurrent.
4. **Dispatch each conflict-free batch as ONE message** of multiple `Agent()`
   calls (the harness runs them concurrently). Prefix each `description` with
   `[grp:<id>]` (shared per batch) so outcome capture can measure the fan-out.
5. **Synthesize**: integrate the returns and report to the user in **1–2
   sentences** total — not one summary per agent.

When **not** to fan out: the subtasks depend on each other sequentially, or
it's really one task dressed up with conjunctions ("read the file **and** tell
me what it does" is one task). When in doubt, single dispatch.

## Escalation

Each worker ends with one of:

- `Done: …` (+ a `Files changed:` list) — success; verify, relay, stop.
- `Done with caveats: …` — success with a flagged concern; relay both.
- `Stopped: too complex for <model> tier. Reason: <why>. Suggest re-dispatch
to router-<higher>.` — explicit escalation.

On `Stopped:` **don't ask the user** — re-dispatch one tier up
(`haiku → sonnet → opus → fable`), reusing the worker's suggested prompt if it
gave one, else the original prompt prefixed with `Previous <model> attempt
stopped: <reason>. Continue from there.` If the verify check (Branch A step 3)
fails, treat it the same way: retry once at the same tier with a clearer
prompt, then escalate one tier up. If even the top tier fails, handle the
prompt inline and surface the failure with a suggested manual next step. Never
silently swallow a worker failure.

## Overriding the router

Trust the user's intent above the classifier. If they said `do it yourself` /
`don't delegate` / `stay on <model>`, do that. Force-routing via
`#model=opus|sonnet|haiku|fable` is honoured (the hook emits `band=auto,
confidence=1.0`, and dispatches even to a dearer model since only the human can
change the session's own model). If the user is mid-iteration on the current
model, prefer staying inline — the worker lacks that context.

## Outcome logging

The hooks handle it; you log nothing by hand. `post-agent-audit.py`
(PostToolUse) records every `auto-model-router:router-*` dispatch automatically
(outcome `delegated`, tokens, wall time, `decision_id`, `group_id`), and
`reconcile-outcomes.py` (Stop) reconciles inline turns. The routing loop
(`router_loop.py`, `/router-loop`) and `usage-report.py` (`/router-report`)
consume that audit — do not duplicate or hand-tune it here.

## Failure modes to avoid

- **Don't paraphrase the agent's output** — relay the substantive result, not
  "I dispatched an agent and it did X."
- **Don't start work in parallel with the agent** on the same subtask.
- **Don't skip the verify step** — one cheap check catches a bad dispatch
  before you relay it.
- **Always use the namespaced `auto-model-router:router-<model>` form.** Bare
  names fail with "Agent type not found."

## How to invoke the worker

| Model  | subagent_type                     | Use for                                                            |
| ------ | --------------------------------- | ------------------------------------------------------------------ |
| haiku  | `auto-model-router:router-haiku`  | Reads, lists, lookups, quick edits, classifications                |
| sonnet | `auto-model-router:router-sonnet` | Routine refactors, doc/spec writing, prototype iteration           |
| opus   | `auto-model-router:router-opus`   | Deep refactors, architecture, hard debugging, multi-file synthesis |
| fable  | `auto-model-router:router-fable`  | Frontier-stakes synthesis, irreversible steps (manual/override)    |

The dispatched agent inherits the parent's working directory and project-level
`CLAUDE.md` / `AGENTS.md`.
