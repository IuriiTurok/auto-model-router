#!/usr/bin/env bash
# Golden-fixture test for hooks/auto-router.py.
#
# Each line of fixtures.jsonl is {prompt, expect:{tier,model,effort,conf_min,band,label}}.
# Empty expect.* fields are skipped (used when the value is intentionally
# subject to drift, e.g. exact confidence on a low-confidence fixture).
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
# Point the global-config merge at a file that doesn't exist so fixtures never
# pick up the developer's real ~/.claude/router.json rules.
export CC_ROUTER_GLOBAL_CONFIG="$CC_ROUTER_CACHE_DIR/no-such-global-router.json"
trap 'rm -rf "$CC_ROUTER_CACHE_DIR"' EXIT

PASS=0
FAIL=0
LINE=0

while IFS= read -r fixture; do
  LINE=$((LINE + 1))
  [ -z "$fixture" ] && continue
  case "$fixture" in '#'*) continue ;; esac

  label=$(printf '%s' "$fixture" | jq -r '.label // ""')
  expect_silent=$(printf '%s' "$fixture" | jq -r '.expect.silent // false')
  expect_tier=$(printf '%s' "$fixture" | jq -r '.expect.tier // ""')
  expect_model=$(printf '%s' "$fixture" | jq -r '.expect.model // ""')
  expect_effort=$(printf '%s' "$fixture" | jq -r '.expect.effort // ""')
  expect_band=$(printf '%s' "$fixture" | jq -r '.expect.band // ""')
  expect_conf_min=$(printf '%s' "$fixture" | jq -r '.expect.conf_min // 0')
  expect_fanout=$(printf '%s' "$fixture" | jq -r 'if .expect|has("fanout") then (.expect.fanout|tostring) else "" end')

  # expect.silent: assert hook produces empty stdout and exits 0
  if [ "$expect_silent" = "true" ]; then
    hook_output=$(printf '%s' "$fixture" \
      | jq -c '{prompt: .prompt, cwd: "/tmp"}' \
      | python3 "$HOOK")
    hook_rc=$?
    if [ $hook_rc -eq 0 ] && [ -z "$hook_output" ]; then
      PASS=$((PASS + 1))
      printf 'PASS L%02d [%-26s] silent=true exit=0\n' "$LINE" "$label"
    else
      FAIL=$((FAIL + 1))
      printf 'FAIL L%02d [%-26s] silent: exit=%d output=%s\n' "$LINE" "$label" "$hook_rc" "$hook_output"
    fi
    continue
  fi

  # Injection is downhill-only (see hooks/auto-router.py): with no
  # transcript_path the parent is assumed to be opus, so opus/fable picks are
  # audited but not injected. Classification is therefore asserted against the
  # audit row the hook always writes, not against stdout. Injection behaviour
  # itself is covered by tests/test_session_state.py.
  printf '%s' "$fixture" \
    | jq -c '{prompt: .prompt, cwd: "/tmp"}' \
    | python3 "$HOOK" >/dev/null
  decision=$(tail -n 1 "$CC_ROUTER_CACHE_DIR/audit.jsonl" | jq -c '.decision')

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

echo "--- classifier fixtures: PASS=$PASS FAIL=$FAIL"

# Run the pure-Python unit suites too, so `bash tests/run.sh` is the one
# entrypoint for the whole test suite. They isolate their own temp dirs.
SUITE_FAIL=$FAIL
echo "=== tests/test_waves.py ==="
python3 "$DIR/test_waves.py" || SUITE_FAIL=$((SUITE_FAIL + 1))
echo "=== tests/test_usage_report.py ==="
python3 "$DIR/test_usage_report.py" || SUITE_FAIL=$((SUITE_FAIL + 1))
echo "=== tests/test_replay_kpi.py ==="
python3 "$DIR/test_replay_kpi.py" || SUITE_FAIL=$((SUITE_FAIL + 1))
echo "=== tests/test_router_loop.py ==="
python3 "$DIR/test_router_loop.py" || SUITE_FAIL=$((SUITE_FAIL + 1))
echo "=== tests/test_continuity.py ==="
python3 "$DIR/test_continuity.py" || SUITE_FAIL=$((SUITE_FAIL + 1))
echo "=== tests/test_session_state.py ==="
python3 "$DIR/test_session_state.py" || SUITE_FAIL=$((SUITE_FAIL + 1))
echo "=== tests/test_agent_audit.py ==="
python3 "$DIR/test_agent_audit.py" || SUITE_FAIL=$((SUITE_FAIL + 1))

echo "==="
[ "$SUITE_FAIL" -eq 0 ] && echo "ALL SUITES PASS" || echo "SUITE FAILURES: $SUITE_FAIL"
[ "$SUITE_FAIL" -eq 0 ]
