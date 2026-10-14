#!/usr/bin/env python3
"""UserPromptSubmit hook: classify task, recommend model + effort, emit a
machine-readable <router-decision> block consumed by the
auto-model-routing skill.

Behaviour:
  - Harness events (task notifications, scheduled-task runs, system
    notifications) are not human prompts: the hook exits without classifying,
    injecting, or writing an audit row.
  - Heuristic-only classifier; a heuristic miss falls through to the
    project's default_model (audited, never injected) or a cheap-and-silent
    default. Short lookups stay inline: a cold worker cannot answer a
    continuity question and the brief costs more than the lookup.
  - Two bands only: `auto` at or above ROUTE_FLOOR, `none` below.
  - Downhill-only injection: the parent session's own model family is read
    from the transcript, and the AUTO-ROUTE instruction is emitted ONLY when
    the routed model is strictly cheaper (haiku < sonnet < opus < fable).
    A same-or-higher pick is audited and stays silent. Only an inline
    `#model=` tag may route up (the parent cannot change its own model);
    router.json rules get no exemption.
  - Effort (low/medium/high/xhigh) is scored independently from tier.
  - Plan mode comes from payload.permission_mode == "plan"; it emits a
    one-line pointer to the plan-with-models skill and nothing else.
  - Context budget: over CC_ROUTER_CTX_WARN input tokens the injection
    carries one extra warning line, throttled per session.
  - Audit-logs every decision to ~/.claude/cache/router/audit.jsonl; the
    full reason/source/tier live there, not in the injection.

Opt outs:
  - Per-prompt: append `#noshift` (or `#noroute`).
  - Global: env CC_ROUTER_DISABLE=1.
  - Override decision: prompt contains `#model=opus` / `#model=sonnet`
    / `#model=haiku` / `#model=fable`.
"""

import glob
import hashlib
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone

CACHE_DIR = os.path.expanduser(
    os.environ.get("CC_ROUTER_CACHE_DIR", "~/.claude/cache/router")
)
AUDIT_LOG = os.path.join(CACHE_DIR, "audit.jsonl")
PROJECT_CONFIG_FILENAME = ".claude/router.json"
# Overridable so tests never merge in the real ~/.claude/router.json.
GLOBAL_CONFIG_PATH = os.path.expanduser(
    os.environ.get("CC_ROUTER_GLOBAL_CONFIG", "~/.claude/router.json")
)

# Parent-session state read off the transcript.
TRANSCRIPT_TAIL_BYTES = 64 * 1024
DEFAULT_PARENT_MODEL = "opus"
# Claude Code writes zero-usage placeholder records (API errors, "No response
# requested.") under this model. They say nothing about the session's model.
SYNTHETIC_MODEL = "<synthetic>"

# Context budget: over this many input tokens (the auto-compact window) the
# injection carries one extra warning line, at most once every
# BUDGET_THROTTLE_PROMPTS prompts per session.
CTX_WARN_TOKENS = int(os.environ.get("CC_ROUTER_CTX_WARN", "600000"))
BUDGET_THROTTLE_PROMPTS = 10

# Canonical model tier table. effort is the effort the router records for a
# forced pick of that tier (the 5.5 models default to medium, Fable 5.1 to high;
# the worker agents pin their own effort in their frontmatter); agent is the
# router-<model> subagent; auto_routable is False for tiers the classifier may
# never auto-pick (manual dispatch only).
MODEL_TIERS = {
    "haiku": {
        "effort": "low",
        "agent": "router-haiku",
        "auto_routable": True,
    },
    "sonnet": {
        "effort": "medium",
        "agent": "router-sonnet",
        "auto_routable": True,
    },
    "opus": {
        "effort": "high",
        "agent": "router-opus",
        "auto_routable": True,
    },
    "fable": {
        "effort": "xhigh",
        "agent": "router-fable",
        "auto_routable": False,
    },
}

# Cost/capability rank, cheapest first — MODEL_TIERS is declared in that order.
# Injection is downhill-only: a routed model whose rank is >= the parent
# session's rank buys nothing, so the hook stays silent.
TIER_RANK = {name: i for i, name in enumerate(MODEL_TIERS)}

