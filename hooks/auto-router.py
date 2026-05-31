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
      auto   if confidence >= AUTO_THRESHOLD  (default 0.90)
      ask    if AUTO_THRESHOLD > confidence >= ASK_THRESHOLD  (default 0.60)
      none   otherwise (silent)
  - Effort (low/medium/high/xhigh) is scored independently from tier; when
    they disagree, confidence is capped to push the decision into ask band.
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

CACHE_DIR = os.path.expanduser(
    os.environ.get("CC_ROUTER_CACHE_DIR", "~/.claude/cache/router")
)
AUDIT_LOG = os.path.join(CACHE_DIR, "audit.jsonl")
CACHE_TTL_SEC = 7 * 86400
HAIKU_TIMEOUT_SEC = 1.5
HAIKU_MODEL = "claude-haiku-4-5-20251001"
MAX_PROMPT_CHARS_TO_API = 2000
PROJECT_CONFIG_FILENAME = ".claude/router.json"

# Bumped whenever classifier output or thresholds change; cache_get treats
# entries with a different version as a miss so old entries naturally expire.
CLASSIFIER_VERSION = 4

# Audit log rotation
AUDIT_ROTATE_BYTES = int(
    os.environ.get("CC_ROUTER_AUDIT_MAX_BYTES", str(5 * 1024 * 1024))
)
AUDIT_RETAIN_DAYS = int(os.environ.get("CC_ROUTER_AUDIT_RETAIN_DAYS", "30"))

DEFAULT_AUTO_THRESHOLD = float(os.environ.get("CC_ROUTER_AUTO_THRESHOLD", "0.90"))
DEFAULT_ASK_THRESHOLD = float(os.environ.get("CC_ROUTER_ASK_THRESHOLD", "0.60"))


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
FILE_PATH = re.compile(
    r"(?<![:\w])(?:~|\.{0,2}/)[\w./~-]+|\b[\w-]+\.(?:py|ts|tsx|js|jsx|md|json|yaml|yml|sh|rs|go|java|cpp|c|h)\b"
)
EFFORT_HEAVY_VERBS = re.compile(
    r"\b(refactor|migrate|investigate|audit|architect|integrate|restructure|"
    r"optimi[sz]e|across|consolidate|review|rewrite|redesign|implement|"
    r"port|extract|build)\b",
    re.I,
)
EFFORT_LEVELS = ("low", "medium", "high", "xhigh")
TEST_VERIFY = re.compile(r"\b(test|tests|verify|verification|spec|specs)\b", re.I)

NUMBERED_ITEM = re.compile(r"^\s*\d+[.)]\s+\S", re.M)
BULLET_ITEM = re.compile(r"^\s*[-*•]\s+\S", re.M)
FOR_EACH = re.compile(r"\bfor (each|all of|every|both)\b", re.I)
IMPERATIVE_VERB = re.compile(
    r"\b(add|update|fix|create|write|refactor|remove|delete|rename|bump|"
    r"implement|build|run|test|document|review|check|move|generate|wire|"
    r"replace|migrate|extract|install|configure|set up)\b",
    re.I,
)


def detect_fanout(prompt: str) -> tuple[bool, int]:
    """Detect a prompt that decomposes into independent subtasks.

    Conservative on purpose (auto-band fans out only on UNMISTAKABLE
    multi-part prompts per the project's routing policy). Returns
    (is_fanout, hint) where hint is a best-estimate subtask count, or 0
    when the count is unknown ("for each …" over an unenumerated set).
    """
    numbered = len(NUMBERED_ITEM.findall(prompt))
    if numbered >= 2:
        return True, numbered
    bullets = len(BULLET_ITEM.findall(prompt))
    if bullets >= 3:
        return True, bullets
    if FOR_EACH.search(prompt):
        return True, 0
    # ≥3 distinct imperative verbs joined by conjunctions in a single ask.
    has_conjunction = " and " in prompt.lower() or ";" in prompt
    verbs = {m.group(1).lower() for m in IMPERATIVE_VERB.finditer(prompt)}
    if has_conjunction and len(verbs) >= 3:
        return True, len(verbs)
    return False, 0


