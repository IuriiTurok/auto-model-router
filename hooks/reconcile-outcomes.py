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
                    if row.get("session_id") == session_id and dec.get("band") == "auto":
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

    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with open(AUDIT_LOG, "a") as f:
            for did in unresolved:
                f.write(
                    json.dumps(
                        {
                            "ts": ts,
                            "session_id": session_id,
                            "decision_id": did,
                            "model": session_auto[did],
                            "outcome": "auto_inline_unattributed",
                        }
                    )
                    + "\n"
                )
    except OSError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
