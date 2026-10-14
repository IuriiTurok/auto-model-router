# Changelog

All notable changes to auto-model-router. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versioning is semver-ish.

## [0.9.0] — 2026-10-14

Data-driven slimming for the 5.5 lineup (main session on Opus 5.5 at medium
effort, 600K auto-compact window). A data audit found the hook injecting mostly
noise: ~51% of the prompts it classified were harness events, not typed prompts;
55% of injections came from `default_model` alone and were rarely followed; none
of 81 haiku injections was ever dispatched (43 of 73 hits were "status?"
continuity prompts a cold worker cannot answer); and the injection averaged
~1,042 chars per prompt. The cost accounting also had stale prices and three
measurement bugs. This release stops the noise and fixes the accounting.

### Changed

- **Harness events are skipped.** A prompt starting with `<task-notification`,
  `<scheduled-task`, or `[SYSTEM NOTIFICATION` (after whitespace or inside a
  `<system-reminder>` wrapper) is not classified, injected, or audited.
- **`default_model` is audited, never injected.** A pick that came only from the
  project default (tier `project_default`) writes its decision row with
  `band: none` and `outcome_hint: project_default_inline`, so the dashboard feed
  keeps working, but no `AUTO-ROUTE` is emitted.
- **Only an inline `#model=` tag may route up.** `router.json` rules used to share
  that exemption (`source: project_config`); they now pass the same
  strictly-cheaper-than-the-parent gate as the heuristics. A rule to a dearer
  or equal model is audited `same_or_higher_inline` and stays silent.
- **Short lookups stay inline.** The trivial-lookup → haiku routing is retired:
  lookup-phrased prompts get no pick (audited as tier `default`, band `none`).
  The `router-haiku` agent stays for explicit dispatch (`#model=haiku`, a plan
  step, a fan-out sub-task). The silent no-match default's tier is renamed
  `trivial` → `default`.
- **Injection shortened.** `AUTO-ROUTE: Agent(subagent_type=…) per
  auto-model-routing skill.` plus the unchanged `<router-decision>` block
  (~236 chars, down from ~410 for the base case); the `[auto-router]` header
  line (model/effort/confidence/parent/ctx, all in the block or the audit row)
  and the repeated procedure text are gone — the skill carries the procedure.
  The `fanout:` clause and the budget line are shorter too.
- **Context warning `200K → 600K`** (`CC_ROUTER_CTX_WARN`) to match the
  auto-compact window; `auto-model-routing` SKILL text updated.
- **Pricing for the 5.5 lineup** (`hooks/pricing.py`). Aliases price as the
  current model — Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Haiku 5.5 $0.10/$0.50,
  Fable 5.1 $10/$50 (2.5x Opus) per MTok — with per-model cache-read rates (5%
  of input on Opus/Sonnet 5.5, 10% Haiku 5.5, 2.5% Fable 5.1) and cache writes
  at 2x input (1-hour TTL). Full model ids price as themselves: Opus 5 / 4.8
  $5/$25, Sonnet 5 $2/$10, Haiku 4.5 $1/$5; an unlisted version falls back to
  its family's current model.
- **Savings are measured against the parent session's model**, not an all-Opus
  baseline: a sub-agent's usage (cache included) is priced at its session's
  dominant model minus its own. `usage-report.py --json` keys renamed:
  `est_saved_vs_opus_usd` → `est_saved_vs_parent_usd`,
  `parent_model_premium_usd` → `worker_premium_usd`. `GOALS.md` drops the
  Haiku 15-20% share target.
- **Worker agents pin `effort:`** in their frontmatter — haiku `low`, sonnet
  `medium`, opus `high`, fable `high` — and their text now names Opus 5.5 /
  Sonnet 5.5 / Haiku 5.5 / Fable 5.1 (Fable is 2.5x Opus; fast mode on Opus is
  $8/$40).
- **`cc-route` reads the decision from the audit row** of a throwaway cache
  instead of parsing the injected block (which carries no `tier`/`confidence`/
  `reason` and is empty for same-or-higher picks), and launches without
  `--model` when the router has no confident pick.

### Added

