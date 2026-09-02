#!/usr/bin/env python3
"""Router adapter + entry for the `sil` self-improving loop.

Closes the router's measure->propose loop into an autonomous
PROPOSE->COMMIT->EXECUTE->EVALUATE->DECIDE->LOG loop for CONFIG-class artifacts
only (rung AUTO_CONFIG). It tunes a single global knob (`auto_threshold`) in a
LOOP-PRIVATE git repo (~/.claude/cache/router/loop-config) — never a user
project repo — and never pushes. The human `/router-report` "offer, don't apply"
contract is untouched; this is a separate opt-in entry point.

Subcommands:
  propose [--force-auto X] [--dry-run]   one PROPOSE iteration
  confirm                                resolve pending experiments past their window
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

KERNEL = os.path.expanduser("~/.claude/lib/self-improving-loop")
sys.path.insert(0, KERNEL)
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_ROOT, "tools"))

import replay_kpi  # noqa: E402

from sil import vcs  # noqa: E402
from sil.ledger import Ledger  # noqa: E402
from sil.loop import Loop  # noqa: E402
from sil.models import ArtifactClass, Candidate, RunResult, Rung  # noqa: E402

CACHE = os.path.expanduser(
    os.environ.get("CC_ROUTER_CACHE_DIR", "~/.claude/cache/router")
)
AUDIT = os.path.join(CACHE, "audit.jsonl")
LOOP_CONFIG = os.path.join(CACHE, "loop-config")
LEDGER = os.path.join(CACHE, "applied-tweaks.jsonl")
BRANCH = "router-loop/auto"

DEFAULT_AUTO, DEFAULT_ASK = 0.75, 0.60
MARGIN_USD = 0.50  # min realized $-saved improvement (over the window) to KEEP
QUALITY_EPS = 2.0  # router correction rate may exceed inline by at most this (pp)
AUTO_CEILING = 0.90  # never auto-route more than this share of decisions
STEP, FLOOR, MIN_MASS = 0.05, 0.65, 5


# ---- pure decision helpers (unit-tested) --------------------------------


def distribution_gate(
    recorded: dict, candidate: dict, *, auto_ceiling: float = AUTO_CEILING
) -> bool:
    """A candidate threshold's projected band distribution is sane iff it does
    not auto-route more than ``auto_ceiling`` of decisions and does not grow the
    silent ('none') band (which only changes if ask_threshold is wrong)."""
    total = sum(candidate.values()) or 1
    if candidate.get("auto", 0) / total > auto_ceiling:
        return False
    if candidate.get("none", 0) > recorded.get("none", 0):
        return False
    return True


def thresholds_valid(
    auto: float, ask: float, *, floor: float = FLOOR, ceil: float = 0.95
) -> bool:
    """Structural safety net (catches forced/override candidates the proposer
    would never emit): auto must sit within [floor, ceil] AND strictly above ask
    — otherwise the band order inverts and the ask band collapses."""
    return (floor <= auto <= ceil) and (auto > ask)


def propose_threshold(
    current_auto: float,
    decisions: list[dict],
    *,
    step: float = STEP,
    floor: float = FLOOR,
    min_mass: int = MIN_MASS,
) -> float | None:
    """Propose lowering auto_threshold by one step if there is enough reclaimable
    ask-band mass — non-opus decisions currently asked whose confidence would be
    auto under the new threshold. Opus is excluded (it keeps a 0.85 floor).
    Returns the new threshold, or None if there's no sensible move."""
    new = round(current_auto - step, 4)
    if new < floor:
        return None
    reclaimable = sum(
        1
        for d in decisions
        if d.get("band") == "ask"
        and d.get("model") != "opus"
        and d.get("confidence", 0) >= new
    )
    return new if reclaimable >= min_mass else None


# ---- I/O helpers --------------------------------------------------------


def _read_config() -> dict:
    p = os.path.join(LOOP_CONFIG, "router.json")
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except (json.JSONDecodeError, OSError):
            pass
    return {"auto_threshold": DEFAULT_AUTO, "ask_threshold": DEFAULT_ASK}


