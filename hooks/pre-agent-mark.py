#!/usr/bin/env python3
"""PreToolUse hook for Agent calls — writes a start-time marker keyed by
tool_use_id so the matching PostToolUse hook (post-agent-audit.py) can
compute wall-clock duration.

Skips non-router subagent dispatches. Idempotent and silent on any error.
"""

import json
import os
import sys
import time

CACHE_DIR = os.path.expanduser(
    os.environ.get("CC_ROUTER_CACHE_DIR", "~/.claude/cache/router")
)
MARK_DIR = os.path.join(CACHE_DIR, "agent-marks")


def main() -> int:
    if os.environ.get("CC_ROUTER_DISABLE") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool = (
        payload.get("tool_name") or payload.get("toolName") or payload.get("tool") or ""
    )
    if tool != "Agent":
        return 0
    tool_input = payload.get("toolInput") or payload.get("tool_input") or {}
    if not (tool_input.get("subagent_type") or "").startswith("router-"):
        return 0
    tool_use_id = payload.get("toolUseId") or payload.get("tool_use_id")
    if not tool_use_id:
        return 0
    try:
        os.makedirs(MARK_DIR, exist_ok=True)
        with open(os.path.join(MARK_DIR, f"{tool_use_id}.ts"), "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
