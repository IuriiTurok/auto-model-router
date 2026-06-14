# Tooling & evals — auto-model-routing

Level-3 disclosure for the router's deterministic tooling. These scripts are
**not** invoked by the parent during routing — they run via slash commands, the
sil loop, or a PostToolUse hook. Paths are relative to this skill dir; the
executables live at the plugin root `tools/` and are wired by their current
path (do not move them).

## Cache surface (router's `~/.claude/cache/router/`)

| File | Writer | Readers |
| --- | --- | --- |
| `audit.jsonl` | parent (skip/failure rows) · `post-agent-audit.py` hook (`delegated` rows) | `router_loop.py`, `usage-report.py`, `analyze-audit.py`, `replay_kpi.py` |
| `overrides.jsonl` | parent (ask-band user choice, Branch B) | `router_loop.py`, `analyze-audit.py` |
| `signal.jsonl` | nightly Phase 5 (Bridge B) — advisory model-outcome rows | `router_loop.py` (rung-2 refit); parent reads it only as a fail-open soft tie-breaker (see SKILL.md → Decision schema → Advisory signal) |

`signal.jsonl` is **optional and advisory**: absent/empty/stale ⇒ the router
behaves exactly as on `audit.jsonl` + `overrides.jsonl` alone. It never changes
the band, the branch, plan-mode authority, or a user override.

## Scripts (`../../tools/`)

| Script | Trigger | Reads | Writes |
| --- | --- | --- | --- |
| `../../tools/router_loop.py` | `/router-loop` (sil rung-2, AUTO_CONFIG only) | `audit.jsonl`, `overrides.jsonl`, `signal.jsonl` | loop-private repo `~/.claude/cache/router/loop-config` (never a user repo, never pushes) — tunes `auto_threshold` |
| `../../tools/usage-report.py` | `/router-report` | `audit.jsonl` (+ Claude Code usage) | KPI report (stdout / `--json` / `--write`) |
| `../../tools/analyze-audit.py` | manual: `python3 tools/analyze-audit.py [--audit P] [--overrides P] [--days N]` | `audit.jsonl`, `overrides.jsonl` | re-runnable analysis to stdout |
| `../../tools/replay_kpi.py` | manual: `python3 tools/replay_kpi.py --auto 0.72 --ask 0.60 [--since ISO]` | `audit.jsonl` | counterfactual re-band of past decisions to stdout (offline eval) |

## Contract notes

- The sil ledger contract is frozen: `router_loop.py` stays at rung
  `AUTO_CONFIG` (config-level auto, never `PRODUCTION` / `AUTO_CODE`).
- Adding `signal.jsonl` is *input enrichment*, not a contract change — it is
  read-only advisory weight on classification, subordinate to the decision
  tree.
- Exact dispatch attribution (threading `decision_id` onto the hook's
  `delegated` rows) is a follow-up for the `post-agent-audit.py` hook owner,
  out of scope for the skill body.
