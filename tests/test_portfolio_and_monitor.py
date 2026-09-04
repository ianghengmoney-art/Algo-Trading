"""Modules F through K, plus the probe's own stop-condition logic."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from gcfp.classification import Classification, Regime
from gcfp.modules import f_sizing, g_execution, h_monitor, i_expectations, j_tax, k_currency
from gcfp.modules.f_sizing import Bucket, PortfolioState
from gcfp.types import CompanyProfile, ReportingFrequency


def portfolio(**kw) -> PortfolioState:
    defaults = dict(
        total_value=1_000_000.0,
        bucket_values={
            Bucket.BALLAST: 500_000.0,
            Bucket.CORE_PICKS: 350_000.0,
            Bucket.GROWTH_PICKS: 100_000.0,
        },
        position_values={"A": 175_000.0, "B": 175_000.0, "C": 100_000.0},
        position_sectors={"A": "Technology", "B": "Healthcare", "C": "Technology"},
        position_regimes={"A": Regime.CORE, "B": Regime.CORE, "C": Regime.GROWTH},
    )
    defaults.update(kw)
    return PortfolioState(**defaults)


class TestModuleF:
    def test_core_picks_are_not_ballast(self):
        """v3's substantive wording error. A CORE-classified stock pick carries
        single-name risk an index fund does not."""
        assert f_sizing.bucket_for(Classification.CORE_STABLE) is Bucket.CORE_PICKS
        assert f_sizing.bucket_for(Classification.REIT) is Bucket.CORE_PICKS
        assert f_sizing.bucket_for(Classification.SPEC_GROWTH) is Bucket.GROWTH_PICKS

    def test_high_conviction_gets_the_larger_core_size(self, config):
        high = f_sizing.size_position(
            "X", Classification.CORE_STABLE, 85.0, "Energy", portfolio(), config
        )
        standard = f_sizing.size_position(
            "X", Classification.CORE_STABLE, 65.0, "Energy", portfolio(), config
        )
        assert high.sleeve_share == 0.08
        assert standard.sleeve_share == 0.04

    def test_growth_positions_land_near_1_5_to_2_25_percent_of_portfolio(self, config):
        """The spec's stated intent: a meaningful fraction will fail outright."""
        # A sector the growth sleeve does not already hold, so the sector cap
        # is not the binding constraint here — that is tested separately below.
        high = f_sizing.size_position(
            "X", Classification.SPEC_GROWTH, 85.0, "Energy", portfolio(), config
        )
        standard = f_sizing.size_position(
            "X", Classification.SPEC_GROWTH, 65.0, "Energy", portfolio(), config
        )
        assert high.portfolio_share == pytest.approx(0.0225)
        assert standard.portfolio_share == pytest.approx(0.015)

    def test_growth_sector_cap_binds_on_a_concentrated_sleeve(self, config):
        """The default fixture's growth sleeve is entirely Technology, which is
        already past the 35% sector cap."""
        decision = f_sizing.size_position(
            "X", Classification.SPEC_GROWTH, 85.0, "Technology", portfolio(), config
        )
        assert not decision.qualified
        assert any("sector Technology" in c for c in decision.constraints)

    def test_a_full_bucket_yields_qualified_no_headroom(self, config):
        full = portfolio(
            bucket_values={
                Bucket.BALLAST: 450_000.0,
                Bucket.CORE_PICKS: 400_000.0,
                Bucket.GROWTH_PICKS: 150_000.0,
            }
        )
        decision = f_sizing.size_position(
            "X", Classification.CORE_STABLE, 85.0, "Energy", full, config
        )
        assert not decision.qualified
        assert "no headroom" in decision.as_report_line()

    def test_sector_cap_trims_rather_than_refuses(self, config):
        """Technology already holds 50% of the CORE sleeve against a 30% cap."""
        concentrated = portfolio(
            position_values={"A": 300_000.0, "B": 50_000.0},
            position_sectors={"A": "Technology", "B": "Healthcare"},
            position_regimes={"A": Regime.CORE, "B": Regime.CORE},
        )
        decision = f_sizing.size_position(
            "X", Classification.CORE_STABLE, 85.0, "Technology", concentrated, config
        )
        assert any("sector" in c for c in decision.constraints)
        assert decision.sleeve_share < 0.08

    def test_ballast_gap_is_reported_before_any_recommendation(self, config):
        low = portfolio(
            bucket_values={
                Bucket.BALLAST: 300_000.0,
                Bucket.CORE_PICKS: 400_000.0,
                Bucket.GROWTH_PICKS: 300_000.0,
            }
        )
        lines = f_sizing.opening_report_lines(low, config)
        assert "BALLAST GAP" in lines[0]

    def test_c5_portfolio_cap_blocks_a_further_single_anchor_name(self, config):
        at_cap = portfolio(single_anchor_positions={"A"})
        decision = f_sizing.size_position(
            "X", Classification.CORE_STABLE, 85.0, "Energy", at_cap, config,
            single_anchor=True,
        )
        # One of three positions is already single-anchor: 33% against a 20% cap.
        assert not decision.qualified
        assert any("SINGLE-ANCHOR" in c for c in decision.constraints)


