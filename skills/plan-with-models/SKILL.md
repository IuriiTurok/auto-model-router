---
name: plan-with-models
description: Use whenever writing an implementation plan in plan mode (or any time the user asks for a step-by-step plan). Requires every step to carry `Model:`, `Effort:`, and `Files:` tags so the executor can dispatch each step to the right model via Agent(subagent_type="router-<model>") AND run independent steps in parallel waves without file conflicts. Heterogeneous plans (cheap steps on Haiku/Sonnet, deep steps on Opus) drop quota burn ~30%; parallel waves cut wall-clock on top.
---

# plan-with-models

When writing a plan in plan mode, **every step MUST declare a model and
effort tier**. The executor uses these tags to dispatch each step to a
matching worker subagent. Without them, the plan falls back to a single
model and you lose the savings.

## Per-step template

```markdown
### Step N — <Imperative action phrase>

Model: haiku | sonnet | opus
Effort: low | medium | high | xhigh
Files: <comma-separated paths or "none">
Action: <what to do — 1-3 sentences>
Verify: <how to confirm the step succeeded>
```

Optional fields when relevant:

- `Depends on: Step N` (when a step needs the result of an earlier one).
- `Parallel with: Step N` (when two steps are independent and can run
  concurrently).
- `Skill: <skill-name>` (when a specific skill applies — invoked inside
  the dispatched agent).

## Choosing the model per step

| Step looks like…                                                                                                                                  | Model                                                         |
| ------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| Read a file. List things. Run `git status`/`gh pr list`. Identify a path.                                                                         | **haiku**                                                     |
| Edit one file. Rename a symbol. Update a single doc. Run a known build/test.                                                                      | **haiku**                                                     |
| Refactor confined to 1–3 files. Write a spec or PR description. Iterate prototype HTML/CSS. Apply a documented migration.                         | **sonnet**                                                    |
| Multi-file refactor (4+ files). Architectural decision. Hard debugging. New module design. CAD/firmware geometry. Cross-cutting performance work. | **opus**                                                      |
| Verification (run tests, lint, type-check, smoke-test UI).                                                                                        | **haiku** if mechanical; **sonnet** if interpretation needed. |
| Final review / sanity check of the whole change.                                                                                                  | **sonnet** or **opus** depending on stakes.                   |

## Effort tiers

- `low` — should take seconds, single tool call. Haiku territory.
- `medium` — minutes, multiple tool calls but no deep thinking.
- `high` — substantial work; multi-file edits, real reasoning.
- `xhigh` — open-ended investigation, architectural synthesis.

## Sample plan structure

