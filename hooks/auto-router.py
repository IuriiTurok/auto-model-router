#!/usr/bin/env python3
"""UserPromptSubmit hook: classify task, recommend model + effort, emit a
machine-readable <router-decision> block consumed by the
auto-model-routing skill.

Behaviour:
  - Heuristic-first classifier; falls back to a Haiku API call when
    heuristics are uncertain and ANTHROPIC_API_KEY is set.
  - Results cached by SHA256(prompt) for 7 days under
    ~/.claude/cache/router/.
  - Confidence bands -> band field:
      auto   if confidence >= AUTO_THRESHOLD  (default 0.80)
      ask    if AUTO_THRESHOLD > confidence >= ASK_THRESHOLD  (default 0.50)
      none   otherwise (silent)
  - Plan-mode detection from payload markers; emits a different message
    instructing the planner to annotate steps with Model:/Effort: tags.
  - Audit-logs every fired decision to ~/.claude/cache/router/audit.jsonl.

Opt outs:
  - Per-prompt: append `#noshift` (or `#noroute`).
  - Global: env CC_ROUTER_DISABLE=1.
  - Override decision: prompt contains `#model=opus` / `#model=sonnet`
    / `#model=haiku`.
"""

import glob
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

CACHE_DIR = os.path.expanduser("~/.claude/cache/router")
AUDIT_LOG = os.path.join(CACHE_DIR, "audit.jsonl")
CACHE_TTL_SEC = 7 * 86400
HAIKU_TIMEOUT_SEC = 0.5
HAIKU_MODEL = "claude-haiku-4-5-20251001"
MAX_PROMPT_CHARS_TO_API = 2000
PROJECT_CONFIG_FILENAME = ".claude/router.json"

# Audit log rotation
AUDIT_ROTATE_BYTES = int(
    os.environ.get("CC_ROUTER_AUDIT_MAX_BYTES", str(5 * 1024 * 1024))
)
AUDIT_RETAIN_DAYS = int(os.environ.get("CC_ROUTER_AUDIT_RETAIN_DAYS", "30"))

DEFAULT_AUTO_THRESHOLD = float(os.environ.get("CC_ROUTER_AUTO_THRESHOLD", "0.80"))
DEFAULT_ASK_THRESHOLD = float(os.environ.get("CC_ROUTER_ASK_THRESHOLD", "0.50"))


def load_project_config(start_dir: str | None = None) -> dict:
    """Walk up from start_dir looking for .claude/router.json; first hit wins.

    Recognised keys (all optional):
      - disabled: bool — turn off routing for this project entirely.
      - default_model: "haiku"|"sonnet"|"opus" — pin every prompt to this
        model with band=auto unless overridden by #model= or #noshift.
      - auto_threshold / ask_threshold: float — per-project band cutoffs.
      - rules: list of {"match": <substring or regex>, "model": "...",
        "reason": "...", "regex": bool}. First match wins; checked
        against the lowercase prompt before heuristics.
    """
    start = os.path.abspath(start_dir or os.getcwd())
    cur = start
    home = os.path.expanduser("~")
    while True:
        candidate = os.path.join(cur, PROJECT_CONFIG_FILENAME)
        try:
            with open(candidate) as f:
                cfg = json.load(f)
            cfg["_source"] = candidate
            return cfg
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        parent = os.path.dirname(cur)
        if parent == cur or cur == home:
            break
        cur = parent
    return {}


def apply_project_rules(prompt: str, cfg: dict) -> dict | None:
    """Return a classification dict if any project rule matches, else None."""
    rules = cfg.get("rules") or []
    lower = prompt.lower()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("match")
        if not pattern:
            continue
        is_regex = bool(rule.get("regex"))
        matched = False
        if is_regex:
            try:
                matched = bool(re.search(pattern, lower, re.I))
            except re.error:
                matched = False
        else:
            matched = pattern.lower() in lower
        if matched:
            model = rule.get("model", "sonnet")
            effort_map = {"haiku": "low", "sonnet": "medium", "opus": "high"}
            return {
                "tier": "project_rule",
                "model": model,
                "effort": rule.get("effort", effort_map.get(model, "medium")),
                "confidence": float(rule.get("confidence", 0.95)),
                "source": "project_config",
                "reasoning": rule.get("reason", f"project rule matched: {pattern}"),
            }
    return None


LOOKUP_VERBS = re.compile(
    r"^(list|show|what|where|when|who|which|find|count|how many|status|read|print|"
    r"display|tell me|summari[sz]e|name |give me|do you have)\b",
    re.I,
)
COMPLEX_VERBS = re.compile(
    r"\b(refactor|redesign|migrate|integrate|implement|optimi[sz]e|rewrite|"
    r"restructure|port|extract|consolidate)\b",
    re.I,
)
DEEP_VERBS = re.compile(
    r"\b(plan|architect|design|investigate|brainstorm|analy[sz]e|audit|profile|"
    r"diagnose|root[- ]cause)\b",
    re.I,
)
OVERRIDE_PATTERN = re.compile(r"#model=(haiku|sonnet|opus)\b", re.I)