- **`hooks/subagent-stop-audit.py` (SubagentStop).** ~90% of router workers run
  in the background, so the PostToolUse launch row has no usage. When a
  `router-*` worker finishes, this hook sums its transcript (deduped by
  `message.id`) into a `worker_usage` row joined to the launch row by the new
  `agent_id` field (and its `decision_id`). `usage-report.py` counts a worker's
  usage once: from the dispatch row when it has one, else from this row.
- **Rotated audit files are read.** `analyze-audit.py`, `usage-report.py`, and
  `replay_session_pivot.py` read `audit.jsonl.<stamp>` next to the live log, so
  30/90-day windows are no longer truncated at the last rotation.

### Fixed

- **Usage is summed once per API response.** One response is written once per
  content block, each record repeating the same `usage` (a 2.8x overcount was
  measured). `reconcile-outcomes.py` and `usage-report.py` now fold records by
  `message.id` (max per field; records without an id stand alone).

### Removed

- **`auto_threshold` / `ask_threshold`.** `auto_threshold` was a no-op
  (`band_for` took `min(effective_auto, ROUTE_FLOOR)`, so every value ≥ 0.60
  collapsed to the route floor). Gone: the `router.json` keys, the
  `CC_ROUTER_AUTO_THRESHOLD` / `CC_ROUTER_ASK_THRESHOLD` env vars, the opus
  `auto_floor`, `load_loop_config()`, and the `thresholds` field on audit
  decision rows. `CC_ROUTER_ROUTE_FLOOR` (default 0.60) is the one cutoff.
- **`/router-loop`, `tools/router_loop.py`, `tools/replay_kpi.py`** and their
  tests. The loop only tuned `auto_threshold` (and since 0.8.0 its proposer
  looked for `ask`-band rows that no longer exist), and `replay_kpi.py` only
  re-banded under candidate thresholds. Delete `~/.claude/cache/router/loop-config`
  if you want the leftover loop-private repo gone; `signal.jsonl` (nightly
  Bridge B) now has no reader.
- **Stale docs for code removed in 0.8.0:** the `ask` band, the Haiku fallback
  classifier and `ANTHROPIC_API_KEY` step, the classification-cache bullets,
  and the manifests' "Haiku classifier … asks when ambiguous" descriptions
  (README, INSTALL, `plugin.json`, `marketplace.json`, `route-status.md`).

## [0.8.0] — 2026-09-09

Session-aware, downhill-only routing: the hook now reads the parent session's
own model off its transcript and only ever routes to something strictly
cheaper, replacing the old `ask` band and its interrupt cost entirely.

### Changed

- **Session-aware downhill-only routing.** `read_session_state()` reads the
  parent's current model family and context size off the tail of its own
  transcript. `main()` injects an `AUTO-ROUTE` block only when the routed
  model is strictly cheaper than the parent (`haiku < sonnet < opus < fable`);
  a same-or-higher pick is audited (`outcome_hint: same_or_higher_inline`) and
  stays silent. An explicit `#model=` override or a project rule may still
  route up, since only the human can change the session's own model.
- **`ask` band removed.** Two bands only — `auto` (route) / `none` (silent).
  `band_for()` drops the `AskUserQuestion` path; the old asymmetric-risk
  `ask_threshold` machinery is gone from the decision path (still accepted on
  the CLI/config surface for backward compatibility and ignored).
- **Real `permission_mode` plan detection.** Plan mode now reads
  `payload.permission_mode == "plan"` directly instead of guessing; on a plan
  turn the hook emits one pointer line to `plan-with-models` and nothing else.
- **Injection compacted to under 300 characters.** The prose line carries only
  what changes the parent's behaviour (model/effort/confidence/parent/ctx +
  an optional fanout/budget clause); tier, source, reasoning, and thresholds
  stay in the audit row, not the parent's context.
- **Haiku lookups restored.** Short, unambiguous lookup phrasing
  (`LOOKUP_VERBS` + short/no-code/few-lines) routes to `haiku` again instead
  of defaulting everything ambiguous to Sonnet.
- **Dead classifier-LLM + SHA cache removed.** The old Haiku-fallback
  classifier call and its `~/.claude/cache/router/<sha256>.json` 7-day
  classification cache are gone — the heuristic is now the only classifier,
  and there is nothing left to invalidate on a `CLASSIFIER_VERSION` bump.
