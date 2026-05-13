#!/usr/bin/env python3
"""PostToolUse hook: when an Agent tool call dispatches a `router-*`
subagent, append an outcome line to the audit log so /route-status can
distinguish *classifier recommendations* from *actually-acted-on*
delegations.

Closes the loop with auto-router.py:
  - auto-router.py logs `outcome: "injected"` when it emits a decision.
  - This hook logs `outcome: "delegated"` (with model + ok/failed +
    response length) every time the parent dispatches a router-* worker.

Idempotent and silent — never blocks the tool call. If the payload
shape is not what we expect, exits 0 without writing anything.
"""
import json
import os
import sys
from datetime import datetime, timezone

CACHE_DIR = os.path.expanduser("~/.claude/cache/router")
AUDIT_LOG = os.path.join(CACHE_DIR, "audit.jsonl")


def main() -> int:
    if os.environ.get("CC_ROUTER_DISABLE") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    tool = payload.get("toolName") or payload.get("tool") or ""
    if tool != "Agent":
        return 0

    tool_input = payload.get("toolInput") or payload.get("tool_input") or {}
    subagent_type = (tool_input.get("subagent_type") or "").strip()
    if not subagent_type.startswith("router-"):
        return 0

    model = subagent_type[len("router-"):] or "?"
    tool_response = payload.get("toolResponse") or payload.get("tool_response") or {}
    # Heuristic ok/failed: presence of an "error" field, or response truthiness.
    ok = True
    if isinstance(tool_response, dict):
        if tool_response.get("error") or tool_response.get("is_error"):
            ok = False
    elif not tool_response:
        ok = False

    # Best-effort response length for inspection
    try:
        resp_len = len(json.dumps(tool_response)) if tool_response else 0
    except (TypeError, ValueError):
        resp_len = 0

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "outcome": "delegated" if ok else "delegated_failed",
        "subagent_type": subagent_type,
        "model": model,
        "response_chars": resp_len,
    }
    desc = tool_input.get("description")
    if desc:
        record["description"] = str(desc)[:80]

    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