def classify_heuristic(prompt: str) -> dict | None:
    lower = prompt.lower()
    words = len(prompt.split())
    chars = len(prompt)
    lines = prompt.count("\n") + 1
    has_code = "```" in prompt
    nontrivial = words > 12 or has_code or lines > 3

    if DEEP_VERBS.search(lower):
        return {
            "tier": "deep",
            "model": "opus",
            "effort": "xhigh",
            "confidence": 0.85,
            "source": "heuristic",
            "reasoning": "explicit design/plan/investigate intent",
        }
    if COMPLEX_VERBS.search(lower):
        # Complex verbs strongly imply non-trivial scope on their own.
        # Calibrate confidence by length so a 5-word "refactor X" doesn't
        # auto-route as confidently as a paragraph-long refactor request.
        conf = 0.85 if nontrivial else 0.75
        return {
            "tier": "complex",
            "model": "opus",
            "effort": "high",
            "confidence": conf,
            "source": "heuristic",
            "reasoning": "complex task verb (refactor/implement/migrate/etc.)",
        }
    if (
        words <= 12
        and chars <= 80
        and not has_code
        and lines <= 2
        and LOOKUP_VERBS.match(lower)
    ):
        return {
            "tier": "trivial",
            "model": "haiku",
            "effort": "low",
            "confidence": 0.9,
            "source": "heuristic",
            "reasoning": "short lookup phrasing",
        }
    if 5 <= words <= 60 and not has_code and lines <= 5:
        # Catch-all for normal-length prompts without complex/deep verbs.
        return {
            "tier": "standard",
            "model": "sonnet",
            "effort": "medium",
            "confidence": 0.75,
            "source": "heuristic",
            "reasoning": "moderate scope, no complex signals",
        }
    return None


def classify_haiku(prompt: str) -> dict | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    system = (
        "Classify the user's coding task. Return ONLY a JSON object with keys: "
        "tier, model, effort, confidence, reasoning. "
        "tier in {trivial,standard,complex,deep}. "
        "model maps tier: trivial->haiku, standard->sonnet, complex->opus, deep->opus. "
        "effort maps tier: trivial->low, standard->medium, complex->high, deep->xhigh. "
        "confidence is a number 0-1. reasoning is one sentence, max 20 words."
    )
    payload = {
        "model": HAIKU_MODEL,
        "max_tokens": 200,
        "system": system,
        "messages": [{"role": "user", "content": prompt[:MAX_PROMPT_CHARS_TO_API]}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HAIKU_TIMEOUT_SEC) as resp:
            body = json.loads(resp.read())
        text = body["content"][0]["text"].strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
        result = json.loads(text)
        result["source"] = "haiku"
        return result
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError):
        return None


def cache_get(prompt: str) -> dict | None:
    h = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    path = os.path.join(CACHE_DIR, f"{h}.json")
    try:
        if time.time() - os.stat(path).st_mtime > CACHE_TTL_SEC:
            return None
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def cache_put(prompt: str, result: dict) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        h = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        with open(os.path.join(CACHE_DIR, f"{h}.json"), "w") as f:
            json.dump(result, f)
    except OSError:
        pass


def band_for(confidence: float, auto_threshold: float, ask_threshold: float) -> str:
    if confidence >= auto_threshold:
        return "auto"
    if confidence >= ask_threshold:
        return "ask"
    return "none"


def detect_plan_mode(payload: dict) -> bool:
    """Best-effort plan-mode detection from hook payload.

    Claude Code surfaces plan mode via system-reminder text in the
    transcript context. The hook only sees the prompt and a few payload
    fields; we look at any embedded markers conservatively.
    """
    raw = json.dumps(payload).lower()
    return (
        "plan mode is active" in raw
        or "exitplanmode" in raw
        or payload.get("plan_mode") is True
    )


