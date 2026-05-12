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
  "model": "haiku" | "sonnet" | "opus",
  "effort": "low" | "medium" | "high" | "xhigh",
  "tier": "trivial" | "standard" | "complex" | "deep" | "override",
  "confidence": 0.0-1.0,
  "reason": "one-sentence justification",
  "source": "heuristic" | "haiku" | "default" | "override",
  "plan_mode": true | false,
  "decision_id": "r_xxxxxxxxxx",
  "thresholds": {"auto": 0.80, "ask": 0.50}
}
```

## Procedure

```
band == "auto" → DELEGATE
band == "ask"  → CONFIRM, then DELEGATE
band == "none" → IGNORE (do the work yourself)
plan_mode == true → don't delegate; invoke `plan-with-models` skill instead
```

### Branch A — `band == "auto"`

1. Dispatch the user's task via the `Agent` tool with:
   - `subagent_type: "router-<model>"` (e.g. `router-sonnet`)
   - `description`: short (3-5 word) summary of the task
   - `prompt`: the user's verbatim request, plus any context they
     supplied in this turn (file paths they referenced, follow-up
     details from earlier turns, etc.). Frame it so the agent can act
     cold.
2. While the agent runs, do not start parallel work on the same task.
3. When it returns, relay its result to the user in **1–2 sentences**
   (plus any direct artifact URLs). Do not paraphrase its entire output.
4. Append an outcome line to the audit log:

   ```bash
   echo "$(jq -nc --arg id "<decision_id>" --arg outcome "delegated" --arg model "<model>" '{ts: now, decision_id: $id, outcome: $outcome, model: $model}')" >> ~/.claude/cache/router/audit.jsonl
   ```

   (Use the `decision_id` from the `<router-decision>` block. If `jq`
   isn't available, write a one-line JSON with python or skip silently.)

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
3. Audit-log the outcome with `outcome: "user_<answer>"`.

### Branch C — `band == "none"` or no decision block

Proceed normally. Do the work yourself on the current session model.

### Branch D — `plan_mode == true`

Invoke the `plan-with-models` skill and let it own the plan structure.
Do not delegate the *planning* itself — planning is the parent's job;
heterogeneous *execution* happens when the plan is later run.

## When to override the router

Trust the user's intent above the classifier:

- If the user explicitly said `do it yourself` / `don't delegate` /
  `stay on opus`, do that. The classifier is wrong in this case.
- If the user already started a task on the current model and is
  iterating (5+ messages deep on the same task), don't re-delegate —
  preserve session continuity.
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
  Treat as `band == "none"`.
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

| Model | subagent_type | Use for |
|---|---|---|
| haiku | `router-haiku` | Reads, lists, lookups, quick edits, classifications |
| sonnet | `router-sonnet` | Routine refactors, doc writing, spec edits, prototype iteration |
| opus | `router-opus` | Deep refactors, architecture, hard debugging, multi-file synthesis |

The dispatched agent inherits the parent's working directory and
project-level `CLAUDE.md` / `AGENTS.md` instructions.
