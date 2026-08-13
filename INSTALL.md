# Installing auto-model-router

This plugin ships as a single-plugin marketplace. Two commands inside
Claude Code and one optional shell command to wire up the CLI wrapper.

v0.5 adds a fourth worker agent (`router-fable`, manual/override-only),
an `optout` tier for clean opt-out signalling, and a `continuity` flag
for mid-iteration prompts. The hook count and install steps are unchanged.

## 1 — Add the marketplace

In any Claude Code session:

```
/plugin marketplace add iuriiturok/auto-model-router
```

(Replace `iuriiturok/auto-model-router` with the actual GitHub
`owner/repo` if you forked it.)

Claude Code clones the repo into `~/.claude/plugins/marketplaces/`,
parses `.claude-plugin/marketplace.json`, and registers the plugin.

## 2 — Install the plugin

```
/plugin install auto-model-router@auto-model-router-mp
```

Confirm the install. Claude Code will:

- Register the `UserPromptSubmit` hook at
  `hooks/auto-router.py` (no manual `settings.json` edit needed).
- Make the skills `auto-model-routing` and `plan-with-models`
  discoverable.
- Make the subagents `router-haiku`, `router-sonnet`, `router-opus`, and
  `router-fable` available to the `Agent` tool. (`router-fable` is
  manual/override-only — the classifier never auto-dispatches to it.)
- Make the slash commands `/route` and `/route-status` available.

## 3 — (Optional but recommended) Install the cc-route CLI wrapper

For session-launch routing — `cc-route "build a thing"` classifies
your prompt and launches `claude --model X` with the right model —
add the wrapper to your PATH:

```bash
~/.claude/plugins/marketplaces/auto-model-router-mp/plugins/auto-model-router/bin/install.sh
```

(Or wherever Claude Code installed the plugin — the script just
symlinks the `cc-route` binary into `~/.local/bin/`.)

After install, verify:

```bash
command -v cc-route && cc-route --help
```

## 4 — (Optional) Set ANTHROPIC_API_KEY for the Haiku fallback

Without an API key, ambiguous prompts default to "haiku at
confidence 0.55 (ask)" — cheap-and-confirm. With a key, the router falls
back to a Haiku classifier call (~1.5 s) for borderline cases and emits
much sharper classifications.

```bash
echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ~/.zshrc   # or .bashrc
```

## 5 — Verify it's working

In a fresh Claude Code session, send any prompt and look at the
chat for the injected line:

```
[auto-router] tier=… model=… effort=… confidence=… band=… ...
```

If you see it, the hook fired. Try `/route-status` to confirm the
audit log is being written and to see the rolling distribution.

## 6 — (Optional) Per-project overrides

Drop a `.claude/router.json` at any project root to pin behaviour.
Example for a design/asset project:

```json
{
  "default_model": "sonnet"
}
```

Example for a CAD / firmware / hard-engineering project:

```json
{
  "default_model": "opus",
  "rules": [
    {
      "match": "list|status|find",
      "regex": true,
      "model": "haiku",
      "reason": "trivial lookups still don't need opus"
    }
  ]
}
```

The hook walks up from your `cwd` looking for the first
`.claude/router.json`. See README for the full schema.

## Manual / non-marketplace install

> **Mutually exclusive with steps 1–2 above.** Apply this _only_ if you cloned the
> repo directly and never ran `/plugin install`. Running both wiring paths at once
> fires all three hooks twice per event and loads every skill, agent, and command
> twice (once namespaced `auto-model-router:*`, once bare). If you are migrating
> from a manual install to the marketplace install, remove these hook entries and
> symlinks first.

If you wire the plugin by hand instead of via the marketplace (e.g. you
cloned the repo straight into `~/.claude/plugins/auto-model-router/`), you
must replicate everything the marketplace would auto-load. **`hooks/hooks.json`
is the source of truth — mirror ALL of it, not just the first hook.** As of
v0.3.0 that means **three** hooks (unchanged in v0.5.0):

```jsonc
// ~/.claude/settings.json → "hooks"
"UserPromptSubmit": [
  { "hooks": [{ "type": "command",
    "command": "python3 $HOME/.claude/plugins/auto-model-router/hooks/auto-router.py",
    "timeout": 5 }] }
],
"PreToolUse": [
  { "matcher": "Agent", "hooks": [{ "type": "command",
    "command": "python3 $HOME/.claude/plugins/auto-model-router/hooks/pre-agent-mark.py",
    "timeout": 2 }] }
],
"PostToolUse": [
  { "matcher": "Agent", "hooks": [{ "type": "command",
    "command": "python3 $HOME/.claude/plugins/auto-model-router/hooks/post-agent-audit.py",
    "timeout": 3 }] }
]
```

Miss the `PreToolUse[Agent]` entry and the classifier still works, but
`wall_ms` is never captured — so the parallelism / time-to-results numbers in
`/router-report` stay empty.

Then symlink the skills, subagents, and **all** commands into `~/.claude/`:

```bash
ln -sf ~/.claude/plugins/auto-model-router/skills/auto-model-routing ~/.claude/skills/auto-model-routing
ln -sf ~/.claude/plugins/auto-model-router/skills/plan-with-models     ~/.claude/skills/plan-with-models
for m in haiku sonnet opus fable; do
  ln -sf ~/.claude/plugins/auto-model-router/agents/router-$m.md ~/.claude/agents/router-$m.md
done
for c in route route-status router-report router-loop; do
  ln -sf ~/.claude/plugins/auto-model-router/commands/$c.md ~/.claude/commands/$c.md
done
```

**Caveat:** a manual install does NOT auto-update when the plugin gains new
hooks or commands. After pulling an upgrade, re-check `hooks/hooks.json` and
`commands/` and add anything new by hand (this is exactly how the
`router-report` command and the `pre-agent-mark.py` hook were missed between
v0.1 and v0.3). The marketplace flow has none of this drift.

## Uninstall

```
/plugin uninstall auto-model-router@auto-model-router-mp
/plugin marketplace remove auto-model-router-mp
```

If you installed the CLI wrapper:

```bash
rm ~/.local/bin/cc-route
```

The audit log and classification cache live at
`~/.claude/cache/router/`. Delete that directory if you want a clean
slate.

## Troubleshooting

**Hook isn't firing.**
Check `~/.claude/settings.json` — under `hooks` there should be either
marketplace-registered entries (no manual edit needed) or, if you
installed manually, command entries pointing at the plugin path. There
are **three** hooks (UserPromptSubmit + PreToolUse[Agent] +
PostToolUse[Agent]) — see "Manual / non-marketplace install" above.
Settings.json hook edits take effect in new sessions (or after opening
`/hooks` once), not mid-session.

**Subagent dispatch goes to the wrong model.**
Run `/route-status` to confirm the classifier's decision matches
what you expected. If yes, the issue is the parent not following
the `auto-model-routing` skill — confirm the skill is in the
discovery list (it's listed in the `Skill` tool's available skills).

**cc-route says "no router hook found".**
The plugin install path moved. Re-run `bin/install.sh` from the
new install location.

**Want to disable routing for one prompt.**
Append `#noshift` to the prompt. To disable for a project, drop
`{"disabled": true}` into that project's `.claude/router.json`.
To disable globally for a shell session: `export CC_ROUTER_DISABLE=1`.
