---
description: Force a re-classification of a prompt by the auto-router (bypasses the 7-day cache). Useful when rules were tweaked or the cached decision is stale. Usage `/route <prompt>` or `/route --show-last` to print the most recent decision.
---

# /route

Re-run the auto-router classifier on a prompt without using the cache.

## Usage

```
/route <prompt>
```

When invoked:

1. Read the user's `<prompt>` argument.
2. Run the auto-router classifier on it. The classifier script lives at
   `~/.claude/plugins/auto-model-router/hooks/auto-router.py`. Pipe the
   prompt to it as JSON with `force_recache: true`:

   ```bash
   echo '{"prompt": "<prompt>"}' | \
     CC_ROUTER_FORCE_RECACHE=1 \
     python3 ~/.claude/plugins/auto-model-router/hooks/auto-router.py
   ```

   (If the script doesn't honour `CC_ROUTER_FORCE_RECACHE` yet, manually
   delete the cache entry: `rm ~/.claude/cache/router/$(echo -n
   "<prompt>" | shasum -a 256 | cut -d' ' -f1).json` first.)

3. Parse the `<router-decision>` block from the hook's stdout JSON
   (`hookSpecificOutput.additionalContext`).
4. Show the user:
   - `tier`, `model`, `effort`, `confidence`, `band`, `source`,
     `reason`
   - The suggested action (auto-delegate / ask / silent).
5. If the user wants to apply the decision, dispatch via
   `Agent(subagent_type="router-<model>", prompt="<prompt>")`.

## Flags

- `--show-last` — instead of classifying, print the last 5 decisions
  from `~/.claude/cache/router/audit.jsonl`:

  ```bash
  tail -n 5 ~/.claude/cache/router/audit.jsonl | jq .
  ```

- `--force-opus` / `--force-sonnet` / `--force-haiku` — bypass the
  classifier and route directly to the named tier.