# Audit log rotation
AUDIT_ROTATE_BYTES = int(
    os.environ.get("CC_ROUTER_AUDIT_MAX_BYTES", str(5 * 1024 * 1024))
)
AUDIT_RETAIN_DAYS = int(os.environ.get("CC_ROUTER_AUDIT_RETAIN_DAYS", "30"))

# Everything at or above this confidence routes; below it the hook stays
# silent. The downhill-only gate makes a mid-confidence pick cheap, so there is
# no band in between.
ROUTE_FLOOR = float(os.environ.get("CC_ROUTER_ROUTE_FLOOR", "0.60"))


def read_session_state(transcript_path: str | None) -> tuple[str, int, int]:
    """Read the parent session's own state off its transcript.

    Returns (parent_model_family, context_tokens, turn_count):
      - parent_model_family: haiku/sonnet/opus/fable, from the model on the
        last assistant record in the tail window.
      - context_tokens: that record's input_tokens + cache_read + cache_creation
        — i.e. how full the parent's context window currently is.
      - turn_count: user records seen in the tail window (best effort).

    Only the last TRANSCRIPT_TAIL_BYTES are read, so cost is flat regardless of
    session length. Never raises: any problem yields the conservative default
    (assume an expensive parent, unknown context), which makes the caller
    inject less, not more.
    """
    try:
        if not transcript_path:
            return DEFAULT_PARENT_MODEL, 0, 0
        path = os.path.expanduser(transcript_path)
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TRANSCRIPT_TAIL_BYTES:
                f.seek(size - TRANSCRIPT_TAIL_BYTES)
                f.readline()  # discard the partial first line
            tail = f.read().decode("utf-8", errors="replace")

        turns = 0
        last_assistant = None
        for line in tail.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            message = rec.get("message")
            if not isinstance(message, dict):
                continue
            role = rec.get("type") or message.get("role")
            if role == "user":
                turns += 1
            elif (
                role == "assistant"
                and message.get("model")
                and message.get("model") != SYNTHETIC_MODEL
            ):
                last_assistant = message

        if last_assistant is None:
            return DEFAULT_PARENT_MODEL, 0, turns

        model = str(last_assistant.get("model", "")).lower()
        parent = next((fam for fam in TIER_RANK if fam in model), DEFAULT_PARENT_MODEL)

        ctx = 0
        usage = last_assistant.get("usage")
        if isinstance(usage, dict):
            for key in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            ):
                value = usage.get(key)
                if isinstance(value, (int, float)):
                    ctx += int(value)
        return parent, ctx, turns
    except Exception:
        return DEFAULT_PARENT_MODEL, 0, 0


def _read_router_json(path: str) -> dict:
    """Load one router.json file. Missing/invalid/non-dict -> {}. Never raises."""
    try:
        with open(path) as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _find_project_config_path(start_dir: str | None = None) -> str | None:
    """Walk up from start_dir looking for .claude/router.json; first hit wins.

    Stops before reaching the home directory — a router.json living directly
    under `~` IS the global config (see load_project_config), not a project
    override, so it is never returned here.
    """
    cur = os.path.abspath(start_dir or os.getcwd())
    home = os.path.expanduser("~")
    while cur != home:
        candidate = os.path.join(cur, PROJECT_CONFIG_FILENAME)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def load_project_config(start_dir: str | None = None) -> dict:
    """Merge the project's .claude/router.json (walking up from start_dir)
    with the global ~/.claude/router.json. Project rules take precedence;
    global rules fill in. Defensive throughout — a missing or invalid file on
    either side just contributes {}.

    Recognised keys (all optional):
      - disabled: bool — turn off routing for this project entirely.
      - default_model: "haiku"|"sonnet"|"opus" — the pick for prompts no
        heuristic matched. It is audited but never injected (it carries no
        per-prompt signal).
      - rules: list of {"match": <substring or regex>, "model": "...",
        "reason": "...", "regex": bool}. First match wins; checked against
        the lowercase prompt before heuristics. A rule's pick must still be
        strictly cheaper than the parent session to be injected.

    Merge semantics:
      - Scalar keys (disabled, default_model, ...): the project's value wins
        when present; otherwise the global value fills in.
      - rules: concatenated project-rules-then-global-rules, so
        `apply_project_rules`'s first-match-wins walk checks every project
        rule before falling through to global rules.
      - _source: the project file's path when one was found, else the global
        file's path when it exists, else absent.
    """
    project_path = _find_project_config_path(start_dir)
    project_cfg = _read_router_json(project_path) if project_path else {}
    global_path = GLOBAL_CONFIG_PATH
    global_cfg = _read_router_json(global_path)

    merged = dict(global_cfg)
    for key, value in project_cfg.items():
        if key != "rules":
            merged[key] = value
    merged["rules"] = list(project_cfg.get("rules") or []) + list(
        global_cfg.get("rules") or []
    )

    if project_path:
        merged["_source"] = project_path
    elif os.path.isfile(global_path):
        merged["_source"] = global_path
    return merged


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
            default_effort = MODEL_TIERS.get(model, {}).get("effort", "medium")
            return {
                "tier": "project_rule",
                "model": model,
                "effort": rule.get("effort", default_effort),
                "confidence": float(rule.get("confidence", 0.95)),
                "source": "project_config",
                "reasoning": rule.get("reason", f"project rule matched: {pattern}"),
            }
    return None


