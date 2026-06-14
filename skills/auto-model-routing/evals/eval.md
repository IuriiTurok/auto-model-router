# Evals — auto-model-routing

Rubric criterion 9: pass/fail signal for skill behavior.
Each case lists a synthetic input, the expected action, and the PASS/FAIL assertion.
Run manually or via `replay_kpi.py` (offline counterfactual re-band).

---

## Case 1 — Band `auto`, high confidence → delegate, no hand-log

**Input**
```json
{
  "band": "auto",
  "model": "sonnet",
  "confidence": 0.88,
  "tier": "standard",
  "decision_id": "r_eval001",
  "fanout": false,
  "continuity": false,
  "plan_mode": false
}
```
Prompt: "Refactor the `parse_config` function to handle missing keys gracefully."

**Expected action**
Dispatch via `Agent(subagent_type="router-sonnet", ...)`.
The `post-agent-audit.py` hook logs `delegated`; the parent writes NO row to `audit.jsonl`.

**PASS** — `router-sonnet` Agent call is made; `audit.jsonl` contains a `delegated` row with `decision_id == "r_eval001"`; parent emits a 1-2 sentence relay (not a verbatim copy of the worker's full output).

**FAIL** — parent does the work inline without dispatching; OR parent dispatches AND also appends its own row to `audit.jsonl`; OR parent paraphrases the user's request rather than relaying the worker's result.

---

## Case 2 — Same-model short-circuit → stay inline, log `same_model_inline`

**Input**
```json
{
  "band": "auto",
  "model": "opus",
  "confidence": 0.81,
  "tier": "complex",
  "decision_id": "r_eval002",
  "fanout": false,
  "continuity": false,
  "plan_mode": false
}
```
Session is already running on an Opus-class model.
Prompt: "Explain the tradeoffs in the current caching strategy."

**Expected action**
Same-model short-circuit fires (second check in Procedure). Parent handles the prompt inline on the current session model.

**PASS** — No `Agent` dispatch is made; `audit.jsonl` receives exactly one row with `outcome == "same_model_inline"` and `decision_id == "r_eval002"`.

**FAIL** — Parent dispatches to `router-opus`; OR parent stays inline but writes no row (silent-inline drift); OR parent writes a row with an outcome string other than `same_model_inline`.

---

## Case 3 — Band `ask` → confirmation question shown, choice logged before work

**Input**
```json
{
  "band": "ask",
  "model": "haiku",
  "confidence": 0.68,
  "tier": "trivial",
  "decision_id": "r_eval003",
  "fanout": false,
  "continuity": false,
  "plan_mode": false
}
```
Prompt: "List all files modified in the last 7 days."

**Expected action**
`AskUserQuestion` with header `Model choice` and three options (Use haiku, Use Opus, Stay on current). After user picks "Stay on current": append one JSON row to `overrides.jsonl` with `user_choice == "stay_inline"` and `decision_id == "r_eval003"` **before** doing the work.

**PASS** — `AskUserQuestion` is called with exactly three options; `overrides.jsonl` row is written before the inline work begins; `overrides.jsonl` row contains correct `decision_id` and a valid `user_choice` value (`use_suggested` | `use_opus` | `stay_inline`).

**FAIL** — Parent skips the question and proceeds inline (unless same-model short-circuit applies); OR work begins before `overrides.jsonl` is written; OR row is written with an invalid `user_choice` value; OR `AskUserQuestion` offers fewer or more options than the three specified.
