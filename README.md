# auto-model-router

Per-prompt model routing for Claude Code. Classifies every user prompt
into a tier (trivial / standard / complex / deep), picks the matching
model (haiku / sonnet / opus), and either auto-delegates the work to a
worker subagent or asks before doing so.

The point: stop running every session on Opus when most of the work
fits Sonnet or Haiku. The plugin keeps the parent session on whatever
you launched with and dispatches real work to a model-matched worker
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

## Install

See [INSTALL.md](INSTALL.md). TL;DR:

```
/plugin marketplace add <owner>/auto-model-router
/plugin install auto-model-router@auto-model-router-mp
```

Then optionally run `bin/install.sh` to add the `cc-route` CLI to PATH.

## How it works

```
prompt → UserPromptSubmit hook
         ↓
         classify (heuristic → Haiku fallback → default)
         ↓
         decide band (auto / ask / silent)
         ↓
         inject <router-decision> JSON into context
         ↓
parent session reads block, auto-model-routing skill fires
         ↓
         band=auto + fanout → Branch E: decompose → parallel batch of agents
         band=auto  → Agent(subagent_type="router-{model}", prompt=…)
         band=ask   → AskUserQuestion chip, then dispatch (or fan out)
         band=none  → ignore, do inline
         IN PLAN MODE (parent-detected) → plan-with-models runs parallel waves
         ↓
         worker(s) return → parent synthesizes 1-2 sentence summary → audit log
```

Plan mode is **parent-authoritative**: the `UserPromptSubmit` hook can't see
plan state (the payload has no plan flag), so the parent decides from its own
context and the `plan_mode` field in the decision block is best-effort only.

## Components

```
auto-model-router/
├── .claude-plugin/
│   ├── marketplace.json     # single-plugin marketplace manifest
│   └── plugin.json          # plugin metadata
├── hooks/
│   ├── auto-router.py       # UserPromptSubmit hook — the classifier (+ fanout)
│   ├── post-agent-audit.py  # PostToolUse — outcome capture (tokens, wall, group)
│   ├── pre-agent-mark.py    # PreToolUse — start-time marker for wall-clock
│   ├── waves.py             # pure DAG→waves + non-interference batching
│   └── hooks.json           # hook registration (auto-loaded by Claude Code)
├── skills/
│   ├── auto-model-routing/SKILL.md   # parent: read decision → delegate / fan out
│   └── plan-with-models/SKILL.md     # plan-mode: tags + parallel wave executor
├── agents/
│   ├── router-haiku.md      # worker subagent_type, model=haiku
│   ├── router-sonnet.md     # worker subagent_type, model=sonnet
│   └── router-opus.md       # worker subagent_type, model=opus
├── commands/
│   ├── route.md             # /route <prompt>  — force re-classify
│   └── route-status.md      # /route-status   — recent decisions + parallelism
├── tools/
│   └── analyze-audit.py     # distribution + parallelism analyzer
├── tests/
│   ├── fixtures.jsonl       # golden classifier cases (+ fanout)
│   ├── run.sh               # classifier fixture harness
│   └── test_waves.py        # wave/non-interference unit tests
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

- **`auto` (≥ 0.90)**: parent auto-dispatches via `Agent` and reports the
  worker's result. No interactive step.
- **`ask` (0.60–0.90)**: parent calls `AskUserQuestion` with options
  `[Use <model>] [Use Opus] [Stay on current]`. Honours your answer.
- **`none` (< 0.60)**: silent. Hook emits an audit log entry but
  injects no context.

## Knobs

| Mechanism                          | Effect                                                                                                               |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `#noshift` in the prompt           | Skip routing entirely for this prompt.                                                                               |
| `#noroute` in the prompt           | Alias for `#noshift`.                                                                                                |
| `#model=opus` (or sonnet/haiku)    | Force-route to that tier; confidence 1.0, band=auto.                                                                 |
| Env `CC_ROUTER_DISABLE=1`          | Disable the hook globally for the shell.                                                                             |
| Env `CC_ROUTER_AUTO_THRESHOLD=0.9` | Raise/lower the auto-delegate cutoff (default 0.90).                                                                 |
| Env `CC_ROUTER_ASK_THRESHOLD=0.6`  | Raise/lower the ask cutoff (default 0.60).                                                                           |
| Env `ANTHROPIC_API_KEY`            | Enables the Haiku fallback classifier for ambiguous cases. Without it, low-confidence prompts default to Sonnet/ask. |
| Project `.claude/router.json`      | Per-project overrides (see below). Found by walking up from `cwd`.                                                   |

### Per-project overrides — `.claude/router.json`

Drop a `.claude/router.json` at any project root to pin behaviour for
that tree. Keys (all optional):