LOOKUP_VERBS = re.compile(
    r"^(what is|where is|is there|does|list|show|what|where|when|who|which|find|"
    r"count|how many|status|read|print|display|tell me|summari[sz]e|name |give me|"
    r"do you have|check|grep|search|ls|cat|open|view)\b",
    re.I,
)
COMPLEX_VERBS = re.compile(
    r"\b(refactor|redesign|migrate|integrate|implement|optimi[sz]e|rewrite|"
    r"restructure|port|extract|consolidate)\b",
    re.I,
)
# Breadth signals that keep a short "complex" verb on Opus rather than
# downshifting to Sonnet — e.g. "redesign the whole architecture" is not a
# light single-surface task even though it is short.
BREADTH_SIGNAL = re.compile(
    r"\b(architect\w*|whole|entire|subsystem|infrastructure|platform|"
    r"multi[- ]?tenant|end[- ]to[- ]end|system[- ]wide|codebase)\b",
    re.I,
)
DEEP_VERBS = re.compile(
    r"\b(plan|architect|design|investigate|brainstorm|analy[sz]e|audit|profile|"
    r"diagnose|root[- ]cause)\b",
    re.I,
)
OVERRIDE_PATTERN = re.compile(r"#model=(" + "|".join(MODEL_TIERS) + r")\b", re.I)
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


# Harness events reach UserPromptSubmit like typed prompts but are not: a
# background-task completion, a scheduled-task run, a system notification. They
# may arrive wrapped in <system-reminder>. Classifying them is pure noise.
HARNESS_EVENT = re.compile(
    r"\s*(?:<system-reminder>\s*)?"
    r"(?:<task-notification|<scheduled-task|\[SYSTEM NOTIFICATION)"
)


def is_harness_event(prompt: str) -> bool:
    return bool(HARNESS_EVENT.match(prompt))


# Session continuity: a short prompt opening with an acknowledgement or a
# back-reference is a follow-up on the work already in flight, not a new task.
# Re-routing it would strip the context the parent is holding, so it stays
# inline no matter what the classifier thinks.
FOLLOWUP = re.compile(
    r"^\s*(yes|ok|okay|continue|also|now|then|that|this|it|again|go|do it|"
    r"proceed|fix that|same)\b",
    re.I,
)
FOLLOWUP_MAX_WORDS = 8


