#!/usr/bin/env python3
"""Stop hook: reconcile auto-band router decisions that never got an outcome row.

Model-written skip logging (same_model_inline / skipped_trivial / ...) has gone
dormant repeatedly because it depends on the parent remembering to log. This
hook backstops it deterministically: at the end of every turn, for each of THIS
session's injected `auto`-band decisions that has no terminal outcome row
(a `delegated` row from post-agent-audit, a hand-written skip row, or a prior
reconcile), it appends one `auto_inline_unattributed` row. That converts the
large "auto decided, outcome unknown" bucket into an explicit, measurable
outcome so /router-report and router_loop.py stop flying blind on follow-through.

Correlation uses the `session_id` that auto-router.py now stamps onto every
injected row, plus the `decision_id` carried by outcome rows (globally unique).

Each terminal row this hook writes also carries a `usage` block — the
assistant token usage realized inline since the last human turn in the
transcript — so /router-report can cost auto-band decisions that never left
the main session, not just router-delegated ones.

Contract: fail-open and non-blocking — any error returns 0, and it never emits
`exit 2`. Idempotent — an appended `auto_inline_unattributed` row is itself
terminal, so re-running the hook does not double-count.
"""

import json
import os
import sys
from datetime import datetime, timezone

CACHE_DIR = os.path.expanduser(
    os.environ.get("CC_ROUTER_CACHE_DIR", "~/.claude/cache/router")
)
AUDIT_LOG = os.path.join(CACHE_DIR, "audit.jsonl")

# Outcomes that mark a decision_id as already accounted for. Anything here means
# the auto decision was followed through (or already reconciled) — don't re-log.
TERMINAL_OUTCOMES = {
    "delegated",
    "delegated_failed",
    "same_model_inline",
    "continuity_inline",
    "skipped_trivial",
    "worker_failed",
    "auto_inline_unattributed",
}

# Tail-read cap for the session transcript (usage summation). Cheap and
# generous — a single turn's worth of records rarely exceeds this.
TRANSCRIPT_TAIL_BYTES = 2_000_000


def _is_human_turn(rec: dict) -> bool:
    """A genuine user message (not a tool_result envelope, not meta)."""
    if rec.get("type") != "user":
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip() != ""
    if isinstance(content, list):
        has_text = any(isinstance(b, dict) and b.get("type") == "text" for b in content)
        has_tool_result = any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
        return has_text and not has_tool_result
    return False


def sum_inline_usage(transcript_path: str | None) -> dict | None:
    """Sum assistant message.usage since the last human (non-tool-result
    user) turn in the transcript. Tail-reads the last TRANSCRIPT_TAIL_BYTES
    to stay cheap.

    -> {model, input, output, cache_read, cache_write, context_tokens} where
    context_tokens is the last assistant turn's input + cache_read +
    cache_creation (its live context size, not the summed total). None if
    the transcript is missing/unreadable or has no assistant usage since
    the last human turn.
    """
    if not transcript_path:
        return None
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    records = []
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue

    last_human_idx = -1
    for i, rec in enumerate(records):
        if _is_human_turn(rec):
            last_human_idx = i

    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    model = None
    last_turn_ctx = None
    for rec in records[last_human_idx + 1 :]:
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        usage = msg.get("usage") or {}
        if not usage:
            continue
        tin = usage.get("input_tokens") or 0
        tout = usage.get("output_tokens") or 0
        cread = usage.get("cache_read_input_tokens") or 0
        ccreate = usage.get("cache_creation_input_tokens") or 0
        totals["input"] += tin
        totals["output"] += tout
        totals["cache_read"] += cread
        totals["cache_write"] += ccreate
        if msg.get("model"):
            model = msg.get("model")
        last_turn_ctx = tin + cread + ccreate

    if last_turn_ctx is None:
        return None

    return {
        "model": model,
        "input": totals["input"],
        "output": totals["output"],
        "cache_read": totals["cache_read"],
        "cache_write": totals["cache_write"],
        "context_tokens": last_turn_ctx,
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    session_id = payload.get("session_id")
    if not session_id or not os.path.exists(AUDIT_LOG):
        return 0

    # Single pass: collect this session's injected auto decisions, and every
    # decision_id (any session) that already carries a terminal outcome.
    session_auto = {}  # decision_id -> model
    resolved = set()
    try:
        with open(AUDIT_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                outcome = row.get("outcome")
                if outcome == "injected":
                    dec = row.get("decision") or {}
                    if (
                        row.get("session_id") == session_id
                        and dec.get("band") == "auto"
                    ):
                        did = dec.get("decision_id")
                        if did:
                            session_auto[did] = dec.get("model")
                elif outcome in TERMINAL_OUTCOMES:
                    did = row.get("decision_id") or (row.get("decision") or {}).get(
                        "decision_id"
                    )
                    if did:
                        resolved.add(did)
    except OSError:
        return 0

    unresolved = [d for d in session_auto if d not in resolved]
    if not unresolved:
        return 0

    usage = sum_inline_usage(
        payload.get("transcript_path") or payload.get("transcriptPath")
    )

    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with open(AUDIT_LOG, "a") as f:
            for did in unresolved:
                row = {
                    "ts": ts,
                    "session_id": session_id,
                    "decision_id": did,
                    "model": session_auto[did],
                    "outcome": "auto_inline_unattributed",
                }
                if usage:
                    row["usage"] = usage
                f.write(json.dumps(row) + "\n")
    except OSError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
