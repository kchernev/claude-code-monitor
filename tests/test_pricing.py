"""Pricing: rate lookup, cache multipliers and the cache-saving delta."""

from __future__ import annotations

import pytest

from claude_monitor import pricing

MILLION = 1_000_000


# ---------------------------------------------------------------------------
# Model id normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("claude-opus-5[1m]", "claude-opus-5"),
    ("claude-opus-5", "claude-opus-5"),
    ("  claude-sonnet-5  ", "claude-sonnet-5"),
    ("", ""),
    (None, ""),
])
def test_normalize_model_strips_context_window_suffix(raw, expected):
    assert pricing.normalize_model(raw) == expected


def test_suffixed_variant_prices_the_same_as_the_base_model():
    # The 1M-context variant is a different id but not a different price;
    # treating it as unknown would quietly reprice a whole session.
    assert pricing.rate_for("claude-opus-5[1m]") is pricing.RATES["claude-opus-5"]


def test_dated_snapshot_falls_back_to_its_family_by_longest_prefix():
    rate = pricing.rate_for("claude-sonnet-5-20260401")
    assert (rate.input, rate.output) == (3.00, 15.00)


def test_unknown_model_is_never_silently_free():
    # A model we have no rate for must still cost something, or a new id
    # would make a session look free rather than unpriced.
    rate = pricing.rate_for("claude-unheard-of-9")
    assert rate is pricing.UNKNOWN_RATE
    assert rate.input > 0 and rate.output > 0
    assert pricing.cost_of("claude-unheard-of-9", output_tokens=MILLION) > 0


@pytest.mark.parametrize("model", ["<synthetic>", "", None])
def test_synthetic_models_carry_no_cost(model):
    rate = pricing.rate_for(model)
    assert (rate.input, rate.output) == (0.0, 0.0)
    assert pricing.cost_of(model, input_tokens=MILLION, output_tokens=MILLION) == 0.0


@pytest.mark.parametrize("raw,expected", [
    ("claude-opus-5[1m]", "Opus 5 (1M)"),
    ("claude-opus-5", "Opus 5"),
    ("claude-haiku-4-5", "Haiku 4.5"),
    ("<synthetic>", "synthetic"),
    (None, "—"),
])
def test_display_name(raw, expected):
    assert pricing.display_name(raw) == expected


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def test_each_token_bucket_bills_at_its_own_multiple_of_the_input_rate():
    # Opus 5 is $5/Mtok in, $25/Mtok out. One million tokens in each bucket
    # makes the multipliers readable as plain dollars.
    m = MILLION
    assert pricing.cost_of("claude-opus-5", input_tokens=m) == pytest.approx(5.00)
    assert pricing.cost_of("claude-opus-5", output_tokens=m) == pytest.approx(25.00)
    assert pricing.cost_of("claude-opus-5", cache_read=m) == pytest.approx(0.50)
    assert pricing.cost_of("claude-opus-5", cache_write_5m=m) == pytest.approx(6.25)
    assert pricing.cost_of("claude-opus-5", cache_write_1h=m) == pytest.approx(10.00)


def test_cost_is_the_sum_of_its_buckets():
    kw = dict(input_tokens=1000, output_tokens=2000, cache_read=50_000,
              cache_write_5m=8000, cache_write_1h=400)
    total = pricing.cost_of("claude-sonnet-5", **kw)
    parts = sum(
        pricing.cost_of("claude-sonnet-5", **{k: v})
        for k, v in kw.items()
    )
    assert total == pytest.approx(parts)


def test_fast_mode_uses_the_fast_rate_where_a_model_has_one():
    normal = pricing.cost_of("claude-opus-5", output_tokens=MILLION)
    fast = pricing.cost_of("claude-opus-5", output_tokens=MILLION, fast=True)
    assert normal == pytest.approx(25.00)
    assert fast == pytest.approx(50.00)


def test_fast_mode_on_a_model_without_fast_rates_changes_nothing():
    kw = dict(input_tokens=MILLION, output_tokens=MILLION)
    assert (pricing.cost_of("claude-sonnet-5", fast=True, **kw)
            == pricing.cost_of("claude-sonnet-5", **kw))


# ---------------------------------------------------------------------------
# The counterfactual used for "cache savings"
# ---------------------------------------------------------------------------


def test_uncached_cost_reprices_every_cached_token_as_fresh_input():
    m = MILLION
    uncached = pricing.uncached_cost_of(
        "claude-opus-5", input_tokens=m, cache_read=m, cache_write_5m=m,
    )
    # Three million input tokens at the full $5 rate, no multipliers.
    assert uncached == pytest.approx(15.00)


def test_caching_can_only_ever_save_money():
    kw = dict(input_tokens=1200, output_tokens=900, cache_read=250_000,
              cache_write_5m=30_000, cache_write_1h=1000)
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"):
        assert (pricing.uncached_cost_of(model, **kw)
                >= pricing.cost_of(model, **kw))


def test_uncached_cost_honours_fast_rates_too():
    # Comparing a fast-mode session against a normal-rate counterfactual
    # would overstate the saving; both sides must use the same rate card.
    kw = dict(cache_read=MILLION, output_tokens=0)
    assert pricing.uncached_cost_of("claude-opus-5", fast=True, **kw) == pytest.approx(10.00)


def test_zero_usage_costs_nothing():
    assert pricing.cost_of("claude-opus-5") == 0.0
    assert pricing.uncached_cost_of("claude-opus-5") == 0.0


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("amount,expected", [
    (0, "$0"),
    (0.000012, "$0.00001"),
    (0.1234, "$0.123"),
    (12.345, "$12.35"),
    (1234.7, "$1,235"),
])
def test_fmt_usd_scales_precision_to_magnitude(amount, expected):
    assert pricing.fmt_usd(amount) == expected


@pytest.mark.parametrize("n,expected", [
    (0, "0"),
    (999, "999"),
    (1500, "1.5K"),
    (1_234_567, "1.23M"),
    (2_500_000_000, "2.50B"),
])
def test_fmt_tokens(n, expected):
    assert pricing.fmt_tokens(n) == expected
