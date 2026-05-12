# Installing auto-model-router

This plugin ships as a single-plugin marketplace. Two commands inside
Claude Code and one optional shell command to wire up the CLI wrapper.

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
- Make the subagents `router-haiku`, `router-sonnet`, `router-opus`
  available to the `Agent` tool.
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

Without an API key, ambiguous prompts default to "sonnet at
confidence 0.5 (ask)". With a key, the router falls back to a Haiku
call (~500 ms) for borderline cases and emits much sharper
classifications.

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
    {"match": "list|status|find", "regex": true, "model": "haiku",
     "reason": "trivial lookups still don't need opus"}
  ]
}
```

The hook walks up from your `cwd` looking for the first
`.claude/router.json`. See README for the full schema.

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
Check `~/.claude/settings.json` — under `hooks.UserPromptSubmit`
there should be either a marketplace-registered entry (no manual
edit needed) or, if you installed manually, a `command` pointing at
the plugin path.

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