- **`load_project_config` merges project + global `router.json` instead of
  first-hit-wins.** A `.claude/router.json` found walking up from `cwd` now
  layers over `~/.claude/router.json`: project scalars (`default_model`,
  `auto_threshold`, ...) override global ones when present and fall through
  to global otherwise; `rules` is the concatenation of project rules then
  global rules, so a project-specific pattern is always checked before a
  global one but a global rule still fires when the project has nothing more
  specific. New `CC_ROUTER_GLOBAL_CONFIG` env override keeps this hermetic
  for tests.
- **Namespaced agent dispatch.** All worker dispatch instructions use the
  `auto-model-router:router-<model>` form; the bare `router-<model>` name
  fails with "Agent type not found" once installed as a plugin.

### Added

- **Every dispatch and inline turn now recorded with realized tokens.**
  `post-agent-audit.py` captures realized usage (input/output/cache
  read/cache write, `model_actual`) for both router and native `Agent`
  dispatches; `reconcile-outcomes.py` (Stop hook) backstops turns that never
  got an explicit outcome row, so the audit log no longer silently drops
  inline work.
- **Realized savings report + `--compare-days`.** `tools/usage-report.py`
  gains a "Realized — actual $ from audit-row usage" rollup (actual spend by
  kind × `model_actual` family) alongside the existing all-Opus counterfactual
  estimate, plus `--compare-days N` to diff the current window against a
  prior one of the same length.

### Removed

- `overrides.jsonl` and the ask-band `AskUserQuestion` confirmation step —
  nothing writes to it anymore; see `skills/auto-model-routing/references/tooling.md`.

## [0.7.0] — 2026-09-02

Autonomy pass: cut the ask-band interrupt rate and heal the outcome log. Driven
by 113 days of audit data (7,153 events) — asks were ~24% of prompts, 78% of
them from repos with no `.claude/router.json`, and outcome logging had gone
dormant for the 3rd time.

### Changed

- **Opus auto-floor `0.90 → 0.85`** (`MODEL_TIERS["opus"]["auto_floor"]`). The
  legacy floor forced ~1 in 3 deep/design prompts into `ask` at confidence 0.85
  even though the parent was usually already on Opus. 0.85 keeps some asymmetric
  caution while auto-routing the high-value deep work.
- **Continuity now suppresses the interrupt instead of causing it.** The old
  `+0.1` auto-threshold bump perversely pushed mid-session standard prompts INTO
  the ask band. Removed the bump; instead, once a session is 5+ turns deep a
  would-be `ask` is downgraded to a silent inline turn (Branch C), logged
  `continuity_inline`.
- **`CLASSIFIER_VERSION` 6 → 7** to expire cached decisions under the new bands.
- **Trimmed the injected `#model=`/`#noshift` override footer** — zero real uses
  across ~113 days of audit; it only added per-prompt tokens.

### Added

- **`hooks/reconcile-outcomes.py` (Stop hook).** Deterministically backstops the
  outcome log: for each of this session's injected `auto` decisions with no
  `delegated`/skip row, appends `auto_inline_unattributed`. Fixes the recurring
  dormant-instrumentation problem (skip logging had drifted to ~3%). Correlated
  by a new `session_id` field stamped onto injected/silent audit rows. Fail-open
  and idempotent.

### Notes

- Pairs with enriched `~/.claude/router.json` + a new `claude-os` overlay
  (`default_model` + deep-verb→opus rules that clear the floor via rule
  confidence 0.95, + lookup→haiku). Those live in the user's config, not the
  plugin.

## [0.6.0] — 2026-08-05

### Changed

- **Claude 5 family alignment.** Worker model targeting is unchanged — bare
  aliases (`opus`/`sonnet`/`haiku`/`fable`) already resolve to Opus 5 / Sonnet 5
  / Haiku 4.5 / Fable 5 — so this release updates calibration, prose, and worker
  guidance, not model IDs. Prose refreshed: `router-opus` "Opus 4.8 → Opus 5";
  `router-sonnet` "Sonnet 4.6 → Sonnet 5 (now the Claude Code default)".
