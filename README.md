# auto-model-router

Per-prompt model routing for Claude Code. Classifies every typed prompt
into a tier (standard / complex / deep), picks the matching model
(sonnet / opus; haiku on request), and — only when that model is
strictly cheaper than the one your session runs on — auto-delegates the
work to a worker subagent. Otherwise it stays silent and your session
just does the work.

The point: stop running a whole session on Opus when part of the work
fits Sonnet. The plugin keeps the parent session on whatever you
launched with and dispatches real work to a model-matched worker
subagent. New sessions can be launched on the right model from the
start via the bundled `cc-route` CLI wrapper.

**Parallel by default where it's safe.** Multi-part prompts and plan-mode
steps follow one spine — **decompose → cheapest sufficient model per task →
run independent pieces in parallel → synthesize**. That's faster
(wall-clock), cheaper (many small Haiku/Sonnet contexts beat one giant
Opus-rate context), and higher quality (a dedicated synthesis pass). Agents
only run concurrently when they can't collide: read-only tasks are always
safe, writers run together only when their file sets are **disjoint**, and
overlapping writers are serialized (or isolated in a git worktree). See
[Plan-mode behaviour](#plan-mode-behaviour).

**v0.5 additions:** a fourth worker agent `router-fable` (manual/override-only
via `#model=fable` — never auto-dispatched) for frontier-stakes synthesis steps;
an `optout` tier so `#noshift`/`#noroute`/disabled-project prompts emit a clean
`tier=optout, band=none` signal instead of falling through silently; and a
`continuity` flag in the decision block that tells the parent to prefer staying
inline when the user is mid-iteration.

## Install

```
/plugin marketplace add IuriiTurok/auto-model-router
/plugin install auto-model-router@auto-model-router-mp
```

Restart Claude Code (or run `/reload-plugins`). That's the whole install — the
hooks, skills, agents, and commands all load from the plugin, and `bin/cc-route`
is added to PATH automatically while the plugin is enabled.

> **Do not also apply the "Manual / non-marketplace install" section of
> [INSTALL.md](INSTALL.md).** It wires the same five hooks by absolute path in
> `~/.claude/settings.json` and symlinks the components into `~/.claude/`. Doing both
> at once fires every hook twice and duplicates every skill, agent, and command.
> It exists only for people who cloned the repo directly instead of installing it.

## How it works

```
prompt → UserPromptSubmit hook
         ↓
         harness event (task notification, scheduled-task run)? → skip, no audit row
         ↓
         classify (heuristic → project default → silent default)
         ↓
         strictly cheaper than the parent session's model?
         ↓
         yes → inject AUTO-ROUTE + <router-decision> JSON into context
         no  → silent (audited, nothing injected)
         ↓
parent session reads block, auto-model-routing skill fires
         ↓
         fanout clause → Branch E: decompose → parallel batch of agents
         otherwise     → Agent(subagent_type="auto-model-router:router-{model}", prompt=…)
         plan mode     → plan-with-models runs parallel waves
         ↓
         worker(s) return → parent synthesizes 1-2 sentence summary → audit log
```

The hook reads the parent's own model off the session transcript, so a pick
the same as or dearer than the session is never injected. Only an inline
`#model=` tag may route up. Short lookups, short follow-ups, and a project's
`default_model` pick are audited but never injected.

Plan mode comes from the payload's `permission_mode`: on a plan turn the hook
injects one line pointing at `plan-with-models` and nothing else.

## Goals & reporting

The router serves three north-star goals, defined in [GOALS.md](GOALS.md):

1. **Cost efficiency** — estimated $ saved by routing work to a cheaper
   model than the session's own (measured against the parent session's
   model, cache tokens included).
2. **Quality** — user-correction rate, router-delegated vs inline. The router
   cohort must not be worse.
3. **Time-to-results** — time-to-first-approval + wall-clock saved by
   parallelism.

`/router-report [7d|1d|yesterday] [--llm-judge]` scores all three over a
window. It reads the audit log + session transcripts (joined by `prompt_sha`),
writes a dated report to `~/.claude/cache/router/reports/`, appends **lessons
learned**, and **offers** concrete, data-derived tweaks (it never applies them
silently). The deterministic core is `tools/usage-report.py` (run it directly
with `--json` for machine output); correction detection is heuristic unless
`--llm-judge` adds a Haiku pass. All figures are estimates and the
router-vs-inline timing comparison is correlation, not causation.

The report reads the rotated audit files (`audit.jsonl.<stamp>`) as well as
the live log, so 30/90-day windows are not truncated at the last rotation, and
it counts a background worker's usage from the `SubagentStop` hook (a
background dispatch returns at launch, before any usage exists).

## Components

