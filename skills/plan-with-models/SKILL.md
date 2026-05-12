---
name: plan-with-models
description: Use whenever writing an implementation plan in plan mode (or any time the user asks for a step-by-step plan). Requires every step to carry `Model:` and `Effort:` tags so the executor can dispatch each step to the right model via Agent(subagent_type="router-<model>"). Heterogeneous plans (cheap steps on Haiku/Sonnet, deep steps on Opus) drop quota burn ~30% without losing quality.
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

| Step looks like… | Model |
|---|---|
| Read a file. List things. Run `git status`/`gh pr list`. Identify a path. | **haiku** |
| Edit one file. Rename a symbol. Update a single doc. Run a known build/test. | **haiku** |
| Refactor confined to 1–3 files. Write a spec or PR description. Iterate prototype HTML/CSS. Apply a documented migration. | **sonnet** |
| Multi-file refactor (4+ files). Architectural decision. Hard debugging. New module design. CAD/firmware geometry. Cross-cutting performance work. | **opus** |
| Verification (run tests, lint, type-check, smoke-test UI). | **haiku** if mechanical; **sonnet** if interpretation needed. |
| Final review / sanity check of the whole change. | **sonnet** or **opus** depending on stakes. |

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

## How the executor consumes the plan

When the plan is later run (e.g. via `superpowers:executing-plans` or
`/route` on a saved plan), each step is dispatched as:

```python
Agent(
    subagent_type=f"router-{step['model']}",
    description=step['title'],
    prompt=f"{step['action']}\n\nFiles: {step['files']}\nVerify: {step['verify']}"
)
```

Steps with `Depends on:` are awaited; steps with `Parallel with:` are
dispatched in the same tool-use block.

## When to skip per-step tagging

- **One-step plans.** If the plan is genuinely a single action, tags are
  overhead. Just describe the step and pick the model directly.
- **Exploratory plans.** When the plan is more "let's see what we find"
  than "here's what to do," skip the dispatch model — the work has to
  stay on the parent.
- **User explicitly opted out** with `#noshift` or said "don't
  delegate."

## Verification

After writing the plan, scan it: every numbered step must have a
`Model:` line. If any step is missing one, fix it before exiting plan
mode. This is a hard requirement for the executor to function.
