#!/usr/bin/env python3
"""Parent-session-state tests for hooks/auto-router.py.

Covers what decides whether the hook says anything at all: harness events
(skipped outright), transcript parsing (parent model + context size), the
downhill-only injection gate (only an inline `#model=` may route up; router.json
rules and default_model may not), and the plan-mode / context-budget lines.
Uses a temp CC_ROUTER_CACHE_DIR so it never touches the real cache. Exit 0 if
all pass; 1 otherwise.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "hooks", "auto-router.py")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# Prompts whose classification is pinned by tests/fixtures.jsonl.
SONNET_PROMPT = "refactor the rate limiter"
OPUS_PROMPT = (
    "design a new auth flow that supports SSO, magic links, and passkeys "
    "across api/auth.py, ui/login.tsx, and tests/test_auth.py — include the "
    "migration story and a rollback plan"
)
LOOKUP_PROMPT = "list open PRs"

MAX_INJECTION_CHARS = 120


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("auto_router_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


router = _load_hook_module()


def _run(payload: dict, cache_dir: str) -> str:
    env = {
        **os.environ,
        "CC_ROUTER_CACHE_DIR": cache_dir,
        # Nonexistent by default so this never merges in the developer's real
        # ~/.claude/router.json.
        "CC_ROUTER_GLOBAL_CONFIG": os.environ.get(
            "CC_ROUTER_GLOBAL_CONFIG",
            os.path.join(cache_dir, "no-such-global-router.json"),
        ),
    }
    return subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    ).stdout


def _context(stdout: str) -> str | None:
    """The injected additionalContext, or None when the hook stayed silent."""
    if not stdout.strip():
        return None
    return json.loads(stdout)["hookSpecificOutput"]["additionalContext"]


def _prose(ctx: str) -> str:
    """Everything the parent reads, i.e. minus the machine-readable block."""
    return ctx.split("<router-decision>")[0].rstrip("\n")


def _audit(cache_dir: str) -> list[dict]:
    path = os.path.join(cache_dir, "audit.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


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


# ---------------------------------------------------------------------------
# read_session_state
# ---------------------------------------------------------------------------
parent, ctx_tokens, turns = router.read_session_state(
    os.path.join(FIXTURES, "parent-opus.jsonl")
)
check(
    "read_session_state: opus transcript -> ('opus', 12004, 2)",
    (parent, ctx_tokens, turns) == ("opus", 12004, 2),
)
check(
    "read_session_state: sonnet transcript -> sonnet",
    router.read_session_state(os.path.join(FIXTURES, "parent-sonnet.jsonl"))[0]
    == "sonnet",
)
check(
    "read_session_state: 250k transcript -> 252006 tokens",
    router.read_session_state(os.path.join(FIXTURES, "parent-opus-250k.jsonl"))[1]
    == 252006,
)
check(
    "read_session_state: missing file -> ('opus', 0, 0)",
    router.read_session_state("/nonexistent/transcript.jsonl") == ("opus", 0, 0),
)
check(
    "read_session_state: no path -> ('opus', 0, 0)",
    router.read_session_state(None) == ("opus", 0, 0),
)
check(
    "read_session_state: malformed lines never raise",
    router.read_session_state(os.path.join(FIXTURES, "parent-malformed.jsonl"))
    == ("opus", 0, 0),
)
# A trailing `<synthetic>` record (zero usage: an API error or "No response
# requested.") must not be read as the parent — it would turn a sonnet session
# into 'opus' with ctx 0.
check(
    "read_session_state: a trailing <synthetic> record is skipped -> ('sonnet', 8505, 1)",
    router.read_session_state(os.path.join(FIXTURES, "parent-sonnet-synthetic.jsonl"))
    == ("sonnet", 8505, 1),
)
check(
    "read_session_state: a synthetic-only transcript -> the default ('opus', 0, 1)",
    router.read_session_state(os.path.join(FIXTURES, "parent-synthetic-only.jsonl"))
    == ("opus", 0, 1),
)

# ---------------------------------------------------------------------------
# load_project_config: project + global router.json merge
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    project_dir = os.path.join(tmp, "project")
    os.makedirs(os.path.join(project_dir, ".claude"))
    project_router_path = os.path.join(project_dir, ".claude", "router.json")
    with open(project_router_path, "w") as f:
        json.dump(
            {
                "default_model": "sonnet",
                "rules": [
                    {"match": "design", "model": "opus", "reason": "project rule"}
                ],
            },
            f,
        )
    global_router_path = os.path.join(tmp, "global-router.json")
    with open(global_router_path, "w") as f:
        json.dump(
            {
                "default_model": "opus",
                "disabled": False,
                "rules": [{"match": "lint", "model": "haiku", "reason": "global rule"}],
            },
            f,
        )

    orig_global_path = router.GLOBAL_CONFIG_PATH
    router.GLOBAL_CONFIG_PATH = global_router_path
    try:
        merged = router.load_project_config(project_dir)
        no_project = router.load_project_config(os.path.join(tmp, "no-project"))
    finally:
        router.GLOBAL_CONFIG_PATH = orig_global_path

    check(
        "merge: project scalar overrides global (default_model=sonnet)",
        merged.get("default_model") == "sonnet",
    )
    check(
        "merge: global-only scalar fills through (disabled=False)",
        merged.get("disabled") is False,
    )
    check(
        "merge: rules = project rules then global rules (project wins first-match)",
        [r["model"] for r in merged.get("rules", [])] == ["opus", "haiku"],
    )
    check(
        "merge: _source points at the project file when one is found",
        merged.get("_source") == project_router_path,
    )
    check(
        "merge: no project file -> global fills entirely",
        no_project.get("default_model") == "opus"
        and no_project.get("disabled") is False
        and [r["model"] for r in no_project.get("rules", [])] == ["haiku"],
    )
    check(
        "merge: no project file -> _source is the global path",
        no_project.get("_source") == global_router_path,
    )

with tempfile.TemporaryDirectory() as tmp:
    # Both files missing/invalid -> {} with no _source, never raises.
    router.GLOBAL_CONFIG_PATH = os.path.join(tmp, "no-such-global.json")
    try:
        empty = router.load_project_config(os.path.join(tmp, "no-project"))
    finally:
        router.GLOBAL_CONFIG_PATH = orig_global_path
    check(
        "merge: both files missing -> rules=[] and no _source",
        empty.get("rules") == [] and "_source" not in empty,
    )

# ---------------------------------------------------------------------------
# Downhill-only injection gate
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as cache_dir:
    ctx = _context(
        _run(
            {
                "session_id": "s-cheaper",
                "transcript_path": os.path.join(FIXTURES, "parent-opus.jsonl"),
                "cwd": "/tmp",
                "permission_mode": "default",
                "prompt": SONNET_PROMPT,
            },
            cache_dir,
        )
    )
    check("sonnet pick under opus parent injects", ctx is not None)
    if ctx:
        check(
            "injection leads with AUTO-ROUTE and points at the skill",
            ctx.startswith("AUTO-ROUTE: ")
            and "auto-model-routing skill" in _prose(ctx)
            and "conf=" not in ctx
            and "[auto-router]" not in ctx,
        )
        check(
            "injection names the router-sonnet subagent",
            'Agent(subagent_type="auto-model-router:router-sonnet")' in ctx,
        )
        check(
            f"injection prose <= {MAX_INJECTION_CHARS} chars (got {len(_prose(ctx))})",
            len(_prose(ctx)) <= MAX_INJECTION_CHARS,
        )
        block = ctx.split("<router-decision>")[1].split("</router-decision>")[0]
        dec = json.loads(block)
        check(
            "decision block is decision_id/model/effort/band/parent/ctx only",
            set(dec) == {"decision_id", "model", "effort", "band", "parent", "ctx"}
            and dec["model"] == "sonnet"
            and dec["band"] == "auto"
            and dec["parent"] == "opus"
            and dec["ctx"] == 12,
        )
        check(
            "no ask-band / plan-disclaimer prose survives",
            "AskUserQuestion" not in ctx and "best-effort" not in ctx,
        )

with tempfile.TemporaryDirectory() as cache_dir:
    stdout = _run(
        {
            "session_id": "s-same",
            "transcript_path": os.path.join(FIXTURES, "parent-opus.jsonl"),
            "cwd": "/tmp",
            "permission_mode": "default",
            "prompt": OPUS_PROMPT,
        },
        cache_dir,
    )
    rows = _audit(cache_dir)
    check("opus pick under opus parent is silent", stdout.strip() == "")
    check(
        "opus pick under opus parent audits same_or_higher_inline",
        bool(rows) and rows[-1].get("outcome_hint") == "same_or_higher_inline",
    )
    check(
        "suppressed row still carries the full decision",
        bool(rows)
        and rows[-1]["decision"]["model"] == "opus"
        and rows[-1]["decision"]["band"] == "auto"
        and rows[-1]["decision"]["parent"] == "opus",
    )

with tempfile.TemporaryDirectory() as cache_dir:
    # An explicit escalation is the one thing that may route UP: the parent
    # cannot switch its own model, so the dispatch is the only mechanism.
    ctx = _context(
        _run(
            {
                "session_id": "s-override-up",
                "transcript_path": os.path.join(FIXTURES, "parent-opus.jsonl"),
                "cwd": "/tmp",
                "permission_mode": "default",
                "prompt": "#model=fable redesign the persona isolation boundary",
            },
            cache_dir,
        )
    )
    check(
        "#model=fable under opus parent injects (explicit escalation)",
        ctx is not None
        and 'Agent(subagent_type="auto-model-router:router-fable")' in ctx,
    )
    silent = _run(
        {
            "session_id": "s-override-same",
            "transcript_path": os.path.join(FIXTURES, "parent-opus.jsonl"),
            "cwd": "/tmp",
            "permission_mode": "default",
            "prompt": "#model=opus redesign the persona isolation boundary",
        },
        cache_dir,
    )
    check("#model=opus under opus parent is silent", silent.strip() == "")

with tempfile.TemporaryDirectory() as cache_dir:
    stdout = _run(
        {
            "session_id": "s-sonnet-parent",
            "transcript_path": os.path.join(FIXTURES, "parent-sonnet.jsonl"),
            "cwd": "/tmp",
            "permission_mode": "default",
            "prompt": SONNET_PROMPT,
        },
        cache_dir,
    )
    check("sonnet pick under sonnet parent is silent", stdout.strip() == "")

with tempfile.TemporaryDirectory() as cache_dir:
    # The same sonnet session, but its transcript ends in a `<synthetic>`
    # record. Misread as an opus parent it would inject a sonnet route.
    stdout = _run(
        {
            "session_id": "s-sonnet-synthetic",
            "transcript_path": os.path.join(FIXTURES, "parent-sonnet-synthetic.jsonl"),
            "cwd": "/tmp",
            "permission_mode": "default",
            "prompt": SONNET_PROMPT,
        },
        cache_dir,
    )
    rows = _audit(cache_dir)
    check(
        "sonnet pick under a sonnet parent with a trailing <synthetic> record is silent",
        stdout.strip() == ""
        and bool(rows)
        and rows[-1]["decision"]["parent"] == "sonnet"
        and rows[-1]["decision"]["ctx"] == 8,
    )

with tempfile.TemporaryDirectory() as cache_dir:
    ctx = _context(
        _run(
            {
                "session_id": "s-haiku",
                "transcript_path": os.path.join(FIXTURES, "parent-sonnet.jsonl"),
                "cwd": "/tmp",
                "permission_mode": "default",
                "prompt": f"#model=haiku {LOOKUP_PROMPT}",
            },
            cache_dir,
        )
    )
    check(
        "#model=haiku under sonnet parent injects",
        ctx is not None
        and 'Agent(subagent_type="auto-model-router:router-haiku")' in ctx
        and '"parent":"sonnet"' in ctx,
    )

# ---------------------------------------------------------------------------
# Short lookups stay inline (the trivial -> haiku routing is retired)
# ---------------------------------------------------------------------------
for parent_fixture in ("parent-opus.jsonl", "parent-sonnet.jsonl"):
    with tempfile.TemporaryDirectory() as cache_dir:
        for lookup in (LOOKUP_PROMPT, "status?", "where is the config loaded?"):
            stdout = _run(
                {
                    "session_id": "s-lookup",
                    "transcript_path": os.path.join(FIXTURES, parent_fixture),
                    "cwd": "/tmp",
                    "permission_mode": "default",
                    "prompt": lookup,
                },
                cache_dir,
            )
            check(f"{lookup!r} under {parent_fixture} is silent", stdout.strip() == "")
        rows = _audit(cache_dir)
        check(
            f"lookups under {parent_fixture} audit band none, never a haiku route",
            len(rows) == 3
            and all(r["decision"]["band"] == "none" for r in rows)
            and all(r["outcome"] == "silent" for r in rows)
            and all(r["decision"]["tier"] == "default" for r in rows),
        )

# ---------------------------------------------------------------------------
# Harness events are not prompts: no classification, no injection, no audit row
# ---------------------------------------------------------------------------
HARNESS_EVENTS = {
    "task-notification": (
        "<task-notification>\n<task-id>a2d809a4adfcf39c4</task-id>\n"
        "<status>completed</status>\n"
        '<summary>Agent "Refresh brandbook router" completed</summary>\n'
        "<result>Done: refactor the rate limiter</result>\n</task-notification>"
    ),
    "leading whitespace": f"  \n\t<task-notification>{SONNET_PROMPT}</task-notification>",
    "system-reminder wrapper": (
        f"<system-reminder>\n<task-notification>{SONNET_PROMPT}</task-notification>"
        "\n</system-reminder>"
    ),
    "scheduled-task": f'<scheduled-task name="nightly-digest">{SONNET_PROMPT}</scheduled-task>',
    "system notification": f"[SYSTEM NOTIFICATION - background job done] {SONNET_PROMPT}",
}
for label, event in HARNESS_EVENTS.items():
    with tempfile.TemporaryDirectory() as cache_dir:
        stdout = _run(
            {
                "session_id": "s-harness",
                "transcript_path": os.path.join(FIXTURES, "parent-opus.jsonl"),
                "cwd": "/tmp",
                "permission_mode": "default",
                "prompt": event,
            },
            cache_dir,
        )
        check(f"harness event ({label}) injects nothing", stdout.strip() == "")
        check(
            f"harness event ({label}) writes no audit row and no session state",
            not os.path.exists(os.path.join(cache_dir, "audit.jsonl"))
            and not os.path.exists(os.path.join(cache_dir, "sessions")),
        )

with tempfile.TemporaryDirectory() as cache_dir:
    # Control: the same body typed by a human routes, and a prompt that merely
    # mentions the tag mid-sentence is still classified (detection is anchored
    # at the start).
    typed = _context(
        _run(
            {
                "session_id": "s-typed",
                "transcript_path": os.path.join(FIXTURES, "parent-opus.jsonl"),
                "cwd": "/tmp",
                "permission_mode": "default",
                "prompt": SONNET_PROMPT,
            },
            cache_dir,
        )
    )
    mention = _context(
        _run(
            {
                "session_id": "s-mention",
                "transcript_path": os.path.join(FIXTURES, "parent-opus.jsonl"),
                "cwd": "/tmp",
                "permission_mode": "default",
                "prompt": "refactor the handler that parses <task-notification> blocks",
            },
            cache_dir,
        )
    )
    check("control: the typed prompt injects", typed is not None)
    check(
        "control: a mid-sentence tag mention is still classified", mention is not None
    )

# ---------------------------------------------------------------------------
# router.json: default_model is audited but never injected; rules obey the
# downhill gate (only an inline #model= tag may route up)
# ---------------------------------------------------------------------------
UNMATCHED_PROMPT = "rename that function now"  # no heuristic tier matches


def _project(tmp: str, cfg: dict) -> str:
    project_dir = os.path.join(tmp, "project")
    os.makedirs(os.path.join(project_dir, ".claude"), exist_ok=True)
    with open(os.path.join(project_dir, ".claude", "router.json"), "w") as f:
        json.dump(cfg, f)
    return project_dir


with tempfile.TemporaryDirectory() as tmp:
    cache_dir = os.path.join(tmp, "cache")
    project_dir = _project(tmp, {"default_model": "sonnet"})
    stdout = _run(
        {
            "session_id": "s-default",
            "transcript_path": os.path.join(FIXTURES, "parent-opus.jsonl"),
            "cwd": project_dir,
            "permission_mode": "default",
            "prompt": UNMATCHED_PROMPT,
        },
        cache_dir,
    )
    rows = _audit(cache_dir)
    check(
        "default_model pick (sonnet under opus) is not injected", stdout.strip() == ""
    )
    check(
        "default_model decision row is still written, band none",
        len(rows) == 1
        and rows[0]["decision"]["tier"] == "project_default"
        and rows[0]["decision"]["model"] == "sonnet"
        and rows[0]["decision"]["band"] == "none"
        and rows[0]["decision"]["decision_id"].startswith("r_")
        and rows[0]["outcome"] == "silent"
        and rows[0].get("outcome_hint") == "project_default_inline",
    )
    check(
        "decision row no longer carries the dead thresholds field",
        "thresholds" not in rows[0]["decision"],
    )
    # A prompt a heuristic DOES match still routes in the same project.
    ctx = _context(
        _run(
            {
                "session_id": "s-default-heuristic",
                "transcript_path": os.path.join(FIXTURES, "parent-opus.jsonl"),
                "cwd": project_dir,
                "permission_mode": "default",
                "prompt": SONNET_PROMPT,
            },
            cache_dir,
        )
    )
    check("default_model does not suppress a heuristic pick", ctx is not None)

with tempfile.TemporaryDirectory() as tmp:
    cache_dir = os.path.join(tmp, "cache")
    project_dir = _project(
        tmp,
        {
            "rules": [
                {"match": "frobnicate", "model": "opus", "reason": "rule to opus"},
                {"match": "wibble", "model": "sonnet", "reason": "rule to sonnet"},
                {"match": "wobble", "model": "fable", "reason": "rule to fable"},
            ]
        },
    )

    def rule_run(session_id: str, parent_fixture: str, prompt: str):
        stdout = _run(
            {
                "session_id": session_id,
                "transcript_path": os.path.join(FIXTURES, parent_fixture),
                "cwd": project_dir,
                "permission_mode": "default",
                "prompt": prompt,
            },
            cache_dir,
        )
        return _context(stdout), _audit(cache_dir)

    ctx, rows = rule_run("s-rule-up", "parent-sonnet.jsonl", "frobnicate the widget")
    check("rule -> opus under a sonnet parent is NOT injected", ctx is None)
    check(
        "rule -> opus under a sonnet parent audits same_or_higher_inline",
        rows[-1]["decision"]["source"] == "project_config"
        and rows[-1]["decision"]["model"] == "opus"
        and rows[-1].get("outcome_hint") == "same_or_higher_inline",
    )
    ctx, rows = rule_run("s-rule-fable", "parent-opus.jsonl", "wobble the widget")
    check("rule -> fable under an opus parent is NOT injected", ctx is None)
    ctx, _ = rule_run("s-rule-down", "parent-opus.jsonl", "wibble the widget")
    check(
        "rule -> sonnet under an opus parent still injects",
        ctx is not None
        and 'Agent(subagent_type="auto-model-router:router-sonnet")' in ctx,
    )
    ctx, _ = rule_run("s-tag-up", "parent-sonnet.jsonl", "#model=opus frobnicate it")
    check(
        "inline #model=opus under a sonnet parent still routes up",
        ctx is not None
        and 'Agent(subagent_type="auto-model-router:router-opus")' in ctx,
    )

# ---------------------------------------------------------------------------
# Follow-ups stay inline
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as cache_dir:
    for followup in ("ok continue", "ok now do that again", "also fix the tests"):
        stdout = _run(
            {
                "session_id": "s-followup",
                "transcript_path": os.path.join(FIXTURES, "parent-opus.jsonl"),
                "cwd": "/tmp",
                "permission_mode": "default",
                "prompt": followup,
            },
            cache_dir,
        )
        check(f"follow-up {followup!r} is silent", stdout.strip() == "")
    hints = [r.get("outcome_hint") for r in _audit(cache_dir)]
    check("follow-up audited as followup_inline", "followup_inline" in hints)

    # A long prompt that merely starts with a follow-up word is still routed.
    ctx = _context(
        _run(
            {
                "session_id": "s-followup-long",
                "transcript_path": os.path.join(FIXTURES, "parent-opus.jsonl"),
                "cwd": "/tmp",
                "permission_mode": "default",
                "prompt": "now refactor the rate limiter to use a token bucket",
            },
            cache_dir,
        )
    )
    check("long prompt starting with a follow-up word still routes", ctx is not None)

    # An inline `#model=` tag is the only way to route up, so a short prompt
    # that opens like a follow-up but carries one must not be dropped.
    ctx = _context(
        _run(
            {
                "session_id": "s-followup-override",
                "transcript_path": os.path.join(FIXTURES, "parent-sonnet.jsonl"),
                "cwd": "/tmp",
                "permission_mode": "default",
                "prompt": "this needs #model=opus please",
            },
            cache_dir,
        )
    )
    check(
        "follow-up-shaped prompt with #model=opus under a sonnet parent routes up",
        ctx is not None
        and 'Agent(subagent_type="auto-model-router:router-opus")' in ctx,
    )
    stdout = _run(
        {
            "session_id": "s-followup-control",
            "transcript_path": os.path.join(FIXTURES, "parent-sonnet.jsonl"),
            "cwd": "/tmp",
            "permission_mode": "default",
            "prompt": "this needs more work please",
        },
        cache_dir,
    )
    check(
        "control: the same follow-up without a tag stays silent", stdout.strip() == ""
    )

# ---------------------------------------------------------------------------
# No post-turn-5 suppression: routing survives a long session
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as cache_dir:
    payload = {
        "session_id": "s-long",
        "transcript_path": os.path.join(FIXTURES, "parent-opus.jsonl"),
        "cwd": "/tmp",
        "permission_mode": "default",
        "prompt": SONNET_PROMPT,
    }
    last = ""
    for _ in range(8):
        last = _run(payload, cache_dir)
    check(
        "turn 8 still injects (no continuity suppression)", _context(last) is not None
    )

# ---------------------------------------------------------------------------
# Context budget line, throttled
# ---------------------------------------------------------------------------
check(
    "context warn defaults to the 600k auto-compact window",
    router.CTX_WARN_TOKENS == 600_000,
)
with tempfile.TemporaryDirectory() as cache_dir:
    mid = _context(
        _run(
            {
                "session_id": "s-250k",
                "transcript_path": os.path.join(FIXTURES, "parent-opus-250k.jsonl"),
                "cwd": "/tmp",
                "permission_mode": "default",
                "prompt": SONNET_PROMPT,
            },
            cache_dir,
        )
    )
    check(
        "252k session is under the 600k window: no budget line",
        mid is not None and "context " not in mid,
    )

with tempfile.TemporaryDirectory() as cache_dir:
    payload = {
        "session_id": "s-budget",
        "transcript_path": os.path.join(FIXTURES, "parent-opus-650k.jsonl"),
        "cwd": "/tmp",
        "permission_mode": "default",
        "prompt": SONNET_PROMPT,
    }
    first = _context(_run(payload, cache_dir))
    second = _context(_run(payload, cache_dir))
    check(
        "over-budget session gets the budget line",
        first is not None
        and "context 650k > 600k: delegate routable work" in first
        and "/wrap-session" in first,
    )
    check(
        "budget line throttled on the next prompt",
        second is not None and "> 600k" not in second,
    )

    # ...and returns once the throttle window has passed.
    session_file = os.path.join(cache_dir, "sessions", "s-budget.json")
    with open(session_file) as f:
        state = json.load(f)
    state["turns"] = 20
    with open(session_file, "w") as f:
        json.dump(state, f)
    third = _context(_run(payload, cache_dir))
    check(
        "budget line returns after 10 prompts",
        third is not None and "> 600k" in third,
    )

with tempfile.TemporaryDirectory() as cache_dir:
    under = _context(
        _run(
            {
                "session_id": "s-under-budget",
                "transcript_path": os.path.join(FIXTURES, "parent-opus.jsonl"),
                "cwd": "/tmp",
                "permission_mode": "default",
                "prompt": SONNET_PROMPT,
            },
            cache_dir,
        )
    )
    check(
        "under-budget session gets no budget line",
        under is not None and "context " not in under,
    )

# ---------------------------------------------------------------------------
# Plan mode
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as cache_dir:
    ctx = _context(
        _run(
            {
                "session_id": "s-plan",
                "transcript_path": os.path.join(FIXTURES, "parent-opus.jsonl"),
                "cwd": "/tmp",
                "permission_mode": "plan",
                "prompt": SONNET_PROMPT,
            },
            cache_dir,
        )
    )
    check(
        "plan mode injects only the plan-with-models pointer",
        ctx
        == "[auto-router] plan mode: use the plan-with-models skill; no delegation.",
    )
    check(
        f"plan-mode line <= {MAX_INJECTION_CHARS} chars",
        ctx is not None and len(ctx) <= MAX_INJECTION_CHARS,
    )
    rows = _audit(cache_dir)
    check(
        "plan-mode decision audited with plan_mode=true",
        bool(rows)
        and rows[-1]["outcome"] == "plan_mode"
        and rows[-1]["decision"]["plan_mode"] is True,
    )

print(f"--- session state: {'OK' if FAIL == 0 else f'{FAIL} FAILED'}")
sys.exit(1 if FAIL else 0)
