#!/usr/bin/env python3
"""Continuity behaviour tests for hooks/auto-router.py.

Uses a temp CC_ROUTER_CACHE_DIR so it never touches the real cache.
Exit 0 if all pass; 1 otherwise.
"""

import json
import os
import subprocess
import sys
import tempfile

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "auto-router.py")


def _run_hook(payload: dict, cache_dir: str) -> tuple[str, int]:
    """Pipe payload JSON to the hook; return (stdout, returncode)."""
    env = {**os.environ, "CC_ROUTER_CACHE_DIR": cache_dir}
    result = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout, result.returncode


def _extract_decision(stdout: str) -> dict | None:
    """Parse the <router-decision> JSON block from hook stdout."""
    try:
        out = json.loads(stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        start = ctx.index("<router-decision>") + len("<router-decision>\n")
        end = ctx.index("\n</router-decision>")
        return json.loads(ctx[start:end])
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


PASS = 0
FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}")


# ---------------------------------------------------------------------------
# Test 1: 6 turns with the same session_id → 6th decision has continuity
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as cache_dir:
    session_id = "test-continuity-session-001"
    prompt = "refactor the auth module to use JWT tokens"
    last_decision = None
    for i in range(6):
        payload = {"prompt": prompt, "cwd": "/tmp", "session_id": session_id}
        stdout, rc = _run_hook(payload, cache_dir)
        last_decision = _extract_decision(stdout)

    check(
        "6th turn: continuity.turns == 6",
        last_decision is not None
        and last_decision.get("continuity", {}).get("turns") == 6,
    )
    check(
        "6th turn: thresholds.auto == 0.85 (0.75 + 0.10 bump)",
        last_decision is not None
        and abs(last_decision.get("thresholds", {}).get("auto", 0) - 0.85) < 1e-9,
    )

# ---------------------------------------------------------------------------
# Test 2: no session_id → sessions/ dir is never created
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as cache_dir:
    prompt = "refactor the rate limiter to use a token bucket algorithm"
    payload = {"prompt": prompt, "cwd": "/tmp"}
    stdout, rc = _run_hook(payload, cache_dir)
    sessions_dir = os.path.join(cache_dir, "sessions")
    check(
        "no session_id: sessions/ dir absent",
        not os.path.isdir(sessions_dir),
    )

print(f"--- continuity: {'OK' if FAIL == 0 else f'{FAIL} FAILED'}")
sys.exit(1 if FAIL else 0)
