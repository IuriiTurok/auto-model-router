---
description: Run the router's autonomous self-improvement loop (rung-2, config-only). Proposes one auto_threshold tweak, gates it offline, applies it to a loop-private git repo, and later confirms or auto-reverts against realized KPIs. Separate from /router-report (which stays offer-don't-apply). Usage `/router-loop [propose|confirm] [--dry-run] [--force-auto X]`.
---

# /router-loop

The closed-loop counterpart to `/router-report`. Where `/router-report` measures
and *offers* tweaks for you to apply by hand, `/router-loop` runs the autonomous
PROPOSE → COMMIT → EXECUTE → EVALUATE → DECIDE → LOG cycle on the Karpathy
autoresearch pattern, for **config-class artifacts only** (the global
`auto_threshold`).

It is safe by construction:

- It edits only a **loop-private git repo** at `~/.claude/cache/router/loop-config/`
  — never a user project repo, never your working branch. Commits land on
  `router-loop/auto` and are **never pushed**.
- Every change is gated offline (fixtures healthy + projected band distribution
  sane) before it is applied, and confirmed against realized KPIs over a live
  window before it is kept. Anything that fails is auto-reverted.
- Quality is a **hard veto** (per `GOALS.md` §2): a candidate is never kept if the
  router cohort's correction rate would exceed the inline cohort's by more than
  the configured margin.
- `/router-report`'s "offer, don't apply" contract is untouched.

See `GOALS.md` for the fitness definitions and the meta-plan for the design.

## Step 1 — Resolve the subcommand

Parse the argument (default `propose`):

- `propose` / nothing → run one PROPOSE iteration.
- `confirm` → resolve any pending experiment whose live window has elapsed.
- `--dry-run` → run at the `DRY_RUN` rung (simulate + log, never apply).
- `--force-auto X` → force a specific `auto_threshold` candidate (testing /
  manual override; still fully gated).

## Step 2 — Run the loop

```bash
python3 ~/.claude/plugins/auto-model-router/tools/router_loop.py propose [--dry-run] [--force-auto X]
# or
python3 ~/.claude/plugins/auto-model-router/tools/router_loop.py confirm
```

The tool prints a compact JSON summary of the iteration: the `decision`
(KEEP / REVERT / ASK), the `change`, the `gates` that ran, whether it was
`reverted`, and `pending_until` if an experiment was applied and is now awaiting
live-window confirmation.

## Step 3 — Relay the outcome

Summarize for the user in a few lines:

- **Proposed + applied (pending):** state the knob change and when confirmation
  is due (`pending_until`). Remind them `confirm` resolves it (or that the
  scheduled run will).
- **REVERT:** state which gate failed (e.g. `distribution_sane` for an insane
  threshold, or `quality` at confirm) and that nothing harmful persists.
- **ASK:** state why a human decision is needed (anti-oscillation, or a rung gate
  on a non-config artifact) — do not auto-apply.
- **No proposal:** the loop found no sensible move (or an experiment is still
  pending). Nothing to do.

Point to the ledger (`~/.claude/cache/router/applied-tweaks.jsonl`) for the full
history; never paste the whole file.

## Notes

- Experiments are **serialized**: `propose` refuses while an experiment is
  pending, so the applied change is always the one `confirm` reverts.
- The loop tunes the **global** `auto_threshold` default; per-project
  `.claude/router.json` overrides still win and are never touched.
- For unattended operation, schedule `confirm` (e.g. daily) and `propose` (e.g.
  every few days) — but review the ledger periodically.
