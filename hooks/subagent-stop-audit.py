#!/usr/bin/env python3
"""SubagentStop hook: record a finished router worker's realized usage.

~90% of router workers run in the background, so the PostToolUse row that
post-agent-audit.py writes at launch carries no usage. SubagentStop fires when
the worker actually finishes; this hook sums the worker's own transcript
(deduped by assistant message.id) and appends one `worker_usage` row to the
audit ledger, joined to the launch row by `agent_id` and to the routing
decision by `decision_id`. Only router-* agent types are recorded.

A foreground dispatch fires both hooks; usage-report.py counts a worker's
usage once, preferring the PostToolUse row's.

Idempotent and silent — never blocks the subagent. On any problem it exits 0
without writing.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

CACHE_DIR = os.path.expanduser(
    os.environ.get("CC_ROUTER_CACHE_DIR", "~/.claude/cache/router")
)
AUDIT_LOG = os.path.join(CACHE_DIR, "audit.jsonl")

# Bare `router-<model>` or namespaced `auto-model-router:router-<model>`.
ROUTER_TYPE_RE = re.compile(r"^(?:auto-model-router:)?router-(.+)$")

# How much of the audit tail to scan for the launch row / a prior stop row. A
# background worker can run for hours, so reach well past the usual 256 KB.
AUDIT_TAIL_BYTES = 2 * 1024 * 1024

# Zero-usage placeholder records (API errors, "No response requested.") are
# written under this model; they are not a real turn.
SYNTHETIC_MODEL = "<synthetic>"


def agent_transcript_path(payload: dict, session_id, agent_id) -> str | None:
    """The worker's own transcript: the path the payload carries, else the
    <session>/subagents/agent-<id>.jsonl layout next to the parent transcript."""
    path = payload.get("agent_transcript_path")
    if path:
        return os.path.expanduser(path)
    parent = payload.get("transcript_path")
    if parent and session_id and agent_id:
        return os.path.join(
            os.path.dirname(os.path.expanduser(parent)),
            session_id,
            "subagents",
            f"agent-{agent_id}.jsonl",
        )
    return None


def sum_worker_usage(path: str) -> tuple[dict, str | None] | None:
    """Sum the worker's assistant usage -> ({tokens_in, tokens_out, cache_read,
    cache_write}, last model id), or None when the transcript is unreadable or
    carries no usage.

    One API response is written once per content block, every record carrying
    the same message.id and usage, so fold per id (max per field: output grows
    while streaming). A record without an id stands alone."""
    messages = {}
    model = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                if msg.get("model") == SYNTHETIC_MODEL:
                    continue  # would win as the last-seen model
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                cur = messages.setdefault(
                    msg.get("id") or f"_{i}",
                    {
                        "tokens_in": 0,
                        "tokens_out": 0,
                        "cache_read": 0,
                        "cache_write": 0,
                    },
                )
                for key, src in (
                    ("tokens_in", "input_tokens"),
                    ("tokens_out", "output_tokens"),
                    ("cache_read", "cache_read_input_tokens"),
                    ("cache_write", "cache_creation_input_tokens"),
                ):
                    cur[key] = max(cur[key], usage.get(src) or 0)
                if msg.get("model"):
                    model = msg["model"]
    except OSError:
        return None
    if not messages:
        return None
    totals = {
        key: sum(m[key] for m in messages.values())
        for key in ("tokens_in", "tokens_out", "cache_read", "cache_write")
    }
    return totals, model


def scan_audit_tail(session_id, agent_id) -> tuple[str | None, bool]:
    """-> (decision_id, already_recorded).

    decision_id is the one the worker's launch row carries (joined on
    agent_id); with no launch row it falls back to the session's most recent
    injected decision, the join post-agent-audit.py uses. already_recorded is
    True when this worker already has a `worker_usage` row (re-fired hook)."""
    try:
        with open(AUDIT_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - AUDIT_TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None, False
    fallback = None
    # Newest first: a worker's stop row (if any) follows its launch row.
    for line in reversed(tail.splitlines()):
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        if agent_id and rec.get("agent_id") == agent_id:
            if rec.get("outcome") == "worker_usage":
                return None, True
            return rec.get("decision_id"), False  # the launch row
        if (
            fallback is None
            and rec.get("outcome") == "injected"
            and (session_id is None or rec.get("session_id") == session_id)
        ):
            fallback = (rec.get("decision") or {}).get("decision_id")
    return fallback, False


def main() -> int:
    if os.environ.get("CC_ROUTER_DISABLE") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    agent_type = (
        payload.get("agent_type") or payload.get("subagent_type") or ""
    ).strip()
    m = ROUTER_TYPE_RE.match(agent_type)
    if not m:
        return 0
    agent_id = payload.get("agent_id")
    session_id = payload.get("session_id")
    path = agent_transcript_path(payload, session_id, agent_id)
    if not path:
        return 0
    summed = sum_worker_usage(path)
    if summed is None:
        return 0
    usage, model_actual = summed
    decision_id, already = scan_audit_tail(session_id, agent_id)
    if already:
        return 0

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "outcome": "worker_usage",
        "kind": "router",
        "subagent_type": agent_type,
        "model": m.group(1) or "?",
        "agent_id": agent_id,
        "session_id": session_id,
        "decision_id": decision_id,
        "model_actual": model_actual,
        "usage": usage,
    }
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