def _run_fixtures() -> bool:
    r = subprocess.run(
        ["bash", os.path.join(PLUGIN_ROOT, "tests", "run.sh")],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def _usage_json(days: int = 7) -> dict:
    r = subprocess.run(
        [
            sys.executable,
            os.path.join(PLUGIN_ROOT, "tools", "usage-report.py"),
            "--json",
            "--days",
            str(days),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


# ---- adapter ------------------------------------------------------------


class RouterAdapter:
    def __init__(self, force_auto: float | None = None):
        self.force_auto = force_auto
        self.band_for = replay_kpi.import_band_for()

    def propose(self) -> Candidate | None:
        cfg = _read_config()
        cur = float(cfg.get("auto_threshold", DEFAULT_AUTO))
        ask = float(cfg.get("ask_threshold", DEFAULT_ASK))
        decisions = list(replay_kpi.iter_decisions(AUDIT))
        if self.force_auto is not None:
            new = float(self.force_auto)
        else:
            new = propose_threshold(cur, decisions)
            if new is None:
                return None
        if abs(new - cur) < 1e-9:
            return None
        newcfg = dict(cfg)
        newcfg["auto_threshold"] = new
        direction = "down" if new < cur else "up"
        return Candidate(
            artifact_path="router.json",
            change_summary=f"auto_threshold {cur} -> {new}",
            change_signature=f"auto_threshold:{direction}",
            artifact_class=ArtifactClass.CONFIG,
            new_text=json.dumps(newcfg, indent=2) + "\n",
            meta={"auto": new, "ask": ask, "prev_auto": cur},
        )

    def execute(self, cand: Candidate) -> RunResult:
        decisions = list(replay_kpi.iter_decisions(AUDIT))
        # Apples-to-apples: compare the candidate re-band against a re-band under
        # the CURRENT thresholds (same rows, same band_for, only the knob differs).
        # Using the historically-recorded bands here would spuriously fail the
        # gate, because old rows used different ask thresholds.
        baseline_dist = replay_kpi.reband_distribution(
            decisions, cand.meta["prev_auto"], cand.meta["ask"], self.band_for
        )
        candidate = replay_kpi.reband_distribution(
            decisions, cand.meta["auto"], cand.meta["ask"], self.band_for
        )
        gates = {
            "thresholds_valid": thresholds_valid(cand.meta["auto"], cand.meta["ask"]),
            "hook_healthy": _run_fixtures(),
            "distribution_sane": distribution_gate(baseline_dist, candidate),
        }
        baseline = (_usage_json().get("cost") or {}).get("est_saved_vs_opus_usd")
        # Gates pass -> the realized $-saved needs a live window before we trust it.
        return RunResult(
            metric=None,
            gates=gates,
            deferred=True,
            detail={
                "baseline": baseline,
                "recorded": replay_kpi.recorded_distribution(decisions),
                "baseline_dist": baseline_dist,
                "candidate": candidate,
            },
        )

    def measure(self, row) -> RunResult:
        u = _usage_json()  # recent window (approx; refine to since=row.ts later)
        cost = u.get("cost") or {}
        quality = u.get("quality") or {}
        est = cost.get("est_saved_vs_opus_usd")
        haiku = (cost.get("pct_tokens_by_model") or {}).get("haiku", 0) / 100.0
        rc = (quality.get("router") or {}).get("correction_rate_pct")
        ic = (quality.get("inline") or {}).get("correction_rate_pct")
        # Quality is the ONLY hard KEEP-veto (GOALS.md §2): the router cohort's
        # correction rate must not exceed inline's by more than QUALITY_EPS.
        # Haiku share is a directional supporting metric, not a veto — low Haiku
        # share is a reason to lower the threshold, not to reject the change — so
        # it is reported for visibility but never gates a KEEP.
        gates = {
            "quality": rc is None or ic is None or (rc - ic) <= QUALITY_EPS,
        }
        return RunResult(
            metric=est, gates=gates, detail={"haiku": haiku, "rc": rc, "ic": ic}
        )


# ---- entry --------------------------------------------------------------


def _ensure_baseline() -> None:
    os.makedirs(LOOP_CONFIG, exist_ok=True)
    p = os.path.join(LOOP_CONFIG, "router.json")
    if not os.path.exists(p):
        with open(p, "w") as f:
            json.dump(
                {"auto_threshold": DEFAULT_AUTO, "ask_threshold": DEFAULT_ASK},
                f,
                indent=2,
            )
            f.write("\n")
    vcs.ensure_repo(LOOP_CONFIG)
    try:
        vcs.commit_file(LOOP_CONFIG, "router.json", "baseline", branch=BRANCH)
    except ValueError:
        pass  # already committed / nothing to commit


def _summ(row) -> dict:
    return {
        "decision": row.decision,
        "change": row.change_summary,
        "gates": row.gates,
        "reverted": row.reverted,
        "pending_until": row.pending_until,
        "commit_sha": (row.commit_sha or "")[:10] or None,
        "note": row.note,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="router-loop")
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("propose")
    pp.add_argument(
        "--force-auto", type=float, help="force a specific auto_threshold (testing)"
    )
    pp.add_argument("--dry-run", action="store_true", help="DRY_RUN rung: never apply")
    sub.add_parser("confirm")
    a = p.parse_args(argv)

    _ensure_baseline()
    ledger = Ledger(LEDGER)
    rung = Rung.DRY_RUN if getattr(a, "dry_run", False) else Rung.AUTO_CONFIG
    loop = Loop(
        "router",
        ledger,
        rung,
        repo=LOOP_CONFIG,
        branch=BRANCH,
        higher_is_better=True,
        margin=MARGIN_USD,
        window_days=2,
    )
    now = datetime.now(timezone.utc)

    if a.cmd == "propose":
        row = loop.propose_iteration(RouterAdapter(force_auto=a.force_auto), now)
        print(
            json.dumps(_summ(row), indent=2)
            if row
            else "no proposal (no sensible move, or an experiment is still pending)"
        )
    else:
        rows = loop.confirm_pending(RouterAdapter(), now)
        print(
            json.dumps([_summ(r) for r in rows], indent=2)
            if rows
            else "no pending experiments are due"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
