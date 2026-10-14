#!/usr/bin/env python3
"""Per-MTok price table + cost helpers, shared by the usage report.

Rates are list prices for the 5.x lineup (Opus 5.5 / Sonnet 5.5 / Haiku 5.5 /
Fable 5.1). Always label downstream numbers as estimates.

An alias (`opus`, `sonnet`, `haiku`, `fable`) is priced as the CURRENT model of
that family. A full model id is priced as that exact model when it is one of
the older models still served (LEGACY_PRICES), and as the family's current
model otherwise.

Cache pricing differs per model: cache reads are a per-model fraction of the
input rate, and cache writes are 2x input (observed writes use the 1-hour TTL).
"""

import re

CACHE_WRITE_MULT = 2.0


def _rates(price_in: float, price_out: float, cache_read_mult: float) -> dict:
    """USD per token from a per-MTok list price."""
    return {
        "in": price_in / 1e6,
        "out": price_out / 1e6,
        "cache_read": price_in * cache_read_mult / 1e6,
        "cache_write": price_in * CACHE_WRITE_MULT / 1e6,
    }


# Current lineup, keyed by the alias the worker agents use (`model: opus`).
PRICES = {
    "fable": _rates(10.0, 50.0, 0.025),  # Fable 5.1
    "opus": _rates(4.0, 20.0, 0.05),  # Opus 5.5
    "sonnet": _rates(2.0, 10.0, 0.05),  # Sonnet 5.5
    "haiku": _rates(0.10, 0.50, 0.10),  # Haiku 5.5
}

# Older models that are still served and keep their own prices, keyed by
# (family, "major.minor" or "major").
LEGACY_PRICES = {
    ("opus", "5"): _rates(5.0, 25.0, 0.10),
    ("opus", "4.8"): _rates(5.0, 25.0, 0.10),
    ("sonnet", "5"): _rates(2.0, 10.0, 0.10),
    ("haiku", "4.5"): _rates(1.0, 5.0, 0.10),
}

# claude-<family>-<major>[-<minor>][-<yyyymmdd>]. The minor is 1-2 digits, so an
# 8-digit date suffix is never mistaken for one.
_MODEL_ID = re.compile(r"claude-(fable|opus|sonnet|haiku)-(\d+)(?:-(\d{1,2})(?!\d))?")


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


def price_for(model: str | None) -> dict | None:
    """Price table for an alias or a full model id; None when unknown."""
    if not model:
        return None
    if model in PRICES:
        return PRICES[model]
    family = model_family(model)
    if family is None:
        return None
    m = _MODEL_ID.search(model.lower())
    if m:
        version = m.group(2) + (f".{m.group(3)}" if m.group(3) else "")
        legacy = LEGACY_PRICES.get((m.group(1), version))
        if legacy:
            return legacy
    return PRICES[family]


def cost_usd(model: str | None, usage: dict) -> float:
    """$ cost of a usage dict against a model id or family alias.

    `usage` accepts either snake_case Anthropic API keys (input_tokens,
    output_tokens, cache_read_input_tokens, cache_creation_input_tokens)
    or the audit-log short keys (tokens_in, tokens_out, cache_read,
    cache_write). Unknown model/family or empty usage -> 0.0.
    """
    p = price_for(model)
    if not p or not usage:
        return 0.0
    tokens_in = usage.get("input_tokens", usage.get("tokens_in", 0)) or 0
    tokens_out = usage.get("output_tokens", usage.get("tokens_out", 0)) or 0
    cache_read = usage.get("cache_read_input_tokens", usage.get("cache_read", 0)) or 0
    cache_write = (
        usage.get("cache_creation_input_tokens", usage.get("cache_write", 0)) or 0
    )
    return (
        tokens_in * p["in"]
        + tokens_out * p["out"]
        + cache_read * p["cache_read"]
        + cache_write * p["cache_write"]
    )


def saving_vs_parent(model: str | None, parent: str | None, usage: dict) -> float:
    """$ saved by running `usage` on `model` instead of on the parent session's
    model. Negative when the worker is dearer than its parent (e.g.
    `#model=fable` under an Opus parent), which is informative. Zero when either
    model is unknown.
    """
    if price_for(model) is None or price_for(parent) is None:
        return 0.0
    return cost_usd(parent, usage) - cost_usd(model, usage)
