#!/usr/bin/env python3
"""PostToolUse hook: when an Agent tool call dispatches a `router-*`
subagent, append an outcome line to the audit log so /route-status can
distinguish *classifier recommendations* from *actually-acted-on*
delegations.

Closes the loop with auto-router.py:
  - auto-router.py logs `outcome: "injected"` when it emits a decision.
  - This hook logs `outcome: "delegated"` (with model + ok/failed +
    response length + tokens + wall time + escalation signal + the
    decision_id it joined to) every time the parent dispatches a
    router-* worker. The outcome rows can then be JOINed against the
    decision rows for downstream analysis.

Idempotent and silent — never blocks the tool call. If the payload
shape is not what we expect, exits 0 without writing anything.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

CACHE_DIR = os.path.expanduser(
    os.environ.get("CC_ROUTER_CACHE_DIR", "~/.claude/cache/router")
)
AUDIT_LOG = os.path.join(CACHE_DIR, "audit.jsonl")
MARK_DIR = os.path.join(CACHE_DIR, "agent-marks")

ESCALATION_PATTERNS = re.compile(
    r"(Stopped: too complex|Done with caveats|RESULT:|OPEN QUESTIONS:|"
    r"ARTIFACTS:|NEXT STEP:)",
    re.M,
)


def find_last_injected_decision_id() -> str | None:
    """Best-effort JOIN key: the decision_id of the most recent injected
    decision in audit.jsonl. Reads only the tail of the file to stay cheap.

    Under sequential usage this is exact; under parallel Agent dispatch
    (rare) the join may be off by one — acceptable for a feedback signal.
    """
    try:
        with open(AUDIT_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if rec.get("outcome") == "injected":
                return (rec.get("decision") or {}).get("decision_id")
    except (OSError, ValueError):
        pass
    return None


def extract_response_text(tool_response) -> str:
    """Pull a text-y view of the subagent's response, however it's shaped."""
    if isinstance(tool_response, str):
        return tool_response
    if not isinstance(tool_response, dict):
        return ""
    content = tool_response.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        if parts:
            return "".join(parts)
    for key in ("output", "text", "result", "message"):
        val = tool_response.get(key)
        if isinstance(val, str):
            return val
    return ""


def extract_usage(tool_response) -> dict:
    """Pull usage fields from common shapes; returns {} when none found."""
    if not isinstance(tool_response, dict):
        return {}
    u = tool_response.get("usage") or tool_response.get("Usage")
    if isinstance(u, dict):
        candidates = {
            "tokens_in": u.get("input_tokens") or u.get("inputTokens"),
            "tokens_out": u.get("output_tokens") or u.get("outputTokens"),
            "cache_read": (
                u.get("cache_read_input_tokens") or u.get("cacheReadInputTokens")
            ),
            "cache_write": (
                u.get("cache_creation_input_tokens")
                or u.get("cacheCreationInputTokens")
            ),
        }
        return {k: v for k, v in candidates.items() if v is not None}
    return {}


def read_wall_ms(tool_use_id: str | None) -> int | None:
    if not tool_use_id:
        return None
    mark_path = os.path.join(MARK_DIR, f"{tool_use_id}.ts")
    try:
        with open(mark_path) as f:
            started = float(f.read().strip())
        wall_ms = int((time.time() - started) * 1000)
        try:
            os.remove(mark_path)
        except OSError:
            pass
        return wall_ms
    except (OSError, ValueError):
        return None


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
    subagent_type = (tool_input.get("subagent_type") or "").strip()
    if not subagent_type.startswith("router-"):
        return 0

    model = subagent_type[len("router-") :] or "?"
    tool_response = payload.get("toolResponse") or payload.get("tool_response") or {}

    ok = True
    if isinstance(tool_response, dict):
        if tool_response.get("error") or tool_response.get("is_error"):
            ok = False
    elif not tool_response:
        ok = False

    try:
        resp_len = len(json.dumps(tool_response)) if tool_response else 0
    except (TypeError, ValueError):
        resp_len = 0

    resp_text = extract_response_text(tool_response)
    escalation = None
    if resp_text:
        m = ESCALATION_PATTERNS.search(resp_text)
        if m:
            escalation = m.group(1)

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "outcome": "delegated" if ok else "delegated_failed",
        "subagent_type": subagent_type,
        "model": model,
        "response_chars": resp_len,
        "decision_id": find_last_injected_decision_id(),
    }

    wall_ms = read_wall_ms(payload.get("toolUseId") or payload.get("tool_use_id"))
    if wall_ms is not None:
        record["wall_ms"] = wall_ms

    usage = extract_usage(tool_response)
    if usage:
        record["usage"] = usage

    if escalation:
        record["escalation"] = escalation

    desc = tool_input.get("description")
    if desc:
        desc = str(desc)
        # Parallel-batch correlation: plan-with-models / Branch E prefix the
        # description with "[grp:<id>]" so concurrent dispatches in one batch
        # share a group_id. Lets the analyzer measure fan-out width precisely.
        m = re.match(r"\s*\[grp:([^\]]+)\]\s*", desc)
        if m:
            record["group_id"] = m.group(1).strip()
            desc = desc[m.end() :]
        record["description"] = desc[:80]

    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