```
auto-model-router/
├── .claude-plugin/
│   ├── marketplace.json     # single-plugin marketplace manifest
│   └── plugin.json          # plugin metadata
├── GOALS.md                 # the three north-star KPIs (cost/quality/time)
├── hooks/
│   ├── auto-router.py       # UserPromptSubmit hook — the classifier (+ fanout)
│   ├── post-agent-audit.py  # PostToolUse — outcome capture (tokens, wall, group)
│   ├── pre-agent-mark.py    # PreToolUse — start-time marker for wall-clock
│   ├── subagent-stop-audit.py # SubagentStop — a finished router worker's usage
│   ├── waves.py             # pure DAG→waves + non-interference batching
│   ├── pricing.py           # per-MTok price table for the cost KPI
│   └── hooks.json           # hook registration (auto-loaded by Claude Code)
├── skills/
│   ├── auto-model-routing/SKILL.md   # parent: read decision → delegate / fan out
│   └── plan-with-models/SKILL.md     # plan-mode: tags + parallel wave executor
├── agents/
│   ├── router-haiku.md      # worker subagent_type, model=haiku
│   ├── router-sonnet.md     # worker subagent_type, model=sonnet
│   ├── router-opus.md       # worker subagent_type, model=opus
│   └── router-fable.md      # worker subagent_type, model=fable (manual/override-only)
├── commands/
│   ├── route.md             # /route <prompt>  — force re-classify
│   ├── route-status.md      # /route-status   — recent decisions + parallelism
│   └── router-report.md     # /router-report  — KPI report + lessons + offers
├── tools/
│   ├── analyze-audit.py     # distribution + parallelism analyzer
│   └── usage-report.py      # KPI usage report (cost / quality / time)
├── tests/
│   ├── fixtures.jsonl       # golden classifier cases (+ fanout)
│   ├── run.sh               # full suite entrypoint (fixtures + both py suites)
│   ├── test_waves.py        # wave/non-interference unit tests
│   └── test_usage_report.py # KPI report unit tests
├── bin/
│   ├── cc-route             # CLI wrapper for session-launch routing
│   └── install.sh           # post-install: symlink cc-route to PATH
├── INSTALL.md
├── LICENSE
└── README.md
```

When installed via the marketplace flow, Claude Code auto-discovers the
hook, skills, agents, and commands from the standard subdirectories.
No manual `settings.json` edit needed.

## Confidence bands

- **`auto` (≥ 0.60)**: the pick is routable. It is injected only if it is
  strictly cheaper than the parent session's model; the parent then
  auto-dispatches via `Agent` and reports the worker's result. No
  interactive step.
- **`none` (< 0.60)**: silent. Hook emits an audit log entry but
  injects no context.

## Knobs

| Mechanism                             | Effect                                                                                                               |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `#noshift` in the prompt              | Skip routing entirely for this prompt (opt-out, `tier=optout`).                                                      |
| `#noroute` in the prompt              | Alias for `#noshift`.                                                                                                |
| `#model=opus` (or sonnet/haiku/fable) | Force-route to that tier; confidence 1.0, band=auto. The only way to route to a model dearer than the session's own. |
| Env `CC_ROUTER_DISABLE=1`             | Disable the hook globally for the shell.                                                                             |
| Env `CC_ROUTER_ROUTE_FLOOR=0.6`       | Confidence cutoff below which the hook stays silent (default 0.60).                                                  |
| Env `CC_ROUTER_CTX_WARN=600000`       | Input-token count above which the injection carries a context-budget line (default 600000, the auto-compact window). |
| Project `.claude/router.json`         | Per-project overrides (see below). Found by walking up from `cwd`.                                                   |

### Per-project overrides — `.claude/router.json`

Drop a `.claude/router.json` at any project root to pin behaviour for
that tree. Keys (all optional):

```jsonc
{
  "disabled": false, // turn off routing for this project entirely
  "default_model": "opus", // the pick for prompts no heuristic matched — audited, never injected
  "rules": [
    // first match wins, checked before heuristics; a rule only routes when its
    // model is strictly cheaper than the session's own, like any other pick
    {
      "match": "design|asset|mascot|logo", // regex (because "regex": true)
      "regex": true,
      "model": "sonnet",
      "reason": "design/asset work — Sonnet is plenty",
    },
    {
      "match": "cad|firmware|geometry",
      "regex": true,
      "model": "opus",
      "reason": "engineering / hard reasoning — keep Opus",
    },
  ],
}
```

Common patterns to start from:

- **Engineering-heavy project** (CAD, firmware, complex distributed
  systems, hard refactors): the heuristics already keep deep work on Opus;
  add a haiku rule only for a lookup pattern you want delegated.
