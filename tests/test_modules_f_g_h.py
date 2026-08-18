"""Modules F, G, H -- sizing, execution discipline, holdings monitor."""

from __future__ import annotations

from datetime import date

import pytest

from gcfp.config import (
    CORE_GROWTH, CORE_SLEEVE, CORE_STABLE, GROWTH_SLEEVE, SPEC_GROWTH,
)
from gcfp.modules.a_health import HealthResult
from gcfp.modules.f_sizing import (
    Portfolio, Position, QUALIFIED_NO_HEADROOM, allocate, growth_sleeve_breach,
    intended_sleeve_pct,
)
from gcfp.modules.g_execution import (
    RecommendationRecord, TRIM_TO_CAP, log_execution,
    quarterly_discipline_report, trim_to_cap_flags,
)
from gcfp.modules.h_monitor import (
    GrowthDeterioration, REVIEW_FLAG, SELL_FLAG, review_holding,
)
from gcfp.types import Verdict


def _portfolio(total=1_000_000.0, ballast=500_000.0, positions=None):
    return Portfolio(total_value=total, ballast_value=ballast, positions=positions or [])


def test_sleeves_are_sized_under_their_own_rules(params):
    assert intended_sleeve_pct(CORE_SLEEVE, 85.0, params)[0] == params.sizing.core_high_conviction_pct
    assert intended_sleeve_pct(GROWTH_SLEEVE, 85.0, params)[0] == params.sizing.growth_high_conviction_pct
    assert intended_sleeve_pct(CORE_SLEEVE, 65.0, params)[0] == params.sizing.core_standard_pct
    assert intended_sleeve_pct(GROWTH_SLEEVE, 65.0, params)[0] == params.sizing.growth_standard_pct
    assert intended_sleeve_pct(CORE_SLEEVE, 45.0, params)[0] == 0.0


def test_growth_positions_land_between_one_and_a_half_and_two_and_a_quarter_percent(params):
    report = allocate(
        [("G1", SPEC_GROWTH, "Technology", 85.0), ("G2", CORE_GROWTH, "Healthcare", 65.0)],
        _portfolio(),
        params,
    )
    sizes = {d.symbol: d.intended_portfolio_pct for d in report.decisions}
    assert sizes["G1"] == pytest.approx(2.25, abs=0.01)
    assert sizes["G2"] == pytest.approx(1.5, abs=0.01)


def test_growth_sleeve_cap_is_hard_enforced(params):
    over = _portfolio(
        positions=[Position("G", SPEC_GROWTH, "Technology", 200_000.0, 200_000.0)]
    )
    breach = growth_sleeve_breach(over, params)
    assert breach is not None and "HARD CAP" in breach
    assert growth_sleeve_breach(_portfolio(), params) is None


def test_full_sleeve_surfaces_qualified_no_headroom_rather_than_recommending(params):
    full = _portfolio(
        positions=[Position("G", SPEC_GROWTH, "Technology", 150_000.0, 150_000.0)]
    )
    report = allocate([("NEW", SPEC_GROWTH, "Healthcare", 85.0)], full, params)
    decision = next(d for d in report.decisions if d.symbol == "NEW")
    assert decision.status == QUALIFIED_NO_HEADROOM
    assert decision.intended_value == 0.0


def test_sector_cap_blocks_a_concentrating_recommendation(params):
    heavy = _portfolio(
        positions=[Position(f"C{i}", CORE_STABLE, "Industrials", 30_000.0, 30_000.0) for i in range(4)]
    )
    report = allocate([("NEW", CORE_STABLE, "Industrials", 85.0)], heavy, params)
    decision = next(d for d in report.decisions if d.symbol == "NEW")
    assert decision.status == QUALIFIED_NO_HEADROOM
    assert "sector cap" in decision.reasons[0]


def test_thin_screen_holds_cash_and_never_concentrates(params):
    report = allocate([("ONE", CORE_STABLE, "Industrials", 90.0)], _portfolio(), params)
    assert any("THIN" in n and "NOT enlarged" in n for n in report.header_notices)
    sized = next(d for d in report.decisions if d.symbol == "ONE")
    assert sized.intended_sleeve_pct == params.sizing.core_high_conviction_pct


def test_ballast_gap_opens_the_report(params):
    report = allocate([], _portfolio(ballast=200_000.0), params)
    assert report.ballast_gap_pct == pytest.approx(25.0)
    assert "INDEX/BALLAST BELOW TARGET" in report.header_notices[0]


def _record(**kwargs):
    base = dict(
        symbol="X", recommended_on=date(2026, 6, 30), classification=CORE_STABLE,
        conviction_score=80.0, fair_value_per_share=100.0, valuation_method="B1",
        own_history_reading_pct=40.0, peer_reading_pct=35.0, intended_sleeve_pct=8.0,
        intended_value=100_000.0, currency="USD",
        thesis_invalidation="operating margin falls below 12% for two consecutive quarters",
    )
    base.update(kwargs)
    return RecommendationRecord(**base)


def test_a_recommendation_requires_a_falsifiable_thesis(params):
    with pytest.raises(ValueError, match="what would change my mind"):
        _record(thesis_invalidation="   ")