class TestModuleG:
    def test_a_recommendation_requires_a_falsifiable_condition(self, config):
        from gcfp.modules.c_anchors import AnchorMode
        from gcfp.modules.d_conviction import ConvictionScore

        with pytest.raises(ValueError, match="falsifiable"):
            g_execution.build_recommendation(
                "X", Classification.CORE_STABLE, ConvictionScore("X", 70.0, [], "standard"),
                100.0, "B1", 70.0, "C1 ok", "C2 ok", AnchorMode.DUAL,
                0.04, 40_000.0, "SGD", "   ", config,
            )

    def test_deviation_beyond_25_percent_requires_a_reason(self, config):
        with pytest.raises(ValueError, match="requires a logged reason"):
            g_execution.log_execution("X", 40_000.0, 60_000.0, config)

    def test_deviation_flag_is_set_when_a_reason_is_given(self, config):
        record = g_execution.log_execution(
            "X", 40_000.0, 60_000.0, config, reason="scaled in over two days"
        )
        assert record.size_deviation
        assert record.deviation == pytest.approx(0.50)

    def test_small_deviation_needs_no_reason_and_sets_no_flag(self, config):
        record = g_execution.log_execution("X", 40_000.0, 44_000.0, config)
        assert not record.size_deviation

    def test_trim_to_cap_fires_on_appreciation(self):
        report = g_execution.quarterly_review(
            executions=[],
            position_shares={"WINNER": 0.14, "NORMAL": 0.05},
            name_caps={"WINNER": 0.10, "NORMAL": 0.10},
            thesis_broken=[],
        )
        assert report.trim_to_cap == ["WINNER"]
        assert any("unmanaged risk" in l for l in report.as_report_lines())


class TestModuleH:
    def test_semiannual_reporter_gets_a_midpoint_check(self, config):
        from tests.conftest import build_company, stable_profile

        last = date.today() - timedelta(days=200)
        company = build_company(
            profile=stable_profile(
                reporting_frequency=ReportingFrequency.SEMIANNUAL,
                last_report_date=last,
            )
        )
        decision = h_monitor.cadence(company, config, last_full_rerun=last)
        assert decision.should_price_and_news_check
        assert not decision.should_full_rerun

    def test_a_new_report_triggers_a_full_rerun(self, config):
        from tests.conftest import build_company, stable_profile

        company = build_company(
            profile=stable_profile(last_report_date=date.today() - timedelta(days=5))
        )
        decision = h_monitor.cadence(
            company, config, last_full_rerun=date.today() - timedelta(days=100)
        )
        assert decision.should_full_rerun

    def test_absolute_conviction_band_fires_where_a_relative_drop_would_not(
        self, config
    ):
        """v3 flaw 7: bought at 62, drifted to 45 — a 17-point drop, below the
        20-point trigger, but squarely in marginal territory."""
        verdict = h_monitor.evaluate_holding(
            "X", Classification.CORE_STABLE, config,
            module_a_failed=False, both_anchors_overvalued=False,
            premium_above_threshold=False, thesis_broken=False,
            conviction_now=45.0, conviction_at_purchase=62.0,
        )
        assert verdict.flag is h_monitor.MonitorFlag.REVIEW
        assert any("absolute terms" in r for r in verdict.review_reasons)

    def test_conviction_below_40_is_a_sell_regardless_of_drop(self, config):
        verdict = h_monitor.evaluate_holding(
            "X", Classification.CORE_STABLE, config,
            module_a_failed=False, both_anchors_overvalued=False,
            premium_above_threshold=False, thesis_broken=False,
            conviction_now=38.0, conviction_at_purchase=42.0,
        )
        assert verdict.flag is h_monitor.MonitorFlag.SELL

    def test_growth_deterioration_needs_two_consecutive_reports(self, config):
        one = h_monitor.evaluate_holding(
            "X", Classification.SPEC_GROWTH, config,
            module_a_failed=False, both_anchors_overvalued=False,
            premium_above_threshold=False, thesis_broken=False,
            conviction_now=70.0, conviction_at_purchase=70.0,
            growth=h_monitor.GrowthDeterioration(revenue_growth_below_10_streak=1),
        )
        two = h_monitor.evaluate_holding(
            "X", Classification.SPEC_GROWTH, config,
            module_a_failed=False, both_anchors_overvalued=False,
            premium_above_threshold=False, thesis_broken=False,
            conviction_now=70.0, conviction_at_purchase=70.0,
            growth=h_monitor.GrowthDeterioration(revenue_growth_below_10_streak=2),
        )
        assert one.flag is h_monitor.MonitorFlag.GREEN
        assert two.flag is h_monitor.MonitorFlag.SELL

    def test_h3_freezes_the_position_until_all_six_steps_complete(self):
        protocol = h_monitor.start_reclassification(
            "X", Classification.SPEC_GROWTH, Classification.CORE_GROWTH
        )
        assert protocol.frozen
        for step in range(1, 7):
            protocol = h_monitor.complete_reclassification_step(protocol, step)
        assert not protocol.frozen

    def test_h3_reclassification_is_a_review_not_an_automatic_sell(self):
        """A business that grew from SPEC-GROWTH into CORE-GROWTH has improved;
        forcing an exit would sell exactly the winners the system exists to find."""
        protocol = h_monitor.start_reclassification(
            "X", Classification.SPEC_GROWTH, Classification.CORE_GROWTH
        )
        protocol = h_monitor.resolve_reclassification(protocol, would_be_bought_today=False)
        assert "REVIEW FOR EXIT" in protocol.outcome
        assert "not an automatic sell" in protocol.outcome