```markdown
## Implementation Plan

### Step 1 — Inventory current usage

Model: haiku
Effort: low
Files: none
Action: Grep for `oldFunction` across the repo; list every file and line number.
Verify: List has at least 1 entry (confirms the function exists).

### Step 2 — Replace call sites

Model: sonnet
Effort: medium
Files: <files from step 1>
Action: Replace `oldFunction(x)` with `newFunction(x, options)` in each
location. Preserve indentation and surrounding code style.
Verify: Type-check passes (`yarn type-check`).

### Step 3 — Refactor the implementation

Model: opus
Effort: high
Files: src/lib/myModule.ts, src/lib/myModule.test.ts
Action: Rewrite `newFunction` to support the new options. Maintain
backwards compatibility for callers passing no options.
Verify: All tests pass (`yarn test src/lib/myModule.test.ts`).

### Step 4 — Add changelog entry

Model: haiku
Effort: low
Files: CHANGELOG.md
Action: Add a one-line entry under the `## Unreleased` section.
Verify: `git diff CHANGELOG.md` shows the new line.
```

## How the executor consumes the plan (parallel by waves)

**This skill OWNS execution.** Do not hand the plan to
`subagent-driven-development` — that skill runs steps strictly sequentially
and forbids parallel dispatch. The whole point here is to run independent
steps concurrently, so follow the wave procedure below instead.

### Step 1 — Build the dependency DAG → waves

Read every step's `Depends on:` field. A **wave** is the set of steps whose
dependencies are all already complete:

- **Wave 0** = steps with no `Depends on:`.
- **Wave N** = steps all of whose dependencies finished in waves `< N`.

Run waves strictly in order. Within a wave, all steps are eligible to run
**at the same time** — subject to the non-interference rule next.

### Step 2 — Split each wave into conflict-free batches

Two steps can run concurrently **unless they would write the same file.**
Use each step's `Files:` field (the paths it WRITES):

1. **Read-only step** (`Files: none`) — safe to run alongside anything.
2. **Two writers** — safe together **iff** their `Files:` sets are
   **disjoint**.
3. **Writers sharing a path** — must NOT run together. Put them in separate
   batches (run back-to-back within the wave). Only when they _genuinely_
   must run at the same time, give each `isolation: "worktree"` and merge
   after (see "Overlapping writes" below).
4. **Ambiguous** (`Files: none` but the `Action:` clearly edits something) —
   treat it as a writer over the paths named in the action; when in doubt,
   serialize.

Greedy batching: walk the wave's steps in order; place each into the first
batch where it conflicts with no member; otherwise open a new batch. (The
canonical algorithm lives in `hooks/waves.py::plan_execution` — same rules,
locked by `tests/test_waves.py`.)

### Step 3 — Dispatch each batch as ONE message of concurrent agents

For a batch of independent steps, emit all their `Agent()` calls in a
**single assistant message** so the harness runs them concurrently:

```python
# one batch = one message, multiple Agent() calls
Agent(subagent_type=f"router-{step['model']}", description=f"[grp:{batch_id}] {step['title']}",
      prompt=f"{step['action']}\n\nFiles you may touch: {step['files']}\n"
             f"Do NOT edit anything outside those files.\nVerify: {step['verify']}")
# … one Agent() call per step in the batch, all in THIS message …
```

- Pass the step's model as `router-<model>` (cheapest sufficient model — the
  table above).
- Prefix `description` with `[grp:<batch_id>]` (any short id) so the
  outcome-capture hook can correlate the batch — this powers the
  "wall-clock saved" metric in `/route-status`.
- Constrain each agent to its `Files:` set in the prompt — defence in depth
  on top of the disjoint-set guarantee.

### Step 4 — Synthesize + verify after each wave

When the batch returns, before starting the next wave:

1. Read each agent's summary; check the results don't contradict each other.
2. Run the wave's `Verify:` checks (tests/type-check/lint).
3. If a step failed or escalated (`Stopped: …`), re-dispatch per the
   `auto-model-routing` retry policy before moving on.

Then proceed to the next wave. Dependent steps now have their inputs ready.

### Overlapping writes (worktree fallback)

If two steps in the same wave MUST write overlapping files concurrently
(rare — usually you can just serialize them), isolate each in its own git
worktree via the Agent `isolation: "worktree"` option, then merge the
branches after the wave. Prefer serialization first: a second batch costs
one extra round-trip; worktrees cost setup + a merge. See
`superpowers:using-git-worktrees`.

### Large-plan path (Workflow)

For a big plan (more than ~12 steps, or deep fan-out with many waves), the
multiple-`Agent()`-calls approach gets unwieldy. Offer to generate a
`Workflow` script instead — its `pipeline()` / `parallel()` primitives and
per-agent `isolation: "worktree"` are built for this scale. The Workflow
tool requires explicit user opt-in, so **propose it; don't auto-launch it.**

## When to skip per-step tagging

- **One-step plans.** If the plan is genuinely a single action, tags are
  overhead. Just describe the step and pick the model directly.
- **Exploratory plans.** When the plan is more "let's see what we find"
  than "here's what to do," skip the dispatch model — the work has to
  stay on the parent.
- **User explicitly opted out** with `#noshift` or said "don't
  delegate."

## Verification

After writing the plan, scan it:

- Every numbered step has a `Model:` line.
- Every step that **edits files** has a `Files:` line listing them — the
  parallel executor needs it to prove batches are conflict-free. A step
  with no `Files:` is treated as read-only and may be parallelised freely,
  so an unlabelled writer is a correctness bug, not just a style nit.
- `Depends on:` references point at real earlier steps (no cycles).

Fix any gaps before exiting plan mode. These are hard requirements for the
wave executor to run steps in parallel safely.
