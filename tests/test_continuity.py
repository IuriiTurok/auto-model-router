#!/usr/bin/env python3
"""Session-continuity tests for hooks/auto-router.py.

Continuity is no longer a turn threshold that silences routing once a session
gets long — it is a shape test on the prompt itself: short acknowledgements and
back-references stay inline, everything else keeps routing however deep into
the session it arrives. Uses a temp CC_ROUTER_CACHE_DIR so it never touches the
real cache. Exit 0 if all pass; 1 otherwise.
"""

import json
import os
import subprocess
import sys
import tempfile

HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hooks",
    "auto-router.py",
)

ROUTABLE_PROMPT = "refactor the auth module to use JWT tokens"


def _run_hook(payload: dict, cache_dir: str) -> tuple[str, int]:
    """Pipe payload JSON to the hook; return (stdout, returncode)."""
    env = {
        **os.environ,
        "CC_ROUTER_CACHE_DIR": cache_dir,
        # Nonexistent by default so this never merges in the developer's real
        # ~/.claude/router.json.
        "CC_ROUTER_GLOBAL_CONFIG": os.environ.get(
            "CC_ROUTER_GLOBAL_CONFIG",
            os.path.join(cache_dir, "no-such-global-router.json"),
        ),
    }
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
        start = ctx.index("<router-decision>") + len("<router-decision>")
        end = ctx.index("</router-decision>")
        return json.loads(ctx[start:end])
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def _audit_rows(cache_dir: str) -> list[dict]:
    path = os.path.join(cache_dir, "audit.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


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
# Test 1: routing survives a long session — the 6th turn still injects, and the
# per-session turn counter tracks it.
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as cache_dir:
    session_id = "test-continuity-session-001"
    payload = {"prompt": ROUTABLE_PROMPT, "cwd": "/tmp", "session_id": session_id}
    last_stdout = ""
    for _ in range(6):
        last_stdout, _rc = _run_hook(payload, cache_dir)

    check(
        "6th turn still injects (no post-turn-5 suppression)",
        _extract_decision(last_stdout) is not None,
    )
    with open(os.path.join(cache_dir, "sessions", f"{session_id}.json")) as f:
        state = json.load(f)
    check("session file counted 6 turns", state.get("turns") == 6)
    check(
        "6th audit row carries turns=6",
        bool(_audit_rows(cache_dir)) and _audit_rows(cache_dir)[-1].get("turns") == 6,
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

# ---------------------------------------------------------------------------
# Test 3: a short follow-up on work in flight stays inline, while the same
# session keeps routing full prompts.
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as cache_dir:
    session_id = "test-continuity-session-002"
    routable, _rc = _run_hook(
        {"prompt": ROUTABLE_PROMPT, "cwd": "/tmp", "session_id": session_id}, cache_dir
    )
    followup, _rc = _run_hook(
        {"prompt": "ok now do that again", "cwd": "/tmp", "session_id": session_id},
        cache_dir,
    )
    check("full prompt routes", _extract_decision(routable) is not None)
    check("follow-up produces no output", followup.strip() == "")
    rows = _audit_rows(cache_dir)
    check(
        "follow-up logged outcome_hint=followup_inline",
        any(r.get("outcome_hint") == "followup_inline" for r in rows),
    )
    check(
        "follow-up row records the parent model it stayed on",
        any(
            r.get("outcome_hint") == "followup_inline"
            and r["decision"]["parent"] == "opus"
            for r in rows
        ),
    )

print(f"--- continuity: {'OK' if FAIL == 0 else f'{FAIL} FAILED'}")
sys.exit(1 if FAIL else 0)
