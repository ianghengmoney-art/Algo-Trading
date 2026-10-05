"""Module A -- health gate and classification router."""

from __future__ import annotations

import pytest

from gcfp.config import (
    CORE_GROWTH, CORE_STABLE, FINANCIAL_BANK, INSURER, REIT, SPEC_GROWTH,
)
from gcfp.data.fixtures import FixtureAdapter, FixtureSpec
from gcfp.modules.a_health import (
    build_trailing_window, classify, evaluate_health, gate_a1_solvency,
    gate_a2_leverage, gate_a3_earnings_quality, gate_a4_red_flags,
)
from gcfp.types import FilingFlags, Verdict


def _health(adapter, symbol, params, as_of, gate=None):
    return evaluate_health(adapter.load_candidate(symbol), params, as_of=as_of, capability_gate=gate)


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("MATURE", CORE_STABLE),
        ("FASTPROF", CORE_GROWTH),
        ("BURNER", SPEC_GROWTH),
        ("BANKCO", FINANCIAL_BANK),
        ("REITCO", REIT),
        ("INSURCO", INSURER),
    ],
)
def test_router_assigns_exactly_one_tag(adapter, params, as_of, gate, symbol, expected):
    result = _health(adapter, symbol, params, as_of, gate)
    assert result.verdict is Verdict.PASS
    assert result.classification == expected


def test_router_refuses_to_force_a_tag(adapter, params):
    """A company matching no A6 row is rejected, not routed to the nearest method."""
    spec = FixtureSpec(
        symbol="NOFIT", name="Fits No Row", sector="Industrials",
        base_revenue=3_000_000_000.0,
        revenue_growth_pct=25.0,   # too fast for CORE-STABLE
        net_margin_pct=2.0,        # profitable, so not SPEC-GROWTH
        ocf_to_ni=1.1, capex_pct_of_revenue=1.0,  # Rule of 40 far below 40
        price=40.0, shares=60_000_000.0,
    )
    local = FixtureAdapter([spec])
    tag, detail = classify(local.load_candidate("NOFIT"), params)
    assert tag is None
    assert "refusing to force a tag" in detail["route"]


def test_a1_negative_working_capital_passes_on_cash_flow_branch(adapter, params):
    """Structurally negative working capital is a feature, not a failure."""
    spec = FixtureSpec(
        symbol="COLLECTS", name="Collects Fast Co", sector="Technology",
        base_revenue=10_000_000_000.0, revenue_growth_pct=8.0, net_margin_pct=20.0,
        current_ratio=0.8, price=100.0, shares=100_000_000.0,
    )
    local = FixtureAdapter([spec])
    window = build_trailing_window(local.load_candidate("COLLECTS"))
    outcome = gate_a1_solvency(window, params)
    assert outcome.passed
    assert outcome.detail["branch"] == "positive_operating_cash_flow"
    assert outcome.detail["current_ratio"] < 1.0


def test_a1_records_which_branch_was_used(adapter, params):
    window = build_trailing_window(adapter.load_candidate("MATURE"))
    outcome = gate_a1_solvency(window, params)
    assert outcome.detail["branch"] == "current_ratio"


def test_a2_is_sector_relative_and_gaps_without_a_median(adapter, params):
    candidate = adapter.load_candidate("MATURE")
    window = build_trailing_window(candidate)
    passing = gate_a2_leverage(candidate, window, params)
    assert passing.passed
    assert passing.detail["sector_median"] > 0

    thin = candidate.__class__(**{**candidate.__dict__, "sector_net_debt_ebitda": (1.2, 1.4)})
    gapped = gate_a2_leverage(thin, window, params)
    assert gapped.verdict is Verdict.DATA_GAP
    assert "not substituting a universal ceiling" in gapped.reason


def test_a3_catches_earnings_not_backed_by_cash(adapter, params, as_of, gate):
    result = _health(adapter, "BADQUAL", params, as_of, gate)
    assert result.verdict is Verdict.FAIL
    a3 = result.gate("A3")
    assert a3.verdict is Verdict.FAIL
    assert a3.detail["ocf_to_net_income"] < params.health.min_ocf_to_net_income


def test_a3_pre_profit_branch_uses_cash_runway(adapter, params):
    candidate = adapter.load_candidate("BURNER")
    window = build_trailing_window(candidate)
    outcome = gate_a3_earnings_quality(window, params, None)
    assert outcome.detail["branch"] == "pre_profit"
    assert outcome.detail["runway_months"] >= params.health.min_cash_runway_months


def test_a4_unknown_filing_status_is_a_data_gap_not_a_pass(adapter, params):
    spec = FixtureSpec(
        symbol="UNKNOWN", name="Unknown Filings Co", sector="Industrials",
        base_revenue=2_000_000_000.0, revenue_growth_pct=5.0, net_margin_pct=10.0,
        price=30.0, shares=50_000_000.0,
        filing_flags=FilingFlags(None, None, None, None, None),
    )
    local = FixtureAdapter([spec])
    candidate = local.load_candidate("UNKNOWN")
    outcome = gate_a4_red_flags(candidate, build_trailing_window(candidate), params, None)
    assert outcome.verdict is Verdict.DATA_GAP
    assert "absence of evidence is not a clean bill of health" in outcome.reason


def test_a4_fails_on_going_concern(adapter, params, as_of, gate):
    result = _health(adapter, "FLAGGED", params, as_of, gate)
    assert result.verdict is Verdict.FAIL
    assert result.gate("A4").verdict is Verdict.FAIL


def test_a5_fails_on_stale_data_and_never_imputes(adapter, params, as_of, gate):
    result = _health(adapter, "STALECO", params, as_of, gate)
    assert result.verdict is Verdict.FAIL
    a5 = result.gate("A5")
    assert a5.verdict is Verdict.FAIL
    assert a5.detail["data_age_months"] > params.health.max_data_age_months


def test_growth_names_get_the_tighter_freshness_and_liquidity_floors(adapter, params, as_of, gate):
    growth = _health(adapter, "BURNER", params, as_of, gate)
    core = _health(adapter, "MATURE", params, as_of, gate)
    assert growth.gate("A5").detail["max_data_age_months"] == params.health.max_data_age_months_growth
    assert core.gate("A5").detail["max_data_age_months"] == params.health.max_data_age_months
    assert (
        growth.gate("UNIVERSE").detail["min_avg_daily_dollar_volume"]
        == params.universe.min_adv_usd_growth
    )


def test_every_gate_logs_the_values_that_produced_it(adapter, params, as_of, gate):
    result = _health(adapter, "MATURE", params, as_of, gate)
    for outcome in result.gates:
        assert outcome.detail, f"{outcome.gate} produced no auditable detail"
