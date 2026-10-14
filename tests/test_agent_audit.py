#!/usr/bin/env python3
"""Unit tests for hooks/pre-agent-mark.py, hooks/post-agent-audit.py,
hooks/subagent-stop-audit.py, hooks/reconcile-outcomes.py, and hooks/pricing.py
— realized-usage capture for every Agent dispatch (router and native, including
background workers that finish after launch) plus inline turns.

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
SUBAGENT_STOP = os.path.join(HOOKS_DIR, "subagent-stop-audit.py")

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
        f.write(json.dumps({"type": "assistant", "message": {"model": model}}) + "\n")
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
    check(
        "namespaced router: agent_type == router-sonnet",
        row.get("agent_type") == "router-sonnet",
    )
    check(
        "namespaced router: launch row carries agent_id (SubagentStop join key)",
        row.get("agent_id") == "a1",
    )

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
                    "message": {"content": [{"type": "tool_result", "content": "ok"}]},
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
        check(
            "reconcile: usage.input == 250 (200+50, excludes pre-human turn)",
            usage.get("input") == 250,
        )
        check("reconcile: usage.output == 45", usage.get("output") == 45)
        check("reconcile: usage.cache_read == 25", usage.get("cache_read") == 25)
        check("reconcile: usage.cache_write == 12", usage.get("cache_write") == 12)
        check(
            "reconcile: usage.context_tokens == last turn's input+cache_read+cache_write (50+5+2=57)",
            usage.get("context_tokens") == 57,
        )
        check(
            "reconcile: usage.model == claude-sonnet-5",
            usage.get("model") == "claude-sonnet-5",
        )


# ---------------------------------------------------------------------------
# Test 6: pricing.py cache_read/cache_write rates + cost_usd helper
# ---------------------------------------------------------------------------
def per_mtok(model, field):
    return pricing.price_for(model)[field] * 1e6


def close(a, b):
    return abs(a - b) < 1e-9


# Aliases are priced as the CURRENT model of the family: Opus 5.5 / Sonnet 5.5 /
# Haiku 5.5 / Fable 5.1 ($/MTok in, out, cache read).
for alias, want in {
    "opus": (4.0, 20.0, 0.20),
    "sonnet": (2.0, 10.0, 0.10),
    "haiku": (0.10, 0.50, 0.01),
    "fable": (10.0, 50.0, 0.25),
}.items():
    check(
        f"pricing: alias {alias} -> in/out/cache_read {want}",
        close(per_mtok(alias, "in"), want[0])
        and close(per_mtok(alias, "out"), want[1])
        and close(per_mtok(alias, "cache_read"), want[2]),
    )
check(
    "pricing: cache_write == 2x input (1h TTL) for every alias",
    all(
        close(per_mtok(a, "cache_write"), 2 * per_mtok(a, "in")) for a in pricing.PRICES
    ),
)
check(
    "pricing: Fable 5.1 is 2.5x Opus 5.5",
    close(per_mtok("fable", "in") / per_mtok("opus", "in"), 2.5),
)

# Full model ids: the current 5.5 ids price as the alias; older models still
# served keep their own prices.
for model_id, want in {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-5-20260115": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 0.10),
    "us.anthropic.claude-opus-4-8-v1:0": (5.0, 25.0, 0.50),
}.items():
    check(
        f"pricing: {model_id} -> in/out/cache_read {want}",
        close(per_mtok(model_id, "in"), want[0])
        and close(per_mtok(model_id, "out"), want[1])
        and close(per_mtok(model_id, "cache_read"), want[2]),
    )
check(
    "pricing: an unlisted version falls back to the family's current model",
    pricing.price_for("claude-sonnet-4-6-20260115") is pricing.PRICES["sonnet"],
)

# Savings are measured against the PARENT's price, not an all-Opus baseline.
usage_1m = {"tokens_in": 1_000_000, "tokens_out": 1_000_000}
check(
    "pricing: haiku worker under an opus parent saves opus - haiku",
    close(
        pricing.saving_vs_parent("haiku", "opus", usage_1m),
        (4.0 + 20.0) - (0.10 + 0.50),
    ),
)
check(
    "pricing: sonnet worker under a sonnet parent saves nothing",
    close(pricing.saving_vs_parent("sonnet", "sonnet", usage_1m), 0.0),
)
check(
    "pricing: haiku worker under a sonnet parent is measured against sonnet",
    close(
        pricing.saving_vs_parent("haiku", "sonnet", usage_1m),
        (2.0 + 10.0) - (0.10 + 0.50),
    ),
)
check(
    "pricing: a fable worker under an opus parent is a (negative) saving",
    close(
        pricing.saving_vs_parent("fable", "opus", usage_1m),
        (4.0 + 20.0) - (10.0 + 50.0),
    ),
)
check(
    "pricing: saving is 0.0 when either side is unknown",
    pricing.saving_vs_parent("mystery", "opus", usage_1m) == 0.0
    and pricing.saving_vs_parent("haiku", None, usage_1m) == 0.0,
)

sonnet_cost = pricing.cost_usd(
    "sonnet",
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

# ---------------------------------------------------------------------------
# Test 7: session-scoped decision_id join. audit.jsonl is shared across
# concurrent sessions; a router dispatch must join to ITS OWN session's
# injected decision, not to a different session's globally-most-recent one.
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as cache_dir:
    os.makedirs(cache_dir, exist_ok=True)
    audit_path = os.path.join(cache_dir, "audit.jsonl")
    with open(audit_path, "w") as f:
        # our session's injected decision (written first -> older)
        f.write(
            json.dumps(
                {
                    "ts": "2026-09-22T00:00:00+00:00",
                    "outcome": "injected",
                    "session_id": "sess-A",
                    "decision": {
                        "decision_id": "dec-A",
                        "band": "auto",
                        "model": "sonnet",
                    },
                }
            )
            + "\n"
        )
        # a DIFFERENT concurrent session's injected decision, written last so
        # it is the globally-most-recent injected row (the mis-join trap the
        # pre-fix code fell into).
        f.write(
            json.dumps(
                {
                    "ts": "2026-09-22T00:00:05+00:00",
                    "outcome": "injected",
                    "session_id": "sess-B",
                    "decision": {
                        "decision_id": "dec-B",
                        "band": "auto",
                        "model": "opus",
                    },
                }
            )
            + "\n"
        )

    transcript_path = os.path.join(cache_dir, "session.jsonl")
    write_subagent_transcript(transcript_path, "sess-A", "a7", "claude-sonnet-5")
    tool_input = {
        "subagent_type": "auto-model-router:router-sonnet",
        "description": "x",
    }
    run_hook(
        POST_AUDIT,
        {
            "tool_name": "Agent",
            "tool_input": tool_input,
            "tool_response": {"agentId": "a7", "agentType": "router-sonnet"},
            "tool_use_id": "tu-a7",
            "session_id": "sess-A",
            "transcript_path": transcript_path,
        },
        cache_dir,
    )
    rows = read_audit(cache_dir)
    delegated = [r for r in rows if r.get("outcome") == "delegated"]
    check("session-scoped join: one delegated row", len(delegated) == 1)
    if delegated:
        check(
            "session-scoped join: joins own session (dec-A), not most-recent dec-B",
            delegated[0].get("decision_id") == "dec-A",
        )

# ---------------------------------------------------------------------------
# Test 8: legacy payload with no session_id falls back to the most-recent
# injected decision of any session (pre-session-scoping behaviour preserved).
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as cache_dir:
    os.makedirs(cache_dir, exist_ok=True)
    audit_path = os.path.join(cache_dir, "audit.jsonl")
    with open(audit_path, "w") as f:
        f.write(
            json.dumps(
                {
                    "ts": "2026-09-22T00:00:00+00:00",
                    "outcome": "injected",
                    "session_id": "sess-X",
                    "decision": {"decision_id": "dec-X"},
                }
            )
            + "\n"
        )
    run_hook(
        POST_AUDIT,
        {
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "router-opus", "description": "y"},
            "tool_response": {"agentId": "z1", "agentType": "router-opus"},
            "tool_use_id": "tu-z1",
        },
        cache_dir,
    )
    rows = read_audit(cache_dir)
    delegated = [r for r in rows if r.get("outcome") == "delegated"]
    check("legacy no-session fallback: one delegated row", len(delegated) == 1)
    if delegated:
        check(
            "legacy no-session fallback: joins most-recent injected (dec-X)",
            delegated[0].get("decision_id") == "dec-X",
        )


# ---------------------------------------------------------------------------
# Test 9: usage is summed once per assistant message.id. One API response is
# written once per content block, every record repeating the same usage.
# ---------------------------------------------------------------------------
def assistant_block(msg_id, model, usage, block_type="text"):
    message = {"model": model, "usage": usage}
    if msg_id:
        message["id"] = msg_id
    return {"type": "assistant", "message": message, "block": block_type}


def usage_of(inp, out, cread, cwrite):
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cread,
        "cache_creation_input_tokens": cwrite,
    }


with tempfile.TemporaryDirectory() as cache_dir:
    session_id = "sess-dedupe"
    audit_path = os.path.join(cache_dir, "audit.jsonl")
    with open(audit_path, "w") as f:
        f.write(
            json.dumps(
                {
                    "ts": "2026-10-14T00:00:00+00:00",
                    "outcome": "injected",
                    "session_id": session_id,
                    "decision": {
                        "decision_id": "dec-dd",
                        "band": "auto",
                        "model": "sonnet",
                    },
                }
            )
            + "\n"
        )
    transcript_path = os.path.join(cache_dir, "session.jsonl")
    records = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
        # msg_A: three content blocks; output grows while streaming
        assistant_block(
            "msg_A", "claude-opus-5-5", usage_of(10, 5, 1000, 200), "thinking"
        ),
        assistant_block(
            "msg_A", "claude-opus-5-5", usage_of(10, 40, 1000, 200), "text"
        ),
        assistant_block(
            "msg_A", "claude-opus-5-5", usage_of(10, 90, 1000, 200), "tool_use"
        ),
        # msg_B: two blocks, the later (last) turn
        assistant_block("msg_B", "claude-opus-5-5", usage_of(4, 30, 1300, 0), "text"),
        assistant_block(
            "msg_B", "claude-opus-5-5", usage_of(4, 30, 1300, 0), "tool_use"
        ),
        # no id: stands alone
        assistant_block(None, "claude-opus-5-5", usage_of(1, 2, 3, 4)),
    ]
    with open(transcript_path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    rc = run_hook(
        RECONCILE,
        {"session_id": session_id, "transcript_path": transcript_path},
        cache_dir,
    )
    added = [
        r
        for r in read_audit(cache_dir)
        if r.get("outcome") == "auto_inline_unattributed"
    ]
    usage = (added[0] if added else {}).get("usage") or {}
    check("reconcile dedupe: exits 0 and writes one row", rc == 0 and len(added) == 1)
    check(
        "reconcile dedupe: input counted once per message (10+4+1)",
        usage.get("input") == 15,
    )
    check(
        "reconcile dedupe: output is the max per message (90+30+2)",
        usage.get("output") == 122,
    )
    check(
        "reconcile dedupe: cache_read once per message (1000+1300+3)",
        usage.get("cache_read") == 2303,
    )
    check(
        "reconcile dedupe: cache_write once per message (200+0+4)",
        usage.get("cache_write") == 204,
    )
    check(
        "reconcile dedupe: context_tokens is the last distinct message (1+3+4)",
        usage.get("context_tokens") == 8,
    )


# ---------------------------------------------------------------------------
# Test 10: subagent-stop-audit.py records a background worker's usage, which the
# launch row (PostToolUse) never has.
# ---------------------------------------------------------------------------
def write_worker_transcript(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


WORKER_RECORDS = [
    {"type": "user", "message": {"content": "do the thing"}},
    assistant_block(
        "msg_W1", "claude-sonnet-5-5", usage_of(5, 20, 4000, 1000), "thinking"
    ),
    assistant_block(
        "msg_W1", "claude-sonnet-5-5", usage_of(5, 60, 4000, 1000), "tool_use"
    ),
    {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": "ok"}]},
    },
    assistant_block("msg_W2", "claude-sonnet-5-5", usage_of(3, 40, 5000, 0), "text"),
]

with tempfile.TemporaryDirectory() as cache_dir:
    session_id = "sess-bg"
    transcript_path = os.path.join(cache_dir, "session.jsonl")
    worker_path = os.path.join(cache_dir, session_id, "subagents", "agent-bg1.jsonl")
    write_worker_transcript(worker_path, WORKER_RECORDS)
    audit_path = os.path.join(cache_dir, "audit.jsonl")
    with open(audit_path, "w") as f:
        # an injected decision from ANOTHER session written last (mis-join trap)
        f.write(
            json.dumps(
                {
                    "ts": "2026-10-14T00:00:00+00:00",
                    "outcome": "delegated",
                    "kind": "router",
                    "agent_id": "bg1",
                    "decision_id": "dec-bg",
                    "subagent_type": "auto-model-router:router-sonnet",
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "ts": "2026-10-14T00:00:05+00:00",
                    "outcome": "injected",
                    "session_id": "sess-other",
                    "decision": {"decision_id": "dec-other", "band": "auto"},
                }
            )
            + "\n"
        )
    payload = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "hook_event_name": "SubagentStop",
        "agent_id": "bg1",
        "agent_type": "auto-model-router:router-sonnet",
        "agent_transcript_path": worker_path,
    }
    rc = run_hook(SUBAGENT_STOP, payload, cache_dir)
    rows = [r for r in read_audit(cache_dir) if r.get("outcome") == "worker_usage"]
    check(
        "subagent-stop: exits 0 and writes one worker_usage row",
        rc == 0 and len(rows) == 1,
    )
    row = rows[0] if rows else {}
    usage = row.get("usage") or {}
    check(
        "subagent-stop: row is a router worker joined on agent_id",
        row.get("kind") == "router"
        and row.get("model") == "sonnet"
        and row.get("agent_id") == "bg1"
        and row.get("session_id") == session_id,
    )
    check(
        "subagent-stop: decision_id comes from the launch row, not another session",
        row.get("decision_id") == "dec-bg",
    )
    check(
        "subagent-stop: usage deduped by message.id (5+3 in, 60+40 out)",
        usage.get("tokens_in") == 8 and usage.get("tokens_out") == 100,
    )
    check(
        "subagent-stop: cache read/write deduped (4000+5000, 1000+0)",
        usage.get("cache_read") == 9000 and usage.get("cache_write") == 1000,
    )
    check(
        "subagent-stop: model_actual is the worker transcript's model",
        row.get("model_actual") == "claude-sonnet-5-5",
    )
    run_hook(SUBAGENT_STOP, payload, cache_dir)
    check(
        "subagent-stop: a re-fired hook does not double-record",
        len([r for r in read_audit(cache_dir) if r.get("outcome") == "worker_usage"])
        == 1,
    )

with tempfile.TemporaryDirectory() as cache_dir:
    # Native subagents are not recorded; neither is a worker with no usage.
    session_id = "sess-native"
    transcript_path = os.path.join(cache_dir, "session.jsonl")
    worker_path = os.path.join(cache_dir, session_id, "subagents", "agent-n1.jsonl")
    write_worker_transcript(worker_path, WORKER_RECORDS)
    run_hook(
        SUBAGENT_STOP,
        {
            "session_id": session_id,
            "transcript_path": transcript_path,
            "agent_id": "n1",
            "agent_type": "Explore",
            "agent_transcript_path": worker_path,
        },
        cache_dir,
    )
    empty_path = os.path.join(cache_dir, session_id, "subagents", "agent-n2.jsonl")
    write_worker_transcript(
        empty_path, [{"type": "user", "message": {"content": "hi"}}]
    )
    run_hook(
        SUBAGENT_STOP,
        {
            "session_id": session_id,
            "transcript_path": transcript_path,
            "agent_id": "n2",
            "agent_type": "router-haiku",
            "agent_transcript_path": empty_path,
        },
        cache_dir,
    )
    rc = run_hook(
        SUBAGENT_STOP,
        {
            "session_id": session_id,
            "transcript_path": transcript_path,
            "agent_id": "missing",
            "agent_type": "router-haiku",
        },
        cache_dir,
    )
    check(
        "subagent-stop: native type, empty transcript, and missing transcript write nothing",
        rc == 0 and not os.path.exists(os.path.join(cache_dir, "audit.jsonl")),
    )

with tempfile.TemporaryDirectory() as cache_dir:
    # No agent_transcript_path in the payload: derive the standard layout, and
    # with no launch row fall back to the session's last injected decision.
    session_id = "sess-derive"
    transcript_path = os.path.join(cache_dir, "session.jsonl")
    write_worker_transcript(
        os.path.join(cache_dir, session_id, "subagents", "agent-d1.jsonl"),
        WORKER_RECORDS,
    )
    with open(os.path.join(cache_dir, "audit.jsonl"), "w") as f:
        f.write(
            json.dumps(
                {
                    "ts": "2026-10-14T00:00:00+00:00",
                    "outcome": "injected",
                    "session_id": session_id,
                    "decision": {"decision_id": "dec-derive", "band": "auto"},
                }
            )
            + "\n"
        )
    run_hook(
        SUBAGENT_STOP,
        {
            "session_id": session_id,
            "transcript_path": transcript_path,
            "agent_id": "d1",
            "agent_type": "router-opus",
        },
        cache_dir,
    )
    rows = [r for r in read_audit(cache_dir) if r.get("outcome") == "worker_usage"]
    check(
        "subagent-stop: derives the worker transcript path and falls back to the session's decision",
        len(rows) == 1
        and rows[0].get("model") == "opus"
        and rows[0].get("decision_id") == "dec-derive"
        and (rows[0].get("usage") or {}).get("tokens_out") == 100,
    )

# ---------------------------------------------------------------------------
# Test 11: a trailing `<synthetic>` record (zero usage: an API error or "No
# response requested.") must not become a transcript's last-seen model.
# ---------------------------------------------------------------------------
SYNTHETIC = assistant_block("msg_SYN", "<synthetic>", usage_of(0, 0, 0, 0), "text")


def worker_usage_rows(cache_dir):
    return [r for r in read_audit(cache_dir) if r.get("outcome") == "worker_usage"]


with tempfile.TemporaryDirectory() as cache_dir:
    session_id = "sess-syn"
    worker_path = os.path.join(cache_dir, session_id, "subagents", "agent-syn1.jsonl")
    write_worker_transcript(worker_path, WORKER_RECORDS + [SYNTHETIC])
    run_hook(
        SUBAGENT_STOP,
        {
            "session_id": session_id,
            "transcript_path": os.path.join(cache_dir, "session.jsonl"),
            "agent_id": "syn1",
            "agent_type": "auto-model-router:router-sonnet",
            "agent_transcript_path": worker_path,
        },
        cache_dir,
    )
    rows = worker_usage_rows(cache_dir)
    check(
        "subagent-stop: a trailing <synthetic> record is not model_actual",
        len(rows) == 1 and rows[0].get("model_actual") == "claude-sonnet-5-5",
    )
    check(
        "subagent-stop: the synthetic record leaves the summed usage unchanged",
        len(rows) == 1
        and (rows[0].get("usage") or {})
        == {
            "tokens_in": 8,
            "tokens_out": 100,
            "cache_read": 9000,
            "cache_write": 1000,
        },
    )

with tempfile.TemporaryDirectory() as cache_dir:
    # A worker whose only assistant record is synthetic has nothing to record.
    session_id = "sess-syn-only"
    worker_path = os.path.join(cache_dir, session_id, "subagents", "agent-syn2.jsonl")
    write_worker_transcript(
        worker_path, [{"type": "user", "message": {"content": "hi"}}, SYNTHETIC]
    )
    rc = run_hook(
        SUBAGENT_STOP,
        {
            "session_id": session_id,
            "transcript_path": os.path.join(cache_dir, "session.jsonl"),
            "agent_id": "syn2",
            "agent_type": "router-haiku",
            "agent_transcript_path": worker_path,
        },
        cache_dir,
    )
    check(
        "subagent-stop: a synthetic-only transcript writes no zero-usage row",
        rc == 0 and not os.path.exists(os.path.join(cache_dir, "audit.jsonl")),
    )

with tempfile.TemporaryDirectory() as cache_dir:
    # reconcile-outcomes.py: the inline usage row names the last REAL turn's
    # model and context, not the synthetic record's (model "<synthetic>", ctx 0).
    session_id = "sess-rec-syn"
    with open(os.path.join(cache_dir, "audit.jsonl"), "w") as f:
        f.write(
            json.dumps(
                {
                    "ts": "2026-10-14T00:00:00+00:00",
                    "outcome": "injected",
                    "session_id": session_id,
                    "decision": {
                        "decision_id": "dec-rs",
                        "band": "auto",
                        "model": "sonnet",
                    },
                }
            )
            + "\n"
        )
    transcript_path = os.path.join(cache_dir, "session.jsonl")
    write_worker_transcript(
        transcript_path,
        [
            {
                "type": "user",
                "message": {"content": [{"type": "text", "text": "go"}]},
            },
            assistant_block("msg_R", "claude-opus-5-5", usage_of(10, 20, 300, 40)),
            SYNTHETIC,
        ],
    )
    run_hook(
        RECONCILE,
        {"session_id": session_id, "transcript_path": transcript_path},
        cache_dir,
    )
    added = [
        r
        for r in read_audit(cache_dir)
        if r.get("outcome") == "auto_inline_unattributed"
    ]
    usage = (added[0] if added else {}).get("usage") or {}
    check(
        "reconcile: a trailing <synthetic> record is not the usage model",
        usage.get("model") == "claude-opus-5-5",
    )
    check(
        "reconcile: context_tokens is the last real turn's (10+300+40), not 0",
        usage.get("context_tokens") == 350
        and usage.get("input") == 10
        and usage.get("output") == 20,
    )

with tempfile.TemporaryDirectory() as cache_dir:
    # post-agent-audit.py: model_actual is read from the worker's transcript.
    agent_id = "syn3"
    write_worker_transcript(
        os.path.join(
            cache_dir, "sess-" + agent_id, "subagents", f"agent-{agent_id}.jsonl"
        ),
        WORKER_RECORDS + [SYNTHETIC],
    )
    row = dispatch(
        cache_dir,
        subagent_type="auto-model-router:router-sonnet",
        agent_id=agent_id,
        agent_type="router-sonnet",
        model_in_transcript=None,  # keep the transcript written above
    )
    check(
        "post-agent-audit: a trailing <synthetic> record is not model_actual",
        row.get("model_actual") == "claude-sonnet-5-5",
    )

print(f"--- agent_audit: {'OK' if FAIL == 0 else f'{FAIL} FAILED'}")
sys.exit(1 if FAIL else 0)
