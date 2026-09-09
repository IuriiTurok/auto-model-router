---
description: Run the auto-router classifier on a prompt and show its decision. Useful when rules were tweaked and you want to see how a prompt classifies now. Usage `/route <prompt>` or `/route --show-last` to print the most recent decision.
---

# /route

Run the auto-router classifier on a prompt and show its decision.

## Usage

```
/route <prompt>
```

When invoked:

1. Read the user's `<prompt>` argument.
2. Run the auto-router classifier on it. The classifier is a pure
   heuristic (no LLM call, no cache) and lives at
   `~/.claude/plugins/auto-model-router/hooks/auto-router.py`. Pipe the
   prompt to it as JSON:

   ```bash
   echo '{"prompt": "<prompt>"}' | \
     python3 ~/.claude/plugins/auto-model-router/hooks/auto-router.py
   ```

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