- **Effort philosophy `deep→high`.** Adopted Opus 5's "start at `high`, reserve
  `xhigh`" in `classify_heuristic`, `classify_haiku`, and the `router` agent.
  `score_effort` still promotes genuinely heavy work to `xhigh`. Net effect:
  high-signal deep prompts stop being confidence-capped into the `ask` band (the
  dominant ask-band friction in the audit).
- **Balanced rebalance toward Sonnet 5.** Light, single-surface `complex` verbs
  (no code block, not multi-file/long) now route `sonnet/medium` instead of
  `opus/high`; `router-sonnet` escalates via `Stopped:` on real depth. `has_code`
  / multi-file complex work stays on Opus.
- **Worker prompt-guide updates.** `router-opus` no longer mandates a blanket
  verification pass (Opus 5 self-verifies) and caps self-delegation; `router-
sonnet` notes literal instruction-following; `router-fable` gains
  "act-when-you-have-enough", progress-grounding, no-reasoning-echo, and
  refusal-fallback notes (Opus 5 / Fable 5 prompting guides).
- **`CLASSIFIER_VERSION` 5 → 6** to expire cached decisions from the old
  philosophy. `pricing.py` docstring updated to the 5.x era (rates unchanged —
  already correct for Claude 5).

### Fixed

- **`router-fable` dispatch gap.** Added the missing
  `~/.claude/agents/router-fable.md` symlink (the other three workers were
  already linked), so `#model=fable` and plan-wave `Model: fable` can dispatch.

### Packaging

- **Manifest brought to catalog quality for marketplace submission.** `plugin.json`
  version corrected to `0.6.0` (it had been left at `0.5.0` while the changelog
  already declared 0.6.0) and given `homepage`, `repository`, `license`, and
  `keywords`. The marketplace entry gains `version`, `tags`, and `license`, and its
  description is cut from a component inventory to a two-sentence catalog card.
  Both manifests pass `claude plugin validate --strict`.
- **Marketplace install documented as the only supported path.** README leads with
  `/plugin marketplace add IuriiTurok/auto-model-router` and warns that the manual
  wiring section of INSTALL.md is mutually exclusive with it — applying both fires
  all three hooks twice and loads every component twice.
- **`router-loop` added to the manual symlink loop** in INSTALL.md; it shipped in
  `commands/` but was never linked, so `/router-loop` was dead on manual installs.
- `.ruff_cache/` gitignored.

## [0.5.0] — 2026-06-11

### Added

- **Configurable `MODEL_TIERS` table** — routing parameters (speed/quality
  profiles, pricing) now centralized and tunable per-deployment without code
  changes. Reads from `.claude/router.json` or `~/.claude/router.json` (project
  bias pattern).
- **Fable tier manual-only mode** — `fable` tier restricted to explicit
  overrides (e.g. `force_model: fable` in `.claude/router.json`). Rationale:
  parent session already runs Fable; auto-routing to a child Fable subagent
  doubles cost (~2x Opus pricing) with diminishing clarity gains. Audit rows
  without fable selection skip this tier.
- **Fable pricing row** — added to `hooks/pricing.py` for cost accounting when
  fable is manually invoked.
- **`router-fable` agent skill** — entry point for explicit fable-override
  workflows (read-only task context, low-confidence prompts, detailed
  inspection).
- **Ultracode goal band=none opt-out** — `band: none` disables auto-routing for
  a decision, treated as `ask`. Audit replay tooling skips `band: none` rows
  (no counterfactual signal). Enables granular per-prompt tuning without bloating
  the classifier.
- **Session-continuity threshold bump** — `AUTO_THRESHOLD` incremented **0.75 →
  0.85** after 5+ consecutive turns in the same session (learned from replay KPI:
  high-confidence decisions cluster at turn 3+ as context stabilizes). Tracks
  `turn_in_session` in audit.
- **`CLASSIFIER_VERSION: 5`** — updated cache invalidation tag after model/rules
  changes.

### Changed

- `plan-with-models` description trimmed (removed redundant "fan-out" mention —
  routing now owns wavegen; plan owns DAG + outcome capture).

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