- **Design / asset / content project**: a rule like
  `{"match": "logo|mascot|asset", "regex": true, "model": "sonnet"}`
  delegates that work whenever the session runs on Opus.
- **Docs-only project**: a sonnet rule for the doc verbs, or a haiku rule
  if the writes are mostly mechanical.

`default_model` records the pick for prompts the heuristics could not
classify, but it is never injected — an unclassified prompt carries no
per-prompt signal. To delegate a pattern, write a rule. Rules, like every
other pick, are injected only when their model is strictly cheaper than the
session's; use `#model=` in the prompt to route up.

You can also commit `.claude/router.json` to your team repo — it
applies to anyone running the plugin in that tree.

## Cache & audit log

- **Audit log**: `~/.claude/cache/router/audit.jsonl`. One line per
  decision; includes prompt SHA prefix, decision, outcome
  (`injected` / `silent`). `/route-status` reads from this. Past 5 MB it is
  rotated to `audit.jsonl.<stamp>` (kept 30 days); the reports read those too.

The cache directory lives under `~/.claude/` (per-user), not in the
plugin — it survives plugin upgrades and uninstalls.

## Slash commands

- **`/route <prompt>`** — re-run the classifier on `<prompt>`. Useful
  after editing the heuristic rules. Flags:
  `--show-last`, `--force-opus|sonnet|haiku`.
- **`/route-status`** — show the last 20 decisions and a rolling
  7-day distribution by model and band.

## CLI wrapper — `cc-route`

For new sessions, `cc-route` classifies the first prompt before
launching `claude`, so the session starts on the right model.

```bash
cc-route "refactor the rate limiter"         # → claude --model sonnet "..."
cc-route "design a new payment system"       # → claude --model opus "..."
cc-route "ambiguous prompt"                  # no confident pick → plain `claude "..."`
cc-route --dry-run "ambiguous prompt"        # classify only, print decision
cc-route --opus "force opus"                 # bypass classifier
cc-route --show-last                         # print last audit log entry
cc-route "fix bug" --resume <session-id>     # extra flags pass through to claude
```

Run `bin/install.sh` once after plugin install to symlink `cc-route`
into `~/.local/bin/`. Use it as a habit when starting a new task that
you suspect doesn't need Opus — much higher-leverage than remembering
to `/model sonnet` after the session already started on Opus.

## Verifying it's working

```bash
# Dry-run on a prompt that routes to Sonnet. With no transcript the hook assumes
# an Opus parent, so this prints an AUTO-ROUTE line for router-sonnet.
# CC_ROUTER_CACHE_DIR keeps the dry run out of your real audit log.
echo '{"prompt":"refactor the rate limiter"}' | \
  CC_ROUTER_CACHE_DIR="$(mktemp -d)" python3 <path-to-plugin>/hooks/auto-router.py | \
  python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["hookSpecificOutput"]["additionalContext"][:200])'

# Audit log line-count grows.
wc -l ~/.claude/cache/router/audit.jsonl
```

In a real session, watch for the `AUTO-ROUTE: …` line in the injected
context — it confirms the hook fired and picked a cheaper model. Silence is
normal: most prompts are the same model as the session, short follow-ups, or
lookups. Use `/route-status` periodically to see the distribution.

## Plan-mode behaviour

The hook reads plan mode from the `UserPromptSubmit` payload's
`permission_mode` and, on a plan turn, injects one line pointing at the
`plan-with-models` skill. The parent then uses that skill, which:

1. requires every step to carry `Model:`, `Effort:`, and `Files:` tags;
2. builds a dependency DAG from `Depends on:` and groups steps into
   **waves**;
3. within each wave, runs steps **concurrently** when their write-`Files:`
   sets are disjoint (read-only steps are always safe), dispatching each
   conflict-free batch as one message of parallel
   `Agent(subagent_type="router-<model>", …)` calls;
4. serializes overlapping writers (or isolates them in a worktree), then
   synthesizes + verifies after each wave.

`plan-with-models` **owns execution** — it does not hand off to
`subagent-driven-development`, which runs steps strictly sequentially. For
very large plans it can emit a `Workflow` script instead (opt-in).

Outside plan mode, an unmistakably multi-part prompt (numbered list,
"X and Y and Z", "for each…") is flagged `fanout` by the hook and handled by
**Branch E** of `auto-model-routing` with the same non-interference rules.

## Retry / escalation policy

Workers can return `Stopped: too complex for <tier>. Suggest re-dispatch
to router-<higher>.` The parent auto-retries one tier up when it sees
that signal, without asking. Implicit failures (errors, empty output)
trigger a same-tier retry-with-clearer-prompt, then escalate. Even Opus
failures fall back to inline handling with a visible note to the user.
See `skills/auto-model-routing/SKILL.md` → "Retry / escalation policy"
for the full state machine.