def test_size_deviation_beyond_threshold_requires_a_logged_reason(params):
    record = _record()
    with pytest.raises(ValueError, match="logged reason is required"):
        log_execution(record, date(2026, 7, 1), 150_000.0, params)

    execution = log_execution(record, date(2026, 7, 1), 150_000.0, params, reason="scaled in early")
    assert execution.size_deviation is True
    assert execution.deviation_pct == pytest.approx(50.0)
    assert execution.reason_for_deviation == "scaled in early"


def test_small_deviation_is_not_flagged(params):
    execution = log_execution(_record(), date(2026, 7, 1), 110_000.0, params)
    assert execution.size_deviation is False


def test_position_grown_past_cap_is_flagged_for_trimming(params):
    # Core capacity here is 700,000: both names were sized inside the 10% cap,
    # and only one of them appreciated through it.
    portfolio = _portfolio(
        total=2_000_000.0,
        ballast=1_000_000.0,
        positions=[
            Position("BIG", CORE_STABLE, "Industrials", 50_000.0, 300_000.0),
            Position("SMALL", CORE_STABLE, "Utilities", 50_000.0, 50_000.0),
        ],
    )
    flags = trim_to_cap_flags(portfolio, params)
    assert [f.symbol for f in flags] == ["BIG"]
    assert flags[0].flag == TRIM_TO_CAP
    assert "grown past cap on appreciation" in flags[0].detail


def test_underfilled_sleeve_does_not_flag_correctly_sized_positions(params):
    """Two names in a half-empty sleeve are not oversized just because the sleeve is empty."""
    portfolio = _portfolio(
        total=2_000_000.0,
        ballast=1_000_000.0,
        positions=[
            Position("A", CORE_STABLE, "Industrials", 50_000.0, 50_000.0),
            Position("B", CORE_STABLE, "Utilities", 50_000.0, 50_000.0),
        ],
    )
    assert trim_to_cap_flags(portfolio, params) == []


def test_position_sized_above_cap_at_cost_is_named_as_a_sizing_failure(params):
    portfolio = _portfolio(
        total=2_000_000.0,
        ballast=1_000_000.0,
        positions=[Position("OVER", CORE_STABLE, "Industrials", 200_000.0, 210_000.0)],
    )
    flags = trim_to_cap_flags(portfolio, params)
    assert "sizing failure rather than appreciation" in flags[0].detail


def test_quarterly_discipline_counts_deviations_and_drift(params):
    records = [_record(symbol="A"), _record(symbol="B")]
    executions = [
        log_execution(records[0], date(2026, 7, 1), 160_000.0, params, reason="conviction"),
        log_execution(records[1], date(2026, 7, 1), 100_000.0, params),
    ]
    report = quarterly_discipline_report(records, executions, _portfolio(), params)
    assert report.size_deviation_count == 1
    assert report.cumulative_drift_pct == pytest.approx(30.0)


def test_thesis_broken_flag_is_independent_of_price(params):
    records = [_record(symbol="A")]
    report = quarterly_discipline_report(
        records, [], _portfolio(), params, thesis_conditions_met={"A": True}
    )
    assert any(f.flag == "THESIS BROKEN" for f in report.flags)


def _health(verdict=Verdict.PASS, reasons=None):
    return HealthResult("X", verdict, CORE_STABLE, [], reasons=reasons or [])


def test_growth_deterioration_needs_two_consecutive_quarters(params):
    one_quarter = review_holding(
        "G", SPEC_GROWTH, _health(), None, None, None, None, params,
        growth=GrowthDeterioration(quarters_revenue_growth_below_floor=1),
    )
    assert SELL_FLAG not in one_quarter.flags

    two_quarters = review_holding(
        "G", SPEC_GROWTH, _health(), None, None, None, None, params,
        growth=GrowthDeterioration(quarters_revenue_growth_below_floor=2),
    )
    assert SELL_FLAG in two_quarters.flags


def test_short_runway_with_a_financing_plan_is_not_an_automatic_sell(params):
    funded = review_holding(
        "G", SPEC_GROWTH, _health(), None, None, None, None, params,
        growth=GrowthDeterioration(cash_runway_months=8.0, credible_financing_plan=True),
    )
    assert SELL_FLAG not in funded.flags

    unfunded = review_holding(
        "G", SPEC_GROWTH, _health(), None, None, None, None, params,
        growth=GrowthDeterioration(cash_runway_months=8.0, credible_financing_plan=False),
    )
    assert SELL_FLAG in unfunded.flags


def test_conviction_falling_twenty_points_triggers_review(params):
    from gcfp.modules.d_conviction import ConvictionScore

    now = ConvictionScore("X", health_margin=55.0, momentum_pending=False)
    review = review_holding("X", CORE_STABLE, _health(), None, None, now, 80.0, params)
    assert REVIEW_FLAG in review.flags
    assert review.conviction_change == pytest.approx(-25.0)


def test_classification_change_forces_a_rebuild_review(params):
    review = review_holding(
        "X", CORE_GROWTH, _health(), None, None, None, None, params,
        classification_at_purchase=CORE_STABLE,
    )
    assert REVIEW_FLAG in review.flags
    assert any("rebuilt, not carried forward" in r for r in review.reasons)


def test_price_history_alone_never_produces_a_flag(params):
    """A quiet position with a collapsed price and intact fundamentals stays green."""
    review = review_holding("X", CORE_STABLE, _health(), None, None, None, None, params)
    assert review.flags == []
    assert review.status == "GREEN"
