# Changelog

All notable changes to auto-model-router. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versioning is semver-ish.

## [0.4.0] — 2026-06-01

### Added

- **`/router-loop` + `tools/router_loop.py`** — a self-improving config loop
  (the Karpathy autoresearch pattern) on the shared `sil` kernel. It proposes
  ONE `auto_threshold` step, gates it offline, applies it to a **loop-private
  git repo** (`~/.claude/cache/router/loop-config`, branch `router-loop/auto`,
  never pushed), and later confirms-or-reverts against realized KPIs. Rung-2
  (config-class auto-apply + auto-rollback via `git revert`); experiments are
  serialized; a 7-day anti-oscillation guard prevents thrash. Gates:
  `thresholds_valid` (auto within `[floor, ceil]` and `>` ask), `hook_healthy`
  (fixtures pass), `distribution_sane` (candidate re-band vs a
  current-threshold re-band — apples-to-apples). The only hard KEEP-veto is
  **quality** (router correction rate must not exceed inline by more than ε),
  per `GOALS.md` §2. A separate entry point from `/router-report`, whose
  "offer, don't apply" contract is untouched.
- **`tools/replay_kpi.py`** — offline counterfactual: re-bands recorded
  `audit.jsonl` confidences under candidate thresholds via the pure
  `band_for()` (no re-classification), identity-checked against the live hook.
  The feasibility gate the loop reasons over.
- **`tests/test_replay_kpi.py`** + **`tests/test_router_loop.py`** — wired into
  `tests/run.sh`.

### Changed

- Default `AUTO_THRESHOLD` lowered **0.90 → 0.75** so the modal 0.75-confidence
  heuristic band auto-routes instead of prompting (the `ask` band was
  structurally busy — ~44% of decisions, clustered at conf 0.75). Opus picks
  keep an asymmetric **0.90 floor** in `band_for()` — mis-routing to Opus is
  the costly failure. Three standard-tier band fixtures flipped accordingly.
- `hooks/auto-router.py` gained a `load_loop_config()` fallback so the loop can
  tune the global `auto_threshold` default; per-project `.claude/router.json`
  overrides still win.

## [0.3.0] — 2026-05-31

### Added

- **`GOALS.md`** — three north-star KPIs (cost efficiency, quality via
  user-correction rate, time-to-results) that the router is measured against.
- **`/router-report [7d|1d|yesterday]`** + `tools/usage-report.py` — a
  deterministic usage report over the audit log + session transcripts (joined
  by `prompt_sha`). Scores all three KPIs, writes lessons learned, and offers
  data-derived routing tweaks (offer-not-apply). `--llm-judge` opt-in sharpens
  correction detection via a Haiku pass; `--json` for machine output.
- **`hooks/pricing.py`** — per-MTok price table powering the cost-saved (vs
  all-Opus counterfactual) estimate.
- **`tests/test_usage_report.py`** — KPI scoring unit tests.

### Changed

- Manual-install docs (`INSTALL.md`, `README.md`) now document all **three**
  hooks (UserPromptSubmit + PreToolUse[Agent] + PostToolUse[Agent]) and all
  command symlinks, with an upgrade-drift caveat.
- Author/owner/copyright switched to GitHub handle; `PATYX` project reference
  removed from `router-opus.md` (project bias belongs in `.claude/router.json`).

### Fixed

- `CC_ROUTER_CACHE_DIR` override threaded through all hooks + the analyzer so
  the test suite never pollutes the production audit log.

## [0.2.0] — 2026-05-31

### Added

- **Parallel wave execution.** `plan-with-models` owns a wave executor: a
  dependency DAG (`Depends on:`) → waves → conflict-free batches dispatched as
  concurrent `Agent()` calls. Non-interference by disjoint write-`Files:`
  (read-only always safe; overlapping writers serialize or worktree-isolate).
  Algorithm in `hooks/waves.py`, locked by `tests/test_waves.py`.
- **Branch E fan-out** in `auto-model-routing` for multi-part prompts; hook
  emits a `fanout` signal.
- Outcome capture: `hooks/pre-agent-mark.py` (PreToolUse) + extended
  `post-agent-audit.py` record `wall_ms`, token usage, escalation, `group_id`;
  `tools/analyze-audit.py` infers parallel batches + wall-clock saved.

### Changed

- Plan mode is **parent-authoritative** (the hook cannot detect it; its
  `plan_mode` field is best-effort only).
- `router-opus` model prose → Opus 4.8 (alias auto-resolves to latest).

## [0.1.0] — 2026-05-31

### Added / Changed

- Independent **effort scorer** decoupled from tier; effort/tier disagreement
  drops a decision into the `ask` band.
- Auto-threshold raised to **0.90** (ask 0.60).
- **Haiku restored**: `project_default` became a bias (not a hard override);
  no-match fallback flipped to cheap-and-confirm (haiku/ask).
- Outcome-capture audit groundwork; `CLASSIFIER_VERSION` cache invalidation;
  golden-fixture test harness (`tests/run.sh`) + reusable audit analyzer.
