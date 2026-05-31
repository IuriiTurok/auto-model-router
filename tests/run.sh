#!/usr/bin/env bash
# Golden-fixture test for hooks/auto-router.py.
#
# Each line of fixtures.jsonl is {prompt, expect:{tier,model,effort,conf_min,band,label}}.
# Empty expect.* fields are skipped (used when the value is intentionally
# subject to drift, e.g. exact confidence inside an ask-band fixture).
#
# Exit 0 if all fixtures pass; 1 otherwise.

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK="$DIR/../hooks/auto-router.py"
FIXTURES="$DIR/fixtures.jsonl"

if [ ! -f "$HOOK" ]; then
  echo "ERROR: hook not found at $HOOK" >&2
  exit 2
fi
if [ ! -f "$FIXTURES" ]; then
  echo "ERROR: fixtures not found at $FIXTURES" >&2
  exit 2
fi

# Redirect the router's cache + audit writes to a throwaway dir so running the
# fixtures never pollutes the real ~/.claude/cache/router/ (audit.jsonl
# distribution + 7-day classification cache). auto-router.py honours this var.
export CC_ROUTER_CACHE_DIR="$(mktemp -d)"
trap 'rm -rf "$CC_ROUTER_CACHE_DIR"' EXIT

PASS=0
FAIL=0
LINE=0

while IFS= read -r fixture; do
  LINE=$((LINE + 1))
  [ -z "$fixture" ] && continue
  case "$fixture" in '#'*) continue ;; esac

  label=$(printf '%s' "$fixture" | jq -r '.label // ""')
  expect_tier=$(printf '%s' "$fixture" | jq -r '.expect.tier // ""')
  expect_model=$(printf '%s' "$fixture" | jq -r '.expect.model // ""')
  expect_effort=$(printf '%s' "$fixture" | jq -r '.expect.effort // ""')
  expect_band=$(printf '%s' "$fixture" | jq -r '.expect.band // ""')
  expect_conf_min=$(printf '%s' "$fixture" | jq -r '.expect.conf_min // 0')
  expect_fanout=$(printf '%s' "$fixture" | jq -r 'if .expect|has("fanout") then (.expect.fanout|tostring) else "" end')

  decision=$(printf '%s' "$fixture" \
    | jq -c '{prompt: .prompt, cwd: "/tmp"}' \
    | python3 "$HOOK" \
    | python3 -c "
import json, sys
out = json.load(sys.stdin)
ctx = out['hookSpecificOutput']['additionalContext']
start = ctx.index('<router-decision>') + len('<router-decision>\n')
end = ctx.index('\n</router-decision>')
print(ctx[start:end])
")

  got_tier=$(printf '%s' "$decision" | jq -r .tier)
  got_model=$(printf '%s' "$decision" | jq -r .model)
  got_effort=$(printf '%s' "$decision" | jq -r .effort)
  got_band=$(printf '%s' "$decision" | jq -r .band)
  got_conf=$(printf '%s' "$decision" | jq -r .confidence)
  got_fanout=$(printf '%s' "$decision" | jq -r '.fanout // false | tostring')

  errors=""
  [ -n "$expect_fanout" ] && [ "$got_fanout" != "$expect_fanout" ] && errors="$errors fanout=$got_fanout(want $expect_fanout)"
  [ -n "$expect_tier" ] && [ "$got_tier" != "$expect_tier" ] && errors="$errors tier=$got_tier(want $expect_tier)"
  [ -n "$expect_model" ] && [ "$got_model" != "$expect_model" ] && errors="$errors model=$got_model(want $expect_model)"
  [ -n "$expect_effort" ] && [ "$got_effort" != "$expect_effort" ] && errors="$errors effort=$got_effort(want $expect_effort)"
  [ -n "$expect_band" ] && [ "$got_band" != "$expect_band" ] && errors="$errors band=$got_band(want $expect_band)"
  conf_ok=$(python3 -c "print(1 if float('$got_conf') >= float('$expect_conf_min') else 0)")
  [ "$conf_ok" -ne 1 ] && errors="$errors conf=$got_conf(want>=$expect_conf_min)"

  if [ -z "$errors" ]; then
    PASS=$((PASS + 1))
    printf 'PASS L%02d [%-26s] tier=%s model=%s effort=%s conf=%s band=%s\n' \
      "$LINE" "$label" "$got_tier" "$got_model" "$got_effort" "$got_conf" "$got_band"
  else
    FAIL=$((FAIL + 1))
    printf 'FAIL L%02d [%-26s]%s\n' "$LINE" "$label" "$errors"
  fi
done < "$FIXTURES"

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