## Disabling

- Temporary, per-prompt: append `#noshift`.
- Temporary, per-shell: `export CC_ROUTER_DISABLE=1`.
- Per-project: `{"disabled": true}` in `.claude/router.json`.
- Permanent: `/plugin disable auto-model-router@auto-model-router-mp`.
  Plugin files remain installed; re-enable with `/plugin enable`.

## Local / development install

If you're hacking on the plugin (not installing as a teammate via the
marketplace), you can run it from a clone:

```bash
git clone https://github.com/<owner>/auto-model-router ~/.claude/plugins/auto-model-router
ln -sf ~/.claude/plugins/auto-model-router/skills/auto-model-routing ~/.claude/skills/auto-model-routing
ln -sf ~/.claude/plugins/auto-model-router/skills/plan-with-models   ~/.claude/skills/plan-with-models
for m in haiku sonnet opus fable; do
  ln -sf ~/.claude/plugins/auto-model-router/agents/router-$m.md ~/.claude/agents/router-$m.md
done
for c in route route-status router-report; do
  ln -sf ~/.claude/plugins/auto-model-router/commands/$c.md ~/.claude/commands/$c.md
done
```

Then register **all five hooks** in `~/.claude/settings.json` (mirror
`hooks/hooks.json` — see INSTALL.md → "Manual / non-marketplace install" for
the full block):

```jsonc
"hooks": {
  "UserPromptSubmit": [{ "hooks": [{ "type": "command",
    "command": "python3 $HOME/.claude/plugins/auto-model-router/hooks/auto-router.py", "timeout": 5 }] }],
  "PreToolUse":  [{ "matcher": "Agent", "hooks": [{ "type": "command",
    "command": "python3 $HOME/.claude/plugins/auto-model-router/hooks/pre-agent-mark.py", "timeout": 2 }] }],
  "PostToolUse": [{ "matcher": "Agent", "hooks": [{ "type": "command",
    "command": "python3 $HOME/.claude/plugins/auto-model-router/hooks/post-agent-audit.py", "timeout": 3 }] }],
  "SubagentStop": [{ "hooks": [{ "type": "command",
    "command": "python3 $HOME/.claude/plugins/auto-model-router/hooks/subagent-stop-audit.py", "timeout": 5 }] }],
  "Stop": [{ "hooks": [{ "type": "command",
    "command": "python3 $HOME/.claude/plugins/auto-model-router/hooks/reconcile-outcomes.py", "timeout": 5 }] }]
}
```

And run `bin/install.sh` for the `cc-route` PATH symlink.

The symlink dance and manual hook entries are needed only because you're
bypassing the marketplace install — and they don't auto-update when the plugin
gains new hooks/commands, so re-check `hooks/hooks.json` after upgrades.
Teammates using the marketplace flow don't do any of this.

## Known limitations

- **Can't switch the running session's model.** Claude Code hooks do
  not expose a model-switch capability. The router-parent + workers
  pattern works around this by keeping the parent on whatever model
  you launched with and delegating the real work elsewhere.
- **Classification is heuristic-only.** There is no LLM classifier:
  ambiguous prompts fall through to the project's `default_model` (audited,
  never injected) or a silent default at confidence 0.55 — below the route
  floor, so your session just does the work.
- **Subagents don't inherit your live conversation context** — only
  your project's CLAUDE.md/AGENTS.md, working directory, and the
  prompt the parent passes. For deeply iterative sub-tasks (5+
  back-and-forths on the same thread) you'll want to stay inline.
- **Non-interference relies on declared `Files:`.** Parallel safety is
  proven from each step's declared write set. A step that writes a file
  it didn't declare can collide with a concurrent sibling — which is why
  the wave executor treats unlabelled writers as a bug and constrains
  each agent to its `Files:` in the prompt. When in doubt it serializes.
- **Plan mode depends on the payload.** The hook reads `permission_mode`
  from the `UserPromptSubmit` payload; if a Claude Code version stops sending
  it, plan-mode routing falls back to the parent honoring its own context (the
  `auto-model-routing` skill instructs this).

## Contributing

Issues and PRs welcome on the repo's GitHub page. Common areas to
improve:

- Heuristic regex tuning (lots of false-negatives possible for niche
  task verbs) — including the `fanout` decomposition patterns.
- A learned decomposer for Branch E fan-out (today it's heuristic; the
  captured `group_id` + outcome data is the dataset for training one).
- A streaming dispatch mode so workers' partial output can flow to
  the parent in real time (Claude Code limitation today).

## License

MIT — see [LICENSE](LICENSE).
