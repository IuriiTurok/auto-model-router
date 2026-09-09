# Evals — auto-model-routing

Rubric criterion 9: pass/fail signal for skill behavior.
Each case lists a synthetic input, the expected action, and the PASS/FAIL assertion.
Run manually or via `replay_kpi.py` (offline counterfactual re-band).

Two bands only — `auto` (an `AUTO-ROUTE` block is present) or `none` (no block,
hook stayed silent). There is no `ask` band, no `AskUserQuestion` step, and no
`overrides.jsonl`. The hook is downhill-only: it reads the parent session's own
model off the transcript and injects an `AUTO-ROUTE` block ONLY when the routed
model is strictly cheaper (`haiku < sonnet < opus < fable`). A same-or-higher
pick is audited by the hook itself and emits nothing — the parent never sees a
decision block it should second-guess on cost grounds.

---

## Case 1 — Band `auto` → delegate, hook logs `delegated` automatically

**Input** — injected context

```
[auto-router] sonnet/medium conf=0.88 parent=opus ctx=42k
AUTO-ROUTE: Agent(subagent_type="auto-model-router:router-sonnet") with a
cold-start brief (files, decisions so far); verify its result; on failure
re-dispatch one tier up. Skip only for a single read-only call.
<router-decision>{"decision_id":"r_eval001","model":"sonnet","effort":"medium","band":"auto","parent":"opus","ctx":42}</router-decision>
```

Prompt: "Refactor the `parse_config` function to handle missing keys gracefully."

**Expected action**
Branch A: dispatch via `Agent(subagent_type="auto-model-router:router-sonnet", ...)` with a
cold-start brief; verify with one cheap check; relay the result in 1-2 sentences.

**PASS** — `router-sonnet` Agent call is made using the namespaced
`auto-model-router:router-sonnet` form; `post-agent-audit.py` (PostToolUse) writes the
`delegated` row with `decision_id == "r_eval001"` — the parent writes NO row of its own;
parent emits a 1-2 sentence relay (not a verbatim copy of the worker's full output).

**FAIL** — parent does the work inline without dispatching; OR parent dispatches using the
bare `router-sonnet` form; OR parent hand-writes its own row to `audit.jsonl`; OR parent
paraphrases the user's request rather than relaying the worker's result.

---

## Case 2 — Downhill dispatch → decision block only carries what changes behaviour

**Input** — injected context

```
[auto-router] haiku/low conf=0.90 parent=opus ctx=12k
AUTO-ROUTE: Agent(subagent_type="auto-model-router:router-haiku") with a
cold-start brief (files, decisions so far); verify its result; on failure
re-dispatch one tier up. Skip only for a single read-only call.
<router-decision>{"decision_id":"r_eval002","model":"haiku","effort":"low","band":"auto","parent":"opus","ctx":12}</router-decision>
```

Session is running on an Opus-class parent model; the hook classified the prompt as a
trivial lookup and routed it to `haiku` — strictly cheaper than the parent, so the block
was injected (the downhill-only gate).
Prompt: "List the files changed in the last 7 days."

**Expected action**
Branch A: dispatch to `auto-model-router:router-haiku`. The `<router-decision>` block
carries only `decision_id`, `model`, `effort`, `band`, `parent`, `ctx` — no `tier`,
`source`, `reason`, or `thresholds` (those stay in the hook's own audit row, not the
parent's context).

**PASS** — `Agent(subagent_type="auto-model-router:router-haiku", ...)` is called;
`post-agent-audit.py` writes exactly one `delegated` row with `decision_id == "r_eval002"`
and `model_actual` resolving to a haiku-class model; the parent's relay does not
re-derive or restate `parent`/`ctx` as if they were new information — they only gate the
decision the hook already made.

**FAIL** — parent dispatches to a model other than `haiku`; OR parent treats the presence
of `parent`/`ctx` in the block as something it must independently verify or re-classify;
OR parent skips dispatch because "haiku seems too cheap for this."

---

## Case 3 — No `AUTO-ROUTE` block (same-or-higher, silent) → Branch C, work inline

**Input** — injected context: **none**. The `UserPromptSubmit` hook classified the
prompt as `opus`, but the parent session is already running an Opus-class model — same
rank, so the downhill-only gate suppressed the injection. The hook still wrote its own
audit row with `outcome: "silent"` and `outcome_hint: "same_or_higher_inline"`; the parent
never sees any of that.
Prompt: "Explain the tradeoffs in the current caching strategy."

**Expected action**
Branch C: no block means the hook decided routing buys nothing (same-or-higher tier here;
could equally be a follow-up, an opt-out, or a sub-route-floor confidence). Parent does the
work itself on the current session model and logs nothing by hand — the hook already
recorded the decision.

**PASS** — No `Agent` dispatch is made; the parent writes NO row to `audit.jsonl` or any
other log; the user gets the answer directly from the parent.

**FAIL** — Parent dispatches to `router-opus` anyway (defeats the downhill-only gate); OR
parent hand-writes an `audit.jsonl` row to "make sure it's logged" (outcome logging is the
hooks' job — see SKILL.md → Outcome logging); OR parent stalls waiting for a decision block
that was never going to arrive.
