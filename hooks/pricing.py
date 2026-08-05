#!/usr/bin/env python3
"""Per-MTok price table + cost helpers, shared by the usage report.

Rates are approximate (per the Anthropic models/pricing docs, 5.x era —
Fable 5 / Opus 5 / Sonnet 5 / Haiku 4.5) and
exclude cache-token pricing — cache is a second-order correction and is
ignored equally on both sides of the savings delta, so the estimate stays
directionally sound. Always label downstream numbers as estimates.
"""

# USD per token (published per-MTok rate / 1_000_000).
PRICES = {
    "fable": {"in": 10.0 / 1e6, "out": 50.0 / 1e6},
    "opus": {"in": 5.0 / 1e6, "out": 25.0 / 1e6},
    "sonnet": {"in": 3.0 / 1e6, "out": 15.0 / 1e6},
    "haiku": {"in": 1.0 / 1e6, "out": 5.0 / 1e6},
}

BASELINE = "opus"  # the all-Opus counterfactual baseline


def model_family(model_id: str | None) -> str | None:
    """Map a concrete model id (e.g. 'claude-opus-4-8') to a family key.

    Returns None for unknown / missing ids so callers can bucket them as
    'other' and exclude them from savings math rather than guessing.
    """
    if not model_id:
        return None
    m = model_id.lower()
    if "fable" in m:
        return "fable"
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return None


def cost(family: str, tokens_in: int, tokens_out: int) -> float:
    p = PRICES.get(family)
    if not p:
        return 0.0
    return tokens_in * p["in"] + tokens_out * p["out"]


def counterfactual_saving(family: str, tokens_in: int, tokens_out: int) -> float:
    """$ saved by running these tokens on `family` instead of the baseline
    (Opus). Zero for the baseline itself or unknown families. Note: Fable's
    savings are legitimately negative (costs more than Opus), which is informative.
    """
    if family == BASELINE or family not in PRICES:
        return 0.0
    return cost(BASELINE, tokens_in, tokens_out) - cost(family, tokens_in, tokens_out)