def classify_heuristic(prompt: str) -> dict | None:
    lower = prompt.lower()
    words = len(prompt.split())
    chars = len(prompt)
    lines = prompt.count("\n") + 1
    has_code = "```" in prompt
    has_path = bool(FILE_PATH.search(prompt))
    nontrivial = words > 12 or has_code or lines > 3

    if DEEP_VERBS.search(lower):
        # Long, file-anchored design/audit requests are highest-signal opus work.
        conf = 0.95 if words > 25 and has_path else 0.85
        return {
            "tier": "deep",
            "model": "opus",
            "effort": "xhigh",
            "confidence": conf,
            "source": "heuristic",
            "reasoning": "explicit design/plan/investigate intent",
        }
    if COMPLEX_VERBS.search(lower):
        if has_code:
            conf = 0.92
        elif nontrivial:
            conf = 0.85
        else:
            conf = 0.75
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
        # A trailing "?" on a short lookup is the clearest possible Haiku signal.
        conf = 0.95 if prompt.rstrip().endswith("?") else 0.90
        return {
            "tier": "trivial",
            "model": "haiku",
            "effort": "low",
            "confidence": conf,
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


def score_effort(prompt: str) -> tuple[str, float, str]:
    """Independent effort assessment from work-shape signals.

    Returns (effort_level, confidence, reason). Used after tier classification
    so model and effort can disagree — that disagreement caps confidence and
    drops the decision into the `ask` band for a human to break the tie.
    """
    lower = prompt.lower()
    words = len(prompt.split())
    lines = prompt.count("\n") + 1
    has_code = "```" in prompt
    paths = len(FILE_PATH.findall(prompt))

    score = 0
    if has_code:
        score += 2
    if paths >= 3:
        score += 2
    elif paths >= 1:
        score += 1
    if EFFORT_HEAVY_VERBS.search(lower):
        score += 2
    if words > 80:
        score += 1
    if lines > 6:
        score += 1
    if TEST_VERIFY.search(lower):
        score += 1
    if words < 10 and prompt.rstrip().endswith("?"):
        score -= 1

    # Confidence is highest when score is comfortably inside a bucket and
    # drops near the boundaries so disagreements with tier cap correctly.
    if score <= 0:
        return "low", 0.95, f"effort_score={score} (clearly light)"
    if score == 1:
        return "low", 0.85, f"effort_score={score} (light)"
    if score == 2:
        return "medium", 0.80, f"effort_score={score} (near low/medium boundary)"
    if score == 3:
        return "medium", 0.90, f"effort_score={score} (clearly medium)"
    if score == 4:
        return "high", 0.85, f"effort_score={score} (heavy)"
    if score == 5:
        return "high", 0.80, f"effort_score={score} (near high/xhigh boundary)"
    return "xhigh", 0.95, f"effort_score={score} (very heavy)"


def _project_default_result(project_cfg: dict, why: str) -> dict:
    pinned = project_cfg["default_model"]
    effort_map = {"haiku": "low", "sonnet": "medium", "opus": "high"}
    return {
        "tier": "project_default",
        "model": pinned,
        "effort": effort_map.get(pinned, "medium"),
        "confidence": 0.85,
        "source": "project_config",
        "reasoning": f"project default {pinned} ({why})",
    }


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
            data = json.load(f)
        if data.get("classifier_version") != CLASSIFIER_VERSION:
            return None
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def cache_put(prompt: str, result: dict) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        h = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        with open(os.path.join(CACHE_DIR, f"{h}.json"), "w") as f:
            json.dump({**result, "classifier_version": CLASSIFIER_VERSION}, f)
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

    Prefer a structured `plan_mode` field if Claude Code provides one;
    fall back to text markers in the JSON payload otherwise. The text
    fallback exists because earlier Claude Code releases only surface
    plan mode via system-reminder strings in the transcript context.
    """
    if payload.get("plan_mode") is True:
        return True
    raw = json.dumps(payload).lower()
    return "plan mode is active" in raw or "exitplanmode" in raw


def audit_rotate_if_needed() -> None:
    """Rotate audit.jsonl when it exceeds AUDIT_ROTATE_BYTES; prune rotated
    files older than AUDIT_RETAIN_DAYS. Silent on failure."""
    try:
        if (
            os.path.exists(AUDIT_LOG)
            and os.path.getsize(AUDIT_LOG) >= AUDIT_ROTATE_BYTES
        ):
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
        # Order: project_rule (explicit pattern) → cache → heuristic → Haiku
        # API → project_default (as bias on ambiguous standard, or as final
        # fallback) → cheap-and-ask. project_default is no longer a hard
        # short-circuit, which is what restores Haiku to its proper share
        # of trivial reads in projects with a sonnet default.
        result = apply_project_rules(prompt, project_cfg)
        if result is None:
            cached = cache_get(prompt)
            if cached is not None:
                result = cached
            else:
                result = classify_heuristic(prompt)
                if result is None or result.get("confidence", 0) < 0.7:
                    haiku = classify_haiku(prompt)
                    if haiku is not None:
                        result = haiku
                if (
                    result is not None
                    and result.get("tier") == "standard"
                    and result.get("confidence", 1.0) < 0.80
                    and project_cfg.get("default_model")
                ):
                    result = _project_default_result(
                        project_cfg,
                        f"bias on ambiguous standard: {result.get('reasoning', '')}",
                    )
                if result is None:
                    if project_cfg.get("default_model"):
                        result = _project_default_result(
                            project_cfg, "no heuristic match"
                        )
                    else:
                        result = {
                            "tier": "trivial",
                            "model": "haiku",
                            "effort": "low",
                            "confidence": 0.55,
                            "source": "default",
                            "reasoning": "no heuristic match; default cheap, ask user",
                        }
                cache_put(prompt, result)

    # Independent effort score: tier picks the model; effort comes from
    # work-shape signals. Adjacent disagreement (gap=1, e.g. tier wants
    # high but scorer says medium) is normal calibration — accept the
    # scored effort and keep the tier's confidence. Large disagreement
    # (gap>=2, e.g. tier wants xhigh but scorer says low) is a real
    # ambiguity signal — cap confidence so the decision drops into ask.
    if result.get("tier") not in ("override", "project_rule"):
        tier_effort = result.get("effort")
        scored_effort, effort_conf, effort_reason = score_effort(prompt)
        result["effort"] = scored_effort
        try:
            gap = abs(
                EFFORT_LEVELS.index(scored_effort) - EFFORT_LEVELS.index(tier_effort)
            )
        except ValueError:
            gap = 0
        if gap >= 2:
            result["confidence"] = min(float(result.get("confidence", 0.5)), 0.85)
            result["reasoning"] = (
                f"{result.get('reasoning', '')}; effort "
                f"{tier_effort}->{scored_effort}: {effort_reason}"
            )

    plan_mode = detect_plan_mode(payload)
    fanout, fanout_hint = detect_fanout(prompt)
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
        "fanout": fanout,
        "decision_id": decision_id,
        "thresholds": {"auto": auto_threshold, "ask": ask_threshold},
        "project_config": project_cfg.get("_source"),
    }
    if fanout:
        decision["fanout_hint"] = fanout_hint

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
    elif band == "auto" and fanout:
        instruction = (
            f"AUTO-ROUTE + FAN-OUT: this prompt looks decomposable (~{fanout_hint} "
            "independent subtasks). Follow Branch E of the `auto-model-routing` "
            "skill: split it into subtasks, classify each to the cheapest "
            "sufficient model, and dispatch the independent ones as ONE message of "
            "concurrent Agent() calls — but only parallelise writers whose file "
            "sets are disjoint (read-only subtasks are always safe). Then "
            "synthesise the results in 1-2 sentences."
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
        fan_note = (
            " (This prompt also looks decomposable — if you delegate, consider "
            "Branch E fan-out across independent subtasks.)"
            if fanout
            else ""
        )
        instruction = (
            f"AMBIGUOUS classification ({band}, confidence={decision['confidence']}). "
            f"Before doing the work, call AskUserQuestion with options "
            f"[Use {result['model']} (Recommended)] [Use Opus] [Stay on current]. "
            f"Honour the answer. Use the `auto-model-routing` skill for the procedure.{fan_note}"
        )

    # Plan mode is parent-authoritative: the hook cannot see it reliably (the
    # UserPromptSubmit payload carries no plan-mode flag), so remind the parent
    # to trust its own context over the best-effort plan_mode field below.
    if not plan_mode:
        instruction += (
            "\n\nIf you are actually in PLAN MODE right now (a system reminder "
            "says so), ignore the routing above and use the `plan-with-models` "
            "skill instead — the plan_mode field below is best-effort and is "
            "often stale."
        )

    fanout_tag = f" fanout={fanout_hint or 'y'}" if fanout else ""
    msg = (
        f"[auto-router] tier={result['tier']} model={result['model']} "
        f"effort={result['effort']} confidence={decision['confidence']:.2f} "
        f"source={result.get('source', '')} band={band} plan_mode={plan_mode}"
        f"{fanout_tag}\n"
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