def audit_rotate_if_needed() -> None:
    """Rotate audit.jsonl when it exceeds AUDIT_ROTATE_BYTES; prune rotated
    files older than AUDIT_RETAIN_DAYS. Silent on failure."""
    try:
        if os.path.exists(AUDIT_LOG) and os.path.getsize(AUDIT_LOG) >= AUDIT_ROTATE_BYTES:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            os.rename(AUDIT_LOG, f"{AUDIT_LOG}.{stamp}")
        cutoff = time.time() - AUDIT_RETAIN_DAYS * 86400
        for path in glob.glob(f"{AUDIT_LOG}.*"):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def audit_append(record: dict) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        audit_rotate_if_needed()
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def main() -> int:
    if os.environ.get("CC_ROUTER_DISABLE") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return 0
    lower = prompt.lower()
    if "#noshift" in lower or "#noroute" in lower:
        return 0
    if len(prompt.split()) < 3 and not prompt.endswith("?"):
        return 0

    project_cfg = load_project_config(payload.get("cwd"))
    if project_cfg.get("disabled") is True:
        return 0

    auto_threshold = float(project_cfg.get("auto_threshold", DEFAULT_AUTO_THRESHOLD))
    ask_threshold = float(project_cfg.get("ask_threshold", DEFAULT_ASK_THRESHOLD))

    # Explicit override -> emit a high-confidence auto decision and skip classification
    override = OVERRIDE_PATTERN.search(prompt)
    if override:
        forced = override.group(1).lower()
        effort_map = {"haiku": "low", "sonnet": "medium", "opus": "high"}
        result = {
            "tier": "override",
            "model": forced,
            "effort": effort_map[forced],
            "confidence": 1.0,
            "source": "override",
            "reasoning": f"user override #model={forced}",
        }
    else:
        # Project rules take precedence over heuristics but not over user overrides.
        result = apply_project_rules(prompt, project_cfg)
        if result is None and project_cfg.get("default_model"):
            pinned = project_cfg["default_model"]
            effort_map = {"haiku": "low", "sonnet": "medium", "opus": "high"}
            result = {
                "tier": "project_default",
                "model": pinned,
                "effort": effort_map.get(pinned, "medium"),
                "confidence": 0.95,
                "source": "project_config",
                "reasoning": f"project default_model={pinned} from {project_cfg.get('_source', '')}",
            }
        if result is None:
            result = cache_get(prompt)
            if result is None:
                result = classify_heuristic(prompt)
                if result is None or result.get("confidence", 0) < 0.7:
                    haiku = classify_haiku(prompt)
                    if haiku is not None:
                        result = haiku
                if result is None:
                    result = {
                        "tier": "standard",
                        "model": "sonnet",
                        "effort": "medium",
                        "confidence": 0.5,
                        "source": "default",
                        "reasoning": "no heuristic match; Haiku unavailable or timed out",
                    }
                cache_put(prompt, result)

    plan_mode = detect_plan_mode(payload)
    band = band_for(float(result.get("confidence", 0)), auto_threshold, ask_threshold)
    decision_id = "r_" + uuid.uuid4().hex[:10]

    decision = {
        "band": band,
        "model": result["model"],
        "effort": result["effort"],
        "tier": result["tier"],
        "confidence": round(float(result.get("confidence", 0)), 2),
        "reason": result.get("reasoning", ""),
        "source": result.get("source", ""),
        "plan_mode": plan_mode,
        "decision_id": decision_id,
        "thresholds": {"auto": auto_threshold, "ask": ask_threshold},
        "project_config": project_cfg.get("_source"),
    }

    # Suppress the silent band entirely; nothing to inject.
    if band == "none" and not plan_mode:
        audit_append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "prompt_sha": hashlib.sha256(prompt.encode()).hexdigest()[:12],
                "decision": decision,
                "outcome": "silent",
            }
        )
        return 0

    if plan_mode:
        instruction = (
            "Plan mode active. When you write the plan, every step MUST carry "
            "`Model:` (haiku/sonnet/opus) and `Effort:` (low/medium/high/xhigh) "
            "tags. The auto-model-router suggests the OVERALL plan tier is "
            f"{result['model']}/{result['effort']}, but individual steps may "
            "differ (cheap reads/edits = haiku/sonnet; deep refactor/debug = opus). "
            "Use the `plan-with-models` skill for the canonical step template."
        )
    elif band == "auto":
        instruction = (
            f"AUTO-ROUTE: dispatch this prompt to a `router-{result['model']}` "
            "subagent via the Agent tool instead of executing it inline. After "
            "the agent returns, summarise its result in 1-2 sentences. Skip "
            "delegation only if the task is genuinely trivial (single Read/Bash "
            "that takes <5 s). Use the `auto-model-routing` skill for the full "
            "procedure."
        )
    else:  # band == "ask"
        instruction = (
            f"AMBIGUOUS classification ({band}, confidence={decision['confidence']}). "
            f"Before doing the work, call AskUserQuestion with options "
            f"[Use {result['model']} (Recommended)] [Use Opus] [Stay on current]. "
            "Honour the answer. Use the `auto-model-routing` skill for the procedure."
        )

    msg = (
        f"[auto-router] tier={result['tier']} model={result['model']} "
        f"effort={result['effort']} confidence={decision['confidence']:.2f} "
        f"source={result.get('source', '')} band={band} plan_mode={plan_mode}\n"
        f"Reason: {result.get('reasoning', '')}\n\n"
        f"{instruction}\n\n"
        "<router-decision>\n"
        f"{json.dumps(decision)}\n"
        "</router-decision>\n"
        "(Override: `#model=opus|sonnet|haiku`. Suppress: `#noshift`.)"
    )

    audit_append(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "prompt_sha": hashlib.sha256(prompt.encode()).hexdigest()[:12],
            "decision": decision,
            "outcome": "injected",
        }
    )

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": msg,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