```jsonc
{
  "disabled": false, // turn off routing for this project entirely
  "default_model": "opus", // pin every prompt; confidence 0.95, band=auto
  "auto_threshold": 0.85, // override the global auto cutoff
  "ask_threshold": 0.6, // override the global ask cutoff
  "rules": [
    // first match wins, checked before heuristics
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
  systems, hard refactors): `{"default_model": "opus"}` plus a haiku
  rule for trivial lookups.
- **Design / asset / content project**: `{"default_model": "sonnet"}`.
- **SaaS feature work / spec writing**: `{"default_model": "sonnet",
"rules": [{"match": "refactor.*architecture", "regex": true,
"model": "opus"}]}`.
- **Docs-only project**: `{"default_model": "sonnet"}` or even
  `{"default_model": "haiku"}` if the writes are mostly mechanical.

You can also commit `.claude/router.json` to your team repo — it
applies to anyone running the plugin in that tree.

## Cache & audit log

- **Classification cache**: `~/.claude/cache/router/<sha256>.json`,
  7-day TTL. `/route` bypasses by deleting the entry.
- **Audit log**: `~/.claude/cache/router/audit.jsonl`. One line per
  decision; includes prompt SHA prefix, decision, outcome
  (`injected` / `silent`). `/route-status` reads from this.

Both directories live under `~/.claude/` (per-user), not in the
plugin — they survive plugin upgrades and uninstalls.

## Slash commands

- **`/route <prompt>`** — re-run the classifier on `<prompt>`, ignoring
  cache. Useful after editing the heuristic rules. Flags:
  `--show-last`, `--force-opus|sonnet|haiku`.
- **`/route-status`** — show the last 20 decisions and a rolling
  7-day distribution by model and band.

## CLI wrapper — `cc-route`

For new sessions, `cc-route` classifies the first prompt before
launching `claude`, so the session starts on the right model.

```bash
cc-route "list all open PRs"                 # → claude --model haiku "..."
cc-route "design a new payment system"       # → claude --model opus "..."
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
# Heuristic dry-run on a trivial prompt — should print band=auto, model=haiku.
echo '{"prompt":"list all open PRs"}' | \
  python3 <path-to-plugin>/hooks/auto-router.py | \
  python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["hookSpecificOutput"]["additionalContext"][:200])'

# Audit log line-count grows.
wc -l ~/.claude/cache/router/audit.jsonl
```

In a real session, watch the chat for the `[auto-router] …` header line
— it confirms the hook fired. Use `/route-status` periodically to see
the distribution. Common targets after a few weeks of use:

- **By session count**: Opus ~30–40%, Sonnet ~60–70%, Haiku trace.
- **By quota burn**: Opus still ~85–90% (it's used on the harder/longer
  sessions), Sonnet ~10–15%. Most savings come from killing long,
  repetitive low-reasoning sessions that bloat on Opus today.

## Plan-mode behaviour

Plan mode is **parent-authoritative** — the parent recognizes it from its
own context, not the hook (the `UserPromptSubmit` payload carries no
plan-mode flag, so the decision block's `plan_mode` is best-effort and is
usually `false` even mid-plan). When in plan mode, the parent uses the
`plan-with-models` skill, which:

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
ln -sf ~/.claude/plugins/auto-model-router/agents/router-haiku.md    ~/.claude/agents/router-haiku.md
ln -sf ~/.claude/plugins/auto-model-router/agents/router-sonnet.md   ~/.claude/agents/router-sonnet.md
ln -sf ~/.claude/plugins/auto-model-router/agents/router-opus.md     ~/.claude/agents/router-opus.md
ln -sf ~/.claude/plugins/auto-model-router/commands/route.md         ~/.claude/commands/route.md
ln -sf ~/.claude/plugins/auto-model-router/commands/route-status.md  ~/.claude/commands/route-status.md
```

Then add the hook to `~/.claude/settings.json`:

```jsonc
"hooks": {
  "UserPromptSubmit": [{
    "hooks": [{
      "type": "command",
      "command": "python3 $HOME/.claude/plugins/auto-model-router/hooks/auto-router.py",
      "timeout": 5
    }]
  }]
}
```

And run `bin/install.sh` for the `cc-route` PATH symlink.

The symlink dance and manual hook entry are needed only because you're
bypassing the marketplace install. Teammates using the marketplace flow
don't do any of this.

## Known limitations

- **Can't switch the running session's model.** Claude Code hooks do
  not expose a model-switch capability. The router-parent + workers
  pattern works around this by keeping the parent on whatever model
  you launched with and delegating the real work elsewhere.
- **Classification is heuristic-first.** Without `ANTHROPIC_API_KEY`,
  ambiguous prompts fall back to "default Haiku at confidence 0.55"
  (which lands in `ask` band — you'll be prompted, cheap-and-confirm).
  With a key, the Haiku fallback classifier fires within ~1.5 s.
- **Subagents don't inherit your live conversation context** — only
  your project's CLAUDE.md/AGENTS.md, working directory, and the
  prompt the parent passes. For deeply iterative sub-tasks (5+
  back-and-forths on the same thread) you'll want to stay inline.
- **Non-interference relies on declared `Files:`.** Parallel safety is
  proven from each step's declared write set. A step that writes a file
  it didn't declare can collide with a concurrent sibling — which is why
  the wave executor treats unlabelled writers as a bug and constrains
  each agent to its `Files:` in the prompt. When in doubt it serializes.
- **Plan mode is parent-detected, not hook-detected.** The hook never
  sees plan state, so plan-mode routing depends on the parent honoring
  its own context (the `auto-model-routing` skill instructs this).

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