def is_followup(prompt: str) -> bool:
    return len(prompt.split()) <= FOLLOWUP_MAX_WORDS and bool(FOLLOWUP.match(prompt))


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
        # Opus 5.5: start at `high`, not `xhigh`. `score_effort` still promotes
        # genuinely heavy work to xhigh; keeping the tier baseline at high
        # avoids a needless effort/tier gap that caps the deep pick's confidence.
        return {
            "tier": "deep",
            "model": "opus",
            "effort": "high",
            "confidence": conf,
            "source": "heuristic",
            "reasoning": "explicit design/plan/investigate intent",
        }
    if COMPLEX_VERBS.search(lower):
        if has_code or nontrivial or BREADTH_SIGNAL.search(lower):
            # Code-bearing, multi-file/long, or breadth-signalled complex work
            # (e.g. "redesign the whole architecture") stays on Opus.
            conf = 0.92 if has_code else 0.85
            return {
                "tier": "complex",
                "model": "opus",
                "effort": "high",
                "confidence": conf,
                "source": "heuristic",
                "reasoning": "complex task verb (refactor/implement/migrate/etc.)",
            }
        # Balanced rebalance: light, single-surface complex work goes to
        # Sonnet 5.5 (half the price of Opus 5.5). The router-sonnet worker
        # escalates via `Stopped:` if it discovers real depth.
        return {
            "tier": "complex",
            "model": "sonnet",
            "effort": "medium",
            "confidence": 0.82,
            "source": "heuristic",
            "reasoning": "light single-surface complex verb; Sonnet 5.5 (escalates if deep)",
        }
    if (
        words <= 20
        and chars <= 140
        and not has_code
        and lines <= 2
        and LOOKUP_VERBS.match(lower)
    ):
        # Short lookups are answered inline: the audit showed zero haiku
        # dispatches ever followed, and most hits were "status?"-style
        # continuity questions a cold worker cannot answer. No pick — the
        # caller falls through to its silent default. A user rule in
        # router.json can still route a lookup pattern explicitly.
        return None
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
    so model and effort can disagree — a large disagreement caps confidence,
    which is what pulls a shaky pick below ROUTE_FLOOR and silences it.
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
    return {
        "tier": "project_default",
        "model": pinned,
        "effort": MODEL_TIERS.get(pinned, {}).get("effort", "medium"),
        "confidence": 0.85,
        "source": "project_config",
        "reasoning": f"project default {pinned} ({why})",
    }


def _session_path(session_id: str) -> str:
    return os.path.join(CACHE_DIR, "sessions", f"{session_id}.json")


def _session_read(session_id: str) -> dict:
    try:
        with open(_session_path(session_id)) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}


def _session_write(session_id: str, state: dict) -> None:
    sessions_dir = os.path.join(CACHE_DIR, "sessions")
    try:
        os.makedirs(sessions_dir, exist_ok=True)
        with open(_session_path(session_id), "w") as f:
            json.dump(state, f)
    except OSError:
        pass


def session_bump(session_id: str) -> int:
    """Maintain CACHE_DIR/sessions/<session_id>.json {"turns": N, "ts": epoch}.

    Read, increment, and write the per-session turn counter on each invocation,
    opportunistically pruning session files older than 24h. Unrecognised keys
    (e.g. budget_turn) are preserved. All I/O is silent on failure.
    Returns the new turn count (0 on any I/O failure, so the caller treats it
    as no bump)."""
    sessions_dir = os.path.join(CACHE_DIR, "sessions")
    try:
        os.makedirs(sessions_dir, exist_ok=True)
        cutoff = time.time() - 24 * 3600
        for old in glob.glob(os.path.join(sessions_dir, "*.json")):
            try:
                if os.path.getmtime(old) < cutoff:
                    os.remove(old)
            except OSError:
                pass
        state = _session_read(session_id)
        try:
            turns = int(state.get("turns", 0)) + 1
        except (TypeError, ValueError):
            turns = 1
        state.update({"turns": turns, "ts": int(time.time())})
        _session_write(session_id, state)
        return turns
    except OSError:
        return 0


def budget_gate(session_id: str | None, turns: int) -> bool:
    """True when the context-budget line may fire on this prompt.

    Throttled to once every BUDGET_THROTTLE_PROMPTS prompts per session, using
    the same per-session state file as the turn counter. Without a session_id
    there is nothing to throttle against, so the warning always fires."""
    if not session_id:
        return True
    state = _session_read(session_id)
    last = state.get("budget_turn")
    if isinstance(last, int) and turns - last < BUDGET_THROTTLE_PROMPTS:
        return False
    state["budget_turn"] = turns
    _session_write(session_id, state)
    return True