class TestModuleI:
    def test_process_criteria_are_evaluable_before_20_closed_positions(self, config):
        """v3's only break criterion could take a decade to evaluate."""
        report = i_expectations.evaluate_break_criteria(
            config, closed_positions=3, single_anchor_rate=0.55
        )
        process = [c for c in report.criteria if c.kind == "process"]
        assert any(c.tripped for c in process)
        outcome = [c for c in report.criteria if c.kind == "outcome"]
        assert not any(c.tripped for c in outcome)

    def test_divergence_above_50_percent_trips(self, config):
        report = i_expectations.evaluate_break_criteria(
            config, closed_positions=0, divergence_rate=0.60
        )
        assert report.any_tripped

    def test_drawdown_is_not_a_valid_reason_to_abandon(self):
        assert "drawdown depth" in i_expectations.NOT_VALID_REASONS

    def test_expectation_statement_is_printed_verbatim(self, config):
        lines = i_expectations.report_header_lines(config)
        assert i_expectations.EXPECTATION_STATEMENT in "\n".join(lines)


class TestModuleJ:
    def test_us_dividend_is_reported_after_withholding(self, config):
        """A 4% US dividend is 2.8% in hand."""
        assessment = j_tax.assess_yield(0.04, "NYSE", Classification.CORE_STABLE, config)
        assert assessment.after_withholding_yield == pytest.approx(0.028)
        assert assessment.thesis_rests_on_yield

    def test_sgx_reit_distributions_are_exempt(self, config):
        assessment = j_tax.assess_yield(0.06, "SGX", Classification.REIT, config)
        assert assessment.exempt
        assert assessment.after_withholding_yield == pytest.approx(0.06)

    def test_zero_capital_gains_means_a_trim_carries_no_cost(self, config):
        assert not j_tax.trim_carries_tax_cost(config)
        assert "no offsetting cost" in j_tax.trim_note(config)


class TestModuleK:
    def test_a_missing_rate_raises_rather_than_defaulting_to_one(self):
        table = k_currency.FxTable({"USDSGD": 1.29}, date.today())
        with pytest.raises(k_currency.FxRateUnavailable):
            table.convert(100.0, "EUR", "SGD")

    def test_inverse_rates_are_derived(self):
        table = k_currency.FxTable({"USDSGD": 1.29}, date.today())
        amount, quote = table.convert(129.0, "SGD", "USD")
        assert amount == pytest.approx(100.0)

    def test_adr_underlying_exposure_is_reported_separately(self, config):
        tsm = CompanyProfile(
            symbol="TSM", name="TSMC", listing_currency="USD",
            is_adr=True, underlying_currency="TWD",
        )
        cat = CompanyProfile(symbol="CAT", name="Caterpillar", listing_currency="USD")
        report = k_currency.exposure_report([(tsm, 250_000), (cat, 750_000)], config)
        assert report.by_trading_currency["USD"] == pytest.approx(1.0)
        assert report.by_underlying_currency["TWD"] == pytest.approx(0.25)

    def test_an_unmapped_adr_is_not_credited_to_the_trading_currency(self, config):
        """An operator who believes they hold three currencies may hold five."""
        unmapped = CompanyProfile(
            symbol="X", name="X", listing_currency="USD",
            is_adr=True, underlying_currency=None,
        )
        report = k_currency.exposure_report([(unmapped, 100_000)], config)
        assert report.by_underlying_currency["UNMAPPED"] == pytest.approx(1.0)
        assert "USD" not in report.by_underlying_currency
