# Tooling & evals — auto-model-routing

Level-3 disclosure for the router's deterministic tooling. These scripts are
**not** invoked by the parent during routing — they run via slash commands, the
sil loop, or a hook. Paths are relative to this skill dir; the executables live
at the plugin root `tools/` and are wired by their current path (do not move
them).

## Cache surface (router's `~/.claude/cache/router/`)

| File | Writer | Readers |
| --- | --- | --- |
| `audit.jsonl` | `auto-router.py` (every classified decision — injected or silent) · `post-agent-audit.py` hook (`delegated` rows) · `reconcile-outcomes.py` (Stop hook, backstops unattributed inline turns) | `router_loop.py`, `usage-report.py`, `analyze-audit.py`, `replay_kpi.py` |
| `signal.jsonl` | nightly Phase 5 (Bridge B, lives in `claude-os`, not this plugin) — advisory model-outcome rows | `router_loop.py` only, when the opt-in `/router-loop` is run — its **only** consumer. The parent never reads it, and nothing in the live routing path reads it live. |

There is no `overrides.jsonl`. The two-band contract (`auto`/`none`) has no
`ask` band and no `AskUserQuestion` step to log a user choice for. Every
decision — injected or silent — is a single `audit.jsonl` row; the hooks write
all of it, so the parent never hand-logs anything (see SKILL.md → Outcome
logging).

`signal.jsonl` is **optional and advisory**: absent/empty/stale ⇒
`router_loop.py` behaves exactly as on `audit.jsonl` alone. It never changes
the band, the branch, plan-mode authority, or an explicit `#model=` override.

## Scripts (`../../tools/`)

| Script | Trigger | Reads | Writes |
| --- | --- | --- | --- |
| `../../tools/router_loop.py` | `/router-loop` (sil rung-2, AUTO_CONFIG only; opt-in) | `audit.jsonl`, `signal.jsonl` | loop-private repo `~/.claude/cache/router/loop-config` (never a user repo, never pushes) — tunes `auto_threshold` |
| `../../tools/usage-report.py` | `/router-report` | `audit.jsonl` (+ Claude Code usage) | KPI report (stdout / `--json` / `--write`) |
| `../../tools/analyze-audit.py` | manual: `python3 tools/analyze-audit.py [--audit P] [--overrides P] [--days N]` | `audit.jsonl` (`--overrides` is a vestigial flag — `overrides.jsonl` is never written under the two-band contract, so it's always empty/absent) | re-runnable analysis to stdout |
| `../../tools/replay_kpi.py` | manual: `python3 tools/replay_kpi.py --auto 0.72 --ask 0.60 [--since ISO]` | `audit.jsonl` | counterfactual re-band of past decisions to stdout (offline eval), via the pure `band_for()` — `--ask` is accepted for signature compatibility and ignored |

## Contract notes

- The sil ledger contract is frozen: `router_loop.py` stays at rung
  `AUTO_CONFIG` (config-level auto, never `PRODUCTION` / `AUTO_CODE`).
- `signal.jsonl` is *input enrichment* for the opt-in loop only, not a
  contract change — it is read-only advisory weight on `router_loop.py`'s
  threshold proposals, subordinate to the decision tree, and irrelevant to
  every other consumer.
- Agent dispatch is always the namespaced `auto-model-router:router-<model>`
  form. A bare `router-<model>` name fails with "Agent type not found" — see
  SKILL.md → Failure modes to avoid.
- Exact dispatch attribution (threading `decision_id` onto the hook's
  `delegated` rows) is a follow-up for the `post-agent-audit.py` hook owner,
  out of scope for the skill body.
