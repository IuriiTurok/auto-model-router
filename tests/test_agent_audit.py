#!/usr/bin/env python3
"""Unit tests for hooks/pre-agent-mark.py, hooks/post-agent-audit.py,
hooks/reconcile-outcomes.py, and hooks/pricing.py — realized-usage capture
for every Agent dispatch (router and native) plus inline turns.

Each hook test isolates its own temp CC_ROUTER_CACHE_DIR and never touches
the real cache. Exit 0 if all pass; 1 otherwise.
"""

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS_DIR = os.path.join(ROOT, "hooks")
PRE_MARK = os.path.join(HOOKS_DIR, "pre-agent-mark.py")
POST_AUDIT = os.path.join(HOOKS_DIR, "post-agent-audit.py")
RECONCILE = os.path.join(HOOKS_DIR, "reconcile-outcomes.py")

sys.path.insert(0, HOOKS_DIR)
import pricing  # noqa: E402

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


def run_hook(hook_path: str, payload: dict, cache_dir: str) -> int:
    env = {**os.environ, "CC_ROUTER_CACHE_DIR": cache_dir}
    result = subprocess.run(
        [sys.executable, hook_path],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode


def read_audit(cache_dir: str) -> list[dict]:
    path = os.path.join(cache_dir, "audit.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(x) for x in f if x.strip()]


def write_subagent_transcript(
    transcript_path: str, session_id: str, agent_id: str, model: str
) -> None:
    subagent_dir = os.path.join(
        os.path.dirname(transcript_path), session_id, "subagents"
    )
    os.makedirs(subagent_dir, exist_ok=True)
    subagent_path = os.path.join(subagent_dir, f"agent-{agent_id}.jsonl")
    with open(subagent_path, "w") as f:
        f.write(
            json.dumps({"type": "assistant", "message": {"model": model}}) + "\n"
        )
        # a later record with no model should not override the last-seen one
        f.write(json.dumps({"type": "user", "message": {"content": "ok"}}) + "\n")


def dispatch(
    cache_dir: str,
    subagent_type: str,
    agent_id: str,
    agent_type: str,
    model_in_transcript: str | None,
    extra_response: dict | None = None,
) -> dict:
    """Runs pre-agent-mark then post-agent-audit for one Agent dispatch;
    returns the resulting audit row."""
    transcript_path = os.path.join(cache_dir, "session.jsonl")
    session_id = "sess-" + agent_id
    if model_in_transcript is not None:
        write_subagent_transcript(
            transcript_path, session_id, agent_id, model_in_transcript
        )

    tool_use_id = "tu-" + agent_id
    tool_input = {"subagent_type": subagent_type, "description": "do the thing"}

    pre_payload = {
        "tool_name": "Agent",
        "tool_input": tool_input,
        "tool_use_id": tool_use_id,
        "session_id": session_id,
        "transcript_path": transcript_path,
    }
    rc = run_hook(PRE_MARK, pre_payload, cache_dir)
    check(f"{subagent_type}: pre-agent-mark exits 0", rc == 0)

    tool_response = {
        "agentId": agent_id,
        "agentType": agent_type,
        "totalTokens": 12345,
        "totalDurationMs": 6789,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 10,
            "cache_creation_input_tokens": 5,
        },
    }
    if extra_response:
        tool_response.update(extra_response)

    post_payload = {
        "tool_name": "Agent",
        "tool_input": tool_input,
        "tool_response": tool_response,
        "tool_use_id": tool_use_id,
        "session_id": session_id,
        "transcript_path": transcript_path,
    }
    rc = run_hook(POST_AUDIT, post_payload, cache_dir)
    check(f"{subagent_type}: post-agent-audit exits 0", rc == 0)

    rows = read_audit(cache_dir)
    check(f"{subagent_type}: exactly one audit row written", len(rows) == 1)
    return rows[-1] if rows else {}


# ---------------------------------------------------------------------------
# Test 1: namespaced router dispatch (auto-model-router:router-sonnet)
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as cache_dir:
    row = dispatch(
        cache_dir,
        subagent_type="auto-model-router:router-sonnet",
        agent_id="a1",
        agent_type="router-sonnet",
        model_in_transcript="claude-sonnet-5",
    )
    check("namespaced router: kind == router", row.get("kind") == "router")
    check("namespaced router: model == sonnet", row.get("model") == "sonnet")
    check("namespaced router: outcome == delegated", row.get("outcome") == "delegated")
    check(
        "namespaced router: model_actual read from subagent transcript",
        row.get("model_actual") == "claude-sonnet-5",
    )
    check("namespaced router: total_tokens == 12345", row.get("total_tokens") == 12345)
    check("namespaced router: duration_ms == 6789", row.get("duration_ms") == 6789)
    check(
        "namespaced router: usage.tokens_in == 100",
        (row.get("usage") or {}).get("tokens_in") == 100,
    )
    check("namespaced router: wall_ms present", isinstance(row.get("wall_ms"), int))
    check("namespaced router: agent_type == router-sonnet", row.get("agent_type") == "router-sonnet")

# ---------------------------------------------------------------------------
# Test 2: bare router dispatch (router-opus)
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as cache_dir:
    row = dispatch(
        cache_dir,
        subagent_type="router-opus",
        agent_id="a2",
        agent_type="router-opus",
        model_in_transcript="claude-opus-5",
    )
    check("bare router: kind == router", row.get("kind") == "router")
    check("bare router: model == opus", row.get("model") == "opus")
    check("bare router: outcome == delegated", row.get("outcome") == "delegated")
    check(
        "bare router: model_actual read from subagent transcript",
        row.get("model_actual") == "claude-opus-5",
    )

# ---------------------------------------------------------------------------
# Test 3: native (non-router) dispatch — e.g. general-purpose / Explore
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as cache_dir:
    row = dispatch(
        cache_dir,
        subagent_type="general-purpose",
        agent_id="a3",
        agent_type="general-purpose",
        model_in_transcript="claude-sonnet-5",
    )
    check("native dispatch: kind == native", row.get("kind") == "native")
    check(
        "native dispatch: outcome == native_dispatch",
        row.get("outcome") == "native_dispatch",
    )
    check("native dispatch: no 'model' field injected", "model" not in row)
    check(
        "native dispatch: agent_type == general-purpose",
        row.get("agent_type") == "general-purpose",
    )
    check(
        "native dispatch: model_actual read from subagent transcript",
        row.get("model_actual") == "claude-sonnet-5",
    )
    check("native dispatch: total_tokens == 12345", row.get("total_tokens") == 12345)
    check(
        "native dispatch: usage.tokens_out == 50",
        (row.get("usage") or {}).get("tokens_out") == 50,
    )

# ---------------------------------------------------------------------------
# Test 4: native dispatch with a different subagent_type (Explore) — sanity
# that "native" covers more than one non-router name.
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as cache_dir:
    row = dispatch(
        cache_dir,
        subagent_type="Explore",
        agent_id="a4",
        agent_type="Explore",
        model_in_transcript=None,  # no subagent transcript -> model_actual None
    )
    check("Explore dispatch: kind == native", row.get("kind") == "native")
    check(
        "Explore dispatch: model_actual falls back to None when transcript missing",
        row.get("model_actual") is None,
    )

# ---------------------------------------------------------------------------
# Test 5: reconcile-outcomes.py attaches summed inline usage to the
# auto_inline_unattributed row it writes.
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as cache_dir:
    session_id = "sess-reconcile-1"
    decision_id = "dec-001"
    audit_path = os.path.join(cache_dir, "audit.jsonl")
    os.makedirs(cache_dir, exist_ok=True)
    with open(audit_path, "w") as f:
        f.write(
            json.dumps(
                {
                    "ts": "2026-09-09T00:00:00+00:00",
                    "outcome": "injected",
                    "session_id": session_id,
                    "decision": {
                        "decision_id": decision_id,
                        "band": "auto",
                        "model": "sonnet",
                    },
                }
            )
            + "\n"
        )

    transcript_path = os.path.join(cache_dir, "session.jsonl")
    with open(transcript_path, "w") as f:
        # earlier human turn (should NOT be counted)
        f.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "model": "claude-sonnet-5",
                        "usage": {
                            "input_tokens": 999,
                            "output_tokens": 999,
                            "cache_read_input_tokens": 999,
                            "cache_creation_input_tokens": 999,
                        },
                    },
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {"content": [{"type": "text", "text": "do the task"}]},
                }
            )
            + "\n"
        )
        # tool_result envelope (not a human turn) — must not reset the window
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [{"type": "tool_result", "content": "ok"}]
                    },
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "model": "claude-sonnet-5",
                        "usage": {
                            "input_tokens": 200,
                            "output_tokens": 30,
                            "cache_read_input_tokens": 20,
                            "cache_creation_input_tokens": 10,
                        },
                    },
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "model": "claude-sonnet-5",
                        "usage": {
                            "input_tokens": 50,
                            "output_tokens": 15,
                            "cache_read_input_tokens": 5,
                            "cache_creation_input_tokens": 2,
                        },
                    },
                }
            )
            + "\n"
        )

    payload = {"session_id": session_id, "transcript_path": transcript_path}
    rc = run_hook(RECONCILE, payload, cache_dir)
    check("reconcile: exits 0", rc == 0)

    rows = read_audit(cache_dir)
    added = [r for r in rows if r.get("outcome") == "auto_inline_unattributed"]
    check("reconcile: one auto_inline_unattributed row appended", len(added) == 1)
    usage = (added[0] or {}).get("usage") if added else None
    check("reconcile: usage block present", usage is not None)
    if usage:
        check("reconcile: usage.input == 250 (200+50, excludes pre-human turn)", usage.get("input") == 250)
        check("reconcile: usage.output == 45", usage.get("output") == 45)
        check("reconcile: usage.cache_read == 25", usage.get("cache_read") == 25)
        check("reconcile: usage.cache_write == 12", usage.get("cache_write") == 12)
        check(
            "reconcile: usage.context_tokens == last turn's input+cache_read+cache_write (50+5+2=57)",
            usage.get("context_tokens") == 57,
        )
        check("reconcile: usage.model == claude-sonnet-5", usage.get("model") == "claude-sonnet-5")

