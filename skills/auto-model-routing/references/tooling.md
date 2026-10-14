# Tooling & evals — auto-model-routing

Level-3 disclosure for the router's deterministic tooling. These scripts are
**not** invoked by the parent during routing — they run via slash commands or a
hook. Paths are relative to this skill dir; the executables live at the plugin
root `tools/` and are wired by their current path (do not move them).

## Cache surface (router's `~/.claude/cache/router/`)

| File | Writer | Readers |
| --- | --- | --- |
| `audit.jsonl` | `auto-router.py` (every classified decision — injected or silent) · `post-agent-audit.py` hook (`delegated` rows, one per dispatch, carrying `agent_id`) · `subagent-stop-audit.py` hook (`worker_usage` rows: a finished router worker's usage, joined on `agent_id`) · `reconcile-outcomes.py` (Stop hook, backstops unattributed inline turns) | `usage-report.py`, `analyze-audit.py`, `replay_session_pivot.py` |
| `audit.jsonl.<stamp>` | `auto-router.py` rotates the live log into a UTC-stamped file (`YYYYmmddTHHMMSSZ`) past 5 MB and prunes rotated files after 30 days | the same readers — they read the rotated files too, oldest first, so a 30/90-day window is not truncated at the last rotation |

There is no `overrides.jsonl`. The two-band contract (`auto`/`none`) has no
`ask` band and no `AskUserQuestion` step to log a user choice for. Every
decision — injected or silent — is a single `audit.jsonl` row; the hooks write
all of it, so the parent never hand-logs anything (see SKILL.md → Outcome
logging).

Harness events (task notifications, scheduled-task runs, system notifications)
are not prompts: the hook skips them and writes no row.

## Scripts (`../../tools/`)

| Script | Trigger | Reads | Writes |
| --- | --- | --- | --- |
| `../../tools/usage-report.py` | `/router-report` | `audit.jsonl` (+ rotated files) and Claude Code transcripts | KPI report (stdout / `--json` / `--write`) |
| `../../tools/analyze-audit.py` | manual: `python3 tools/analyze-audit.py [--audit P] [--overrides P] [--days N]` | `audit.jsonl` (+ rotated files; `--overrides` is a vestigial flag — `overrides.jsonl` is never written under the two-band contract, so it's always empty/absent) | re-runnable analysis to stdout |
| `../../tools/replay_session_pivot.py` | manual: `python3 tools/replay_session_pivot.py [--days N] [--candidate sonnet] [--table]` | `audit.jsonl` (+ rotated files) | session-pivot counterfactual to stdout (offline eval) |

## Contract notes

- Agent dispatch is always the namespaced `auto-model-router:router-<model>`
  form. A bare `router-<model>` name fails with "Agent type not found" — see
  SKILL.md → Failure modes to avoid.
- Savings are measured against the **parent session's** model (the `parent`
  recorded on each decision, the session's dominant model in a transcript), not
  an all-Opus baseline.
- Exact dispatch attribution: a delegated row's `decision_id` is joined to the
  session's own injected decision (`post-agent-audit.py`), and a background
  worker's `worker_usage` row inherits it from the launch row via `agent_id`.
