#!/usr/bin/env python3
"""Per-MTok price table + cost helpers, shared by the usage report.

Rates are approximate (per the Anthropic models/pricing docs, 5.x era —
Fable 5 / Opus 5 / Sonnet 5 / Haiku 4.5). Always label downstream numbers
as estimates.

Cache pricing follows Anthropic's published multipliers off the base input
rate: cache reads are 0.1x input, cache writes (5-minute ephemeral cache
creation) are 1.25x input.
"""

CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25

# USD per token (published per-MTok rate / 1_000_000).
PRICES = {
    "fable": {"in": 10.0 / 1e6, "out": 50.0 / 1e6},
    "opus": {"in": 5.0 / 1e6, "out": 25.0 / 1e6},
    "sonnet": {"in": 3.0 / 1e6, "out": 15.0 / 1e6},
    "haiku": {"in": 1.0 / 1e6, "out": 5.0 / 1e6},
}
for _p in PRICES.values():
    _p["cache_read"] = _p["in"] * CACHE_READ_MULT
    _p["cache_write"] = _p["in"] * CACHE_WRITE_MULT
del _p

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


def cost_usd(model: str | None, usage: dict) -> float:
    """$ cost of a usage dict against a model id or family name.

    `usage` accepts either snake_case Anthropic API keys (input_tokens,
    output_tokens, cache_read_input_tokens, cache_creation_input_tokens)
    or the audit-log short keys (tokens_in, tokens_out, cache_read,
    cache_write). Unknown model/family or empty usage -> 0.0.
    """
    family = model_family(model) or (model if model in PRICES else None)
    p = PRICES.get(family)
    if not p or not usage:
        return 0.0
    tokens_in = usage.get("input_tokens", usage.get("tokens_in", 0)) or 0
    tokens_out = usage.get("output_tokens", usage.get("tokens_out", 0)) or 0
    cache_read = (
        usage.get("cache_read_input_tokens", usage.get("cache_read", 0)) or 0
    )
    cache_write = (
        usage.get("cache_creation_input_tokens", usage.get("cache_write", 0)) or 0
    )
    return (
        tokens_in * p["in"]
        + tokens_out * p["out"]
        + cache_read * p["cache_read"]
        + cache_write * p["cache_write"]
    )


def counterfactual_saving(family: str, tokens_in: int, tokens_out: int) -> float:
    """$ saved by running these tokens on `family` instead of the baseline
    (Opus). Zero for the baseline itself or unknown families. Note: Fable's
    savings are legitimately negative (costs more than Opus), which is informative.
    """
    if family == BASELINE or family not in PRICES:
        return 0.0
    return cost(BASELINE, tokens_in, tokens_out) - cost(family, tokens_in, tokens_out)