# ---------------------------------------------------------------------------
# Test 6: pricing.py cache_read/cache_write rates + cost_usd helper
# ---------------------------------------------------------------------------
check(
    "pricing: cache_read == 0.1x input for sonnet",
    abs(pricing.PRICES["sonnet"]["cache_read"] - pricing.PRICES["sonnet"]["in"] * 0.1)
    < 1e-12,
)
check(
    "pricing: cache_write == 1.25x input for opus",
    abs(pricing.PRICES["opus"]["cache_write"] - pricing.PRICES["opus"]["in"] * 1.25)
    < 1e-12,
)
sonnet_cost = pricing.cost_usd(
    "claude-sonnet-5",
    {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
    },
)
expected = (
    pricing.PRICES["sonnet"]["in"]
    + pricing.PRICES["sonnet"]["out"]
    + pricing.PRICES["sonnet"]["cache_read"]
    + pricing.PRICES["sonnet"]["cache_write"]
) * 1_000_000
check(
    "pricing: cost_usd sums input/output/cache_read/cache_write at 1 MTok each",
    abs(sonnet_cost - expected) < 1e-6,
)
check(
    "pricing: cost_usd accepts audit-log short keys (tokens_in/tokens_out/...)",
    abs(
        pricing.cost_usd(
            "sonnet",
            {"tokens_in": 1_000_000, "tokens_out": 1_000_000},
        )
        - (pricing.PRICES["sonnet"]["in"] + pricing.PRICES["sonnet"]["out"]) * 1_000_000
    )
    < 1e-6,
)
check(
    "pricing: cost_usd returns 0.0 for unknown model",
    pricing.cost_usd("some-unknown-model-xyz", {"input_tokens": 1000}) == 0.0,
)
check(
    "pricing: cost_usd returns 0.0 for empty usage",
    pricing.cost_usd("sonnet", {}) == 0.0,
)

print(f"--- agent_audit: {'OK' if FAIL == 0 else f'{FAIL} FAILED'}")
sys.exit(1 if FAIL else 0)
