"""Model pricing and cost computation.

Rates are USD per million tokens, sourced from the Anthropic pricing table
(current as of 2026-07). Cache multipliers are applied against the model's
*input* rate:

    cache write (5m TTL) = 1.25x input
    cache write (1h TTL) = 2.00x input
    cache read           = 0.10x input

Models marked ``legacy=True`` are retired or deprecated; their rates are
best-known historical values retained so that old transcripts still cost out
to something sensible. They are flagged in the UI so nobody mistakes them for
current pricing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

# Cache tier multipliers, relative to the model's input rate.
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.00
CACHE_READ_MULT = 0.10


@dataclass(frozen=True)
class Rate:
    """Per-million-token pricing for one model."""

    input: float
    output: float
    label: str
    legacy: bool = False
    # Some models bill differently in "fast mode" (Opus 5 / 4.8 research preview).
    fast_input: Optional[float] = None
    fast_output: Optional[float] = None


# Keys are the exact model ids that appear in transcripts.
RATES: Dict[str, Rate] = {
    # --- Frontier tier -----------------------------------------------------
    "claude-fable-5": Rate(10.00, 50.00, "Fable 5"),
    "claude-mythos-5": Rate(10.00, 50.00, "Mythos 5"),
    "claude-mythos-preview": Rate(10.00, 50.00, "Mythos Preview", legacy=True),
    # --- Opus tier ---------------------------------------------------------
    "claude-opus-5": Rate(5.00, 25.00, "Opus 5", fast_input=10.00, fast_output=50.00),
    "claude-opus-4-8": Rate(5.00, 25.00, "Opus 4.8", fast_input=10.00, fast_output=50.00),
    "claude-opus-4-7": Rate(5.00, 25.00, "Opus 4.7"),
    "claude-opus-4-6": Rate(5.00, 25.00, "Opus 4.6"),
    "claude-opus-4-5": Rate(5.00, 25.00, "Opus 4.5"),
    "claude-opus-4-1": Rate(15.00, 75.00, "Opus 4.1", legacy=True),
    "claude-opus-4-0": Rate(15.00, 75.00, "Opus 4", legacy=True),
    # --- Sonnet tier -------------------------------------------------------
    "claude-sonnet-5": Rate(3.00, 15.00, "Sonnet 5"),
    "claude-sonnet-4-6": Rate(3.00, 15.00, "Sonnet 4.6"),
    "claude-sonnet-4-5": Rate(3.00, 15.00, "Sonnet 4.5"),
    "claude-sonnet-4-0": Rate(3.00, 15.00, "Sonnet 4", legacy=True),
    "claude-3-7-sonnet-20250219": Rate(3.00, 15.00, "Sonnet 3.7", legacy=True),
    # --- Haiku tier --------------------------------------------------------
    "claude-haiku-4-5": Rate(1.00, 5.00, "Haiku 4.5"),
    "claude-haiku-4-5-20251001": Rate(1.00, 5.00, "Haiku 4.5", legacy=True),
    "claude-3-5-haiku-20241022": Rate(0.80, 4.00, "Haiku 3.5", legacy=True),
}

# Fallback used when a transcript names a model we have no rate for. Priced at
# the Opus tier so unknown models are never silently free.
UNKNOWN_RATE = Rate(5.00, 25.00, "unknown", legacy=True)

# Model ids that carry no billable usage.
SYNTHETIC_MODELS = {"<synthetic>", None, ""}


def normalize_model(model: Optional[str]) -> str:
    """Strip context-window and deployment suffixes from a model id.

    Claude Code writes ids like ``claude-opus-5[1m]`` for the 1M-context
    variant; pricing is identical, so we collapse to the base id.
    """
    if not model:
        return ""
    base = model.split("[", 1)[0].strip()
    return base


def rate_for(model: Optional[str]) -> Rate:
    """Look up the rate for a model id, tolerating suffixed variants."""
    base = normalize_model(model)
    if not base or base in SYNTHETIC_MODELS:
        return Rate(0.0, 0.0, base or "synthetic", legacy=False)
    if base in RATES:
        return RATES[base]
    # Longest-prefix match handles dated snapshots we haven't enumerated.
    for known in sorted(RATES, key=len, reverse=True):
        if base.startswith(known):
            return RATES[known]
    return UNKNOWN_RATE


def display_name(model: Optional[str]) -> str:
    """Human-friendly model label, e.g. ``claude-opus-5[1m]`` -> ``Opus 5 (1M)``."""
    if not model:
        return "—"
    if model in SYNTHETIC_MODELS:
        return "synthetic"
    base = normalize_model(model)
    suffix = ""
    if "[" in model:
        suffix = " (" + model.split("[", 1)[1].rstrip("]").upper() + ")"
    return rate_for(base).label + suffix


def cost_of(
    model: Optional[str],
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    cache_read: int = 0,
    fast: bool = False,
) -> float:
    """Return the USD cost of one API call's token usage."""
    r = rate_for(model)
    in_rate = r.input
    out_rate = r.output
    if fast and r.fast_input is not None:
        in_rate = r.fast_input
        out_rate = r.fast_output or out_rate

    dollars = (
        input_tokens * in_rate
        + cache_write_5m * in_rate * CACHE_WRITE_5M_MULT
        + cache_write_1h * in_rate * CACHE_WRITE_1H_MULT
        + cache_read * in_rate * CACHE_READ_MULT
        + output_tokens * out_rate
    )
    return dollars / 1_000_000.0


def uncached_cost_of(
    model: Optional[str],
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    cache_read: int = 0,
    fast: bool = False,
) -> float:
    """Cost this usage *would* have been with prompt caching disabled.

    Every cached token becomes a full-price input token. The delta against
    :func:`cost_of` is the realised saving from caching.
    """
    r = rate_for(model)
    in_rate = r.fast_input if (fast and r.fast_input is not None) else r.input
    out_rate = r.fast_output if (fast and r.fast_output is not None) else r.output
    total_input = input_tokens + cache_write_5m + cache_write_1h + cache_read
    return (total_input * in_rate + output_tokens * out_rate) / 1_000_000.0


def fmt_usd(amount: float) -> str:
    """Format a dollar amount with precision scaled to magnitude."""
    if amount >= 1000:
        return f"${amount:,.0f}"
    if amount >= 10:
        return f"${amount:,.2f}"
    if amount >= 0.01:
        return f"${amount:.3f}"
    if amount > 0:
        return f"${amount:.5f}"
    return "$0"


def fmt_tokens(n: float) -> str:
    """Compact token count: 1234567 -> ``1.23M``."""
    n = float(n)
    if abs(n) >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:.0f}"