def band_for(confidence: float) -> str:
    """Two bands: `auto` (routable) or `none` (silent).

    main() only injects when the routed model is strictly cheaper than the
    parent, so routing a mid-confidence pick is cheap and the one cutoff is
    ROUTE_FLOOR.
    """
    return "auto" if confidence >= ROUTE_FLOOR else "none"


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


def audit_decision(
    prompt: str,
    session_id: str | None,
    turns: int,
    decision: dict,
    outcome: str,
    hint: str | None = None,
) -> None:
    """Log one classified prompt. Every decision is logged, injected or not —
    outcome_hint says why a routable decision stayed silent."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "prompt_sha": hashlib.sha256(prompt.encode()).hexdigest()[:12],
        "session_id": session_id,
        "turns": turns,
        "decision": decision,
        "outcome": outcome,
    }
    if hint:
        record["outcome_hint"] = hint
    audit_append(record)


def emit_context(msg: str) -> None:
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
    if is_harness_event(prompt):
        return 0
    lower = prompt.lower()
    if "#noshift" in lower or "#noroute" in lower:
        return 0
    if (
        re.search(r"\bultracode\b", lower)
        or lower.startswith("/goal")
        or "/goal-driven" in lower
    ):
        os.makedirs(CACHE_DIR, exist_ok=True)
        audit_append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "prompt_sha": hashlib.sha256(prompt.encode()).hexdigest()[:12],
                "decision": {
                    "band": "none",
                    "tier": "optout",
                    "model": None,
                    "reason": "ultracode/goal-driven turn — must stay on parent",
                },
                "outcome": "silent",
            }
        )
        return 0
    if len(prompt.split()) < 3 and not prompt.endswith("?"):
        return 0

    project_cfg = load_project_config(payload.get("cwd"))
    if project_cfg.get("disabled") is True:
        return 0

    # Parent-session state: which model is holding this conversation, and how
    # full its context already is. Both gate the injection below.
    parent_model, ctx_tokens, _transcript_turns = read_session_state(
        payload.get("transcript_path")
    )
    ctx_k = ctx_tokens // 1000
    plan_mode = payload.get("permission_mode") == "plan"
    session_id = payload.get("session_id")
    turns = session_bump(str(session_id)) if session_id else 0

    # Session continuity: a short acknowledgement or back-reference continues
    # the work already in flight. The parent is holding that context; handing
    # it to a cold subagent costs more than it saves. An inline `#model=` tag is
    # an explicit instruction (and the only way to route up), so it is never
    # swallowed as a follow-up.
    if is_followup(prompt) and not OVERRIDE_PATTERN.search(prompt):
        audit_decision(
            prompt,
            session_id,
            turns,
            {
                "band": "none",
                "tier": "followup",
                "model": None,
                "parent": parent_model,
                "ctx": ctx_k,
                "plan_mode": plan_mode,
                "reason": "short follow-up on work in flight — stays inline",
            },
            "silent",
            "followup_inline",
        )
        return 0

    # Explicit override -> emit a high-confidence auto decision and skip classification
    override = OVERRIDE_PATTERN.search(prompt)
    if override:
        forced = override.group(1).lower()
        result = {
            "tier": "override",
            "model": forced,
            "effort": MODEL_TIERS[forced]["effort"],
            "confidence": 1.0,
            "source": "override",
            "reasoning": f"user override #model={forced}",
        }
    else:
        # Order: project_rule (explicit pattern) → heuristic → project_default
        # (only when the heuristic found nothing) → cheap-and-silent. A real
        # heuristic result (any tier, any confidence) is never overridden by
        # project_default — heuristics are microseconds, so there is nothing
        # to gain by second-guessing a low-confidence one.
        result = apply_project_rules(prompt, project_cfg)
        if result is None:
            result = classify_heuristic(prompt)
            if result is None:
                if project_cfg.get("default_model"):
                    result = _project_default_result(project_cfg, "no heuristic match")
                else:
                    result = {
                        "tier": "default",
                        "model": "haiku",
                        "effort": "low",
                        "confidence": 0.55,
                        "source": "default",
                        "reasoning": "no heuristic match; below route floor, stay inline",
                    }

    # Independent effort score: tier picks the model; effort comes from
    # work-shape signals. Adjacent disagreement (gap=1, e.g. tier wants
    # high but scorer says medium) is normal calibration — accept the
    # scored effort and keep the tier's confidence. Large disagreement
    # (gap>=2, e.g. tier wants xhigh but scorer says low) is a real
    # ambiguity signal — cap confidence so a shaky pick can fall silent.
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

    # Fable 5.1 is 2.5x the price of Opus 5.5 — manual dispatch only. Clamp a
    # classifier-picked fable down to opus unless the user explicitly asked for
    # it (#model=fable) or a project rule pinned it.
    if result.get("model") == "fable" and result.get("source") not in (
        "override",
        "project_config",
    ):
        result["model"] = "opus"

    model = result["model"]
    fanout, fanout_hint = detect_fanout(prompt)
    band = band_for(float(result.get("confidence", 0)))
    # default_model alone is no per-prompt signal (it was 55% of all injections
    # and rarely followed): record the decision, never inject it.
    pinned_default = result.get("tier") == "project_default"
    if pinned_default:
        band = "none"
    decision_id = "r_" + uuid.uuid4().hex[:10]

    decision = {
        "band": band,
        "model": model,
        "effort": result["effort"],
        "tier": result["tier"],
        "confidence": round(float(result.get("confidence", 0)), 2),
        "reason": result.get("reasoning", ""),
        "source": result.get("source", ""),
        "parent": parent_model,
        "ctx": ctx_k,
        "plan_mode": plan_mode,
        "fanout": fanout,
        "decision_id": decision_id,
        "project_config": project_cfg.get("_source"),
    }
    if fanout:
        decision["fanout_hint"] = fanout_hint

    # Plan mode owns the turn: the planner tags each step with its own
    # model/effort, so a whole-prompt routing decision would fight it.
    if plan_mode:
        audit_decision(prompt, session_id, turns, decision, "plan_mode")
        emit_context(
            "[auto-router] plan mode: use the plan-with-models skill; no delegation."
        )
        return 0

    if band == "none":
        audit_decision(
            prompt,
            session_id,
            turns,
            decision,
            "silent",
            "project_default_inline" if pinned_default else None,
        )
        return 0

    # Downhill-only: dispatching to the model the parent already runs (or a
    # dearer one) buys nothing and costs a cold-start brief. Audit it so the
    # analyzers still see the classification, but say nothing. The one exception
    # is an inline `#model=` tag: the parent cannot change its own model
    # mid-session, so the dispatch IS the escalation mechanism and swallowing it
    # would drop a direct instruction. router.json rules are standing config,
    # not a per-prompt instruction, so they pass the same gate as the
    # heuristics. Equal rank is always silent — there is nothing to escalate to.
    routed_rank = TIER_RANK.get(model, 99)
    parent_rank = TIER_RANK.get(parent_model, 99)
    explicit = result.get("source") == "override"
    if routed_rank == parent_rank or (routed_rank > parent_rank and not explicit):
        audit_decision(
            prompt, session_id, turns, decision, "silent", "same_or_higher_inline"
        )
        return 0

    budget_line = ""
    if ctx_tokens >= CTX_WARN_TOKENS and budget_gate(session_id, turns):
        budget_line = (
            f"\ncontext {ctx_k}k > {CTX_WARN_TOKENS // 1000}k: delegate routable "
            "work; consider /wrap-session + a fresh session."
        )
    fanout_clause = (
        " fanout: dispatch independent subtasks in one message." if fanout else ""
    )

    # The auto-model-routing skill carries the procedure (cold-start brief,
    # verify, escalate); the injection only names the worker and carries the
    # decision block the skill parses. Tier, source, reason, and confidence stay
    # in the audit row.
    msg = (
        f'AUTO-ROUTE: Agent(subagent_type="auto-model-router:router-{model}") '
        f"per auto-model-routing skill.{fanout_clause}{budget_line}\n"
        "<router-decision>"
        + json.dumps(
            {
                "decision_id": decision_id,
                "model": model,
                "effort": result["effort"],
                "band": band,
                "parent": parent_model,
                "ctx": ctx_k,
            },
            separators=(",", ":"),
        )
        + "</router-decision>"
    )

    audit_decision(prompt, session_id, turns, decision, "injected")
    emit_context(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
