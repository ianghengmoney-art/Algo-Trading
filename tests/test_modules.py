"""Gate arithmetic and scoring formulas, tested against the spec's own numbers.

Where the spec states a value — D2's table, C5's penalties, D4's point scales —
the test asserts that value rather than whatever the implementation produces.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from gcfp.classification import Classification, Regime
from gcfp.data.fixtures import make_multiple_series
from gcfp.ledger import AuditLedger, Outcome
from gcfp.modules import a_health, b_valuation, c_anchors, d_conviction, e_triggers
from gcfp.config import Config
from gcfp.types import (
    AuditorEvent,
    CorporateAction,
    CorporateActionType,
    MarketData,
    TaxonomyLevel,
)
from tests.conftest import build_company, stable_profile


# -- Module A -------------------------------------------------------------


class TestModuleA:
    def test_a1_negative_working_capital_is_not_an_automatic_fail(self, config):
        """Structurally negative working capital is a feature in some
        businesses, never an automatic fail."""
        company = build_company(
            quarterly_kw=dict(total_current_assets=6e9, total_current_liabilities=14e9)
        )
        result = a_health.gate_a1_solvency(company, config)
        assert result.passed
        assert result.branch == "operating_cash_flow"

    def test_a1_fails_when_neither_branch_holds(self, config):
        company = build_company(
            quarterly_kw=dict(
                total_current_assets=6e9,
                total_current_liabilities=14e9,
                operating_cash_flow=-500e6,
            )
        )
        assert a_health.gate_a1_solvency(company, config).failed

    def test_a2_escalates_when_the_grouping_is_thin(self, config, market):
        """A grouping with too few computable members escalates to the next
        rung, and the level used is logged."""
        import dataclasses

        thin = dataclasses.replace(
            market,
            group_member_counts={"Machinery": 3, "Industrials": 40},
            group_net_debt_ebitda_median={"Machinery": 1.8, "Industrials": 2.2},
        )
        ledger = AuditLedger("STABLECO", date.today())
        result, grouping = a_health.gate_a2_leverage(
            build_company(), thin, config, ledger
        )
        assert grouping.level is TaxonomyLevel.SECTOR
        assert grouping.meets_minimum
        assert result.branch == "sector"
        assert result.detail["group_median"] == 2.2, (
            "escalating must also switch to the escalated grouping's median"
        )
        assert any("VENDOR-SUBSTITUTE" in n for n in ledger.notes)

    def test_a2_refuses_when_every_rung_is_too_thin(self, config, market):
        """Escalation exists because a thin grouping's median is untrustworthy.
        Falling back on it when the ladder is exhausted would defeat that."""
        import dataclasses

        exhausted = dataclasses.replace(
            market,
            group_member_counts={"Machinery": 3, "Industrials": 4},
            group_net_debt_ebitda_median={"Machinery": 1.8, "Industrials": 2.2},
        )
        result, grouping = a_health.gate_a2_leverage(
            build_company(), exhausted, config
        )
        assert result.outcome is Outcome.NOT_COMPUTABLE
        assert not grouping.meets_minimum

    def test_a3_catches_profit_not_backed_by_cash(self, config):
        """The unrealised-gains pattern: GAAP profit with weak operating cash."""
        company = build_company(
            quarterly_kw=dict(net_income=2e9, operating_cash_flow=1e9)
        )
        result = a_health.gate_a3_earnings_quality(company, config)
        assert result.failed
        assert result.branch == "profitable"
        assert result.value == pytest.approx(0.5)
        assert "unrealised" in (result.reason or "")

    def test_a3_passes_at_exactly_the_threshold(self, config):
        company = build_company(
            quarterly_kw=dict(net_income=1e9, operating_cash_flow=800e6)
        )
        result = a_health.gate_a3_earnings_quality(company, config)
        assert result.passed
        assert result.value == pytest.approx(0.80)

    def test_a3_pre_profit_branch_uses_runway(self, config):
        company = build_company(
            quarterly_kw=dict(
                net_income=-500e6,
                operating_cash_flow=-250e6,
                cash_and_equivalents=3e9,
            )
        )
        result = a_health.gate_a3_earnings_quality(company, config)
        assert result.branch == "pre_profit_runway"
        # 3e9 cash / (1e9 annual burn / 12) = 36 months.
        assert result.value == pytest.approx(36.0, rel=0.01)
        assert result.passed

    def test_a4_missing_share_history_is_uncomputable_not_a_pass(self, config):
        """No dilution and no data must not look alike."""
        company = build_company(quarterly_kw=dict(shares=None))
        result = a_health.gate_a4_red_flags(company, config)
        assert result.outcome is Outcome.NOT_COMPUTABLE

    def test_a4_flags_an_auditor_change_without_a_benign_reason(self, config):
        company = build_company(
            auditor_events=[
                AuditorEvent(date.today() - timedelta(days=60), changed=True, benign=False)
            ]
        )
        assert a_health.gate_a4_red_flags(company, config).failed

    def test_a4_accepts_a_benign_auditor_change(self, config):
        company = build_company(
            auditor_events=[
                AuditorEvent(
                    date.today() - timedelta(days=60),
                    changed=True,
                    benign=True,
                    stated_reason="mandatory rotation",
                )
            ]
        )
        assert a_health.gate_a4_red_flags(company, config).passed

    def test_a5_never_imputes_a_missing_input(self, config):
        company = build_company()
        stale = company.__class__(
            **{**company.__dict__, "source_notes": ("annual unavailable: no rows",)}
        )
        result = a_health.gate_a5_data_integrity(stale, config, growth_routed=False)
        assert result.failed

    def test_a6_ambiguity_rule_prefers_structure_over_profitability(self, config):
        """A REIT's accounting makes the CORE-STABLE tests meaningless
        regardless of its growth rate."""
        company = build_company(profile=stable_profile(symbol="REITCO", is_reit=True))
        tag, considered, _ = a_health.classify(company, config)
        assert tag is Classification.REIT
        assert Classification.CORE_STABLE in considered, (
            "the test is only meaningful if both tags were genuinely available"
        )

    def test_a6_growth_routing_tightens_the_liquidity_floor(self, config):
        thin = build_company(
            profile=stable_profile(adv_3m_usd=3e6),
            annual_kw=dict(revenue_growth=0.45),
        )
        assert a_health.gate_universe(thin, config, growth_routed=True).failed
        assert a_health.gate_universe(thin, config, growth_routed=False).passed


# -- Module B -------------------------------------------------------------


class TestModuleB:
    def test_b1_flags_a_terminal_value_above_75_percent(self, market, config):
        """A model valuing a perpetuity assumption, not a business.

        Under the default parameters this flag is structurally hard to trip:
        a 10-year explicit period, a 2.5% terminal cap and a 9% discount floor
        together hold the terminal share around 55-67% across any plausible
        trailing growth rate. That is the constraints doing their job — but the
        flag still has to work, so it is exercised against a shortened
        projection, which is the parameter change that would reintroduce the
        pathology.
        """
        import dataclasses

        short = dataclasses.replace(
            config,
            valuation=dataclasses.replace(
                config.valuation, b1_projection_years=5, b1_stage_one_years=2
            ),
        )
        company = build_company(annual_kw=dict(revenue_growth=0.12))
        result = b_valuation.value_b1_core_stable(company, market, short)
        assert result.terminal_value_share > 0.75
        assert any("TERMINAL VALUE" in f for f in result.flags)

    def test_b1_default_parameters_keep_the_terminal_share_bounded(
        self, market, config
    ):
        """The flag rarely fires because the other constraints already prevent
        what it warns about. Asserting that keeps a future parameter change
        from quietly reintroducing the pathology unnoticed."""
        for growth in (0.05, 0.15, 0.30):
            result = b_valuation.value_b1_core_stable(
                build_company(annual_kw=dict(revenue_growth=growth)), market, config
            )
            assert result.terminal_value_share < 0.75, (
                f"terminal share reached {result.terminal_value_share:.1%} at "
                f"{growth:.0%} trailing growth under default parameters"
            )

    def test_b1_reports_the_half_growth_rerun(self, healthy_company, market, config):
        result = b_valuation.value_b1_core_stable(healthy_company, market, config)
        assert result.half_growth_fair_value is not None
        assert result.half_growth_fair_value < result.fair_value_per_share

    def test_discount_rate_never_assumes_beta_of_one(self, market, config):
        """A missing beta is an A5 data gap, not an assumption."""
        from gcfp.data.adapter import DataUnavailable
        from gcfp.modules.b_discount import build_discount_rate

        company = build_company(profile=stable_profile(beta=None))
        with pytest.raises(DataUnavailable, match="1.0 is not assumed"):
            build_discount_rate(company, market, config, Classification.CORE_STABLE)

    def test_discount_rate_uses_a_group_median_beta_as_a_flagged_proxy(
        self, market, config
    ):
        import dataclasses

        from gcfp.modules.b_discount import build_discount_rate

        with_median = dataclasses.replace(market, group_beta_median={"Machinery": 1.2})
        company = build_company(profile=stable_profile(beta=None))
        rate = build_discount_rate(
            company, with_median, config, Classification.CORE_STABLE
        )
        assert rate.beta == 1.2
        assert rate.beta_is_proxy
        assert "BETA: PROXY" in rate.flags

    def test_growth_floors_are_higher_and_never_lowered(self, market, config):
        from gcfp.modules.b_discount import classification_floor

        assert classification_floor(Classification.SPEC_GROWTH, config) == 0.12
        assert classification_floor(Classification.CORE_GROWTH, config) == 0.10
        assert classification_floor(Classification.CORE_STABLE, config) == 0.09

    def test_b2_1_uses_the_lower_figure_when_sources_disagree(self, config):
        from gcfp.types import TamSource

        sources = [
            TamSource("Analyst A", date(2026, 1, 1), 100e9),
            TamSource("Analyst B", date(2026, 2, 1), 300e9),
        ]
        company = build_company()
        assessment = b_valuation.assess_tam(company, config, sources, 20e9)
        assert "TAM DISPUTED" in assessment.flags
        assert assessment.tam == 100e9

    def test_b2_1_refuses_an_uncited_tam(self, config):
        company = build_company()
        assessment = b_valuation.assess_tam(company, config, [], 20e9)
        assert not assessment.computable
        assert any("NOT COMPUTABLE" in f for f in assessment.flags)

    def test_b2_1_proxy_is_flagged_as_indicative_only(self, config):
        company = build_company()
        assessment = b_valuation.assess_tam(
            company, config, [], 20e9, industry_revenue=50e9, industry_cagr=0.08
        )
        assert assessment.is_proxy
        assert any("TAM: PROXY" in f for f in assessment.flags)

    def test_b2_1_flags_a_breached_ceiling(self, config):
        from gcfp.types import TamSource

        sources = [
            TamSource("A", date(2026, 1, 1), 100e9),
            TamSource("B", date(2026, 1, 1), 110e9),
        ]
        company = build_company()
        assessment = b_valuation.assess_tam(company, config, sources, 60e9)
        assert any("TAM CEILING BREACHED" in f for f in assessment.flags)

    def test_b4_refuses_a_reit_without_ffo(self, market, config):
        """P/E is banned on this path, so no FFO means no valuation."""
        from gcfp.data.adapter import DataUnavailable

        reit = build_company(profile=stable_profile(symbol="REITCO", is_reit=True))
        with pytest.raises(DataUnavailable, match="P/E is banned"):
            b_valuation.value_b4_reit(reit, market, config, peer_price_to_affo=16.0)


# -- Module C -------------------------------------------------------------


class TestModuleC:
    def test_c1_1_truncates_at_a_spinoff(self, config):
        company = build_company(
            corporate_actions=[
                CorporateAction(
                    CorporateActionType.SPINOFF, date.today() - timedelta(days=400)
                )
            ]
        )
        reading = c_anchors.compute_c1(
            company, config, Classification.CORE_STABLE, 14.0
        )
        assert not reading.computable
        assert "C5" in (reading.reason or "")

    def test_c1_1_ignores_a_small_acquisition(self, config):
        """Only transformative events break the series."""
        company = build_company(
            corporate_actions=[
                CorporateAction(
                    CorporateActionType.ACQUISITION,
                    date.today() - timedelta(days=400),
                    market_cap_share=0.05,
                )
            ]
        )
        assert c_anchors.compute_c1(
            company, config, Classification.CORE_STABLE, 14.0
        ).computable

    def test_c1_2_detects_a_sustained_re_rating(self, config):
        """The Micron DRAM-to-HBM and Apple hardware-to-Services problem."""
        stepped = [40, 41, 39, 42, 40, 41, 38, 40, 41, 39, 40, 42, 41, 40] + [
            15, 16, 14, 15, 16, 15, 14, 16, 15, 15, 16, 14, 15, 16
        ]
        series = make_multiple_series("trailing_pe", stepped)
        detected, message = c_anchors.detect_re_rating(series)
        assert detected
        assert "POSSIBLE RE-RATING" in message

    def test_c1_2_does_not_fire_on_noise(self, config):
        noisy = [20, 21, 19, 22, 18, 20, 21, 19, 20, 22, 18, 21, 19, 20] * 2
        detected, _ = c_anchors.detect_re_rating(
            make_multiple_series("trailing_pe", noisy)
        )
        assert not detected

    def test_c2_logs_every_rejection_with_a_reason(self, config):
        candidates = [
            c_anchors.PeerCandidate("GOOD1", 19.0, 60e9, 0.05, "G"),
            c_anchors.PeerCandidate("TOOBIG", 25.0, 500e9, 0.05, "G"),
            c_anchors.PeerCandidate("FASTGROW", 40.0, 50e9, 0.45, "G"),
            c_anchors.PeerCandidate("OTHERSECTOR", 30.0, 45e9, 0.05, "OTHER"),
        ]
        kept, decisions = c_anchors.select_peers(
            "G", 80e9, 0.05, candidates, config
        )
        assert len(decisions) == 4, "every candidate must get a logged decision"
        assert all(d.reason for d in decisions)
        rejected = {d.candidate.symbol: d.reason for d in decisions if not d.included}
        assert "market cap" in rejected["TOOBIG"]
        assert "revenue growth" in rejected["FASTGROW"]
        assert "different grouping" in rejected["OTHERSECTOR"]

    def test_c2_uses_the_median_never_the_mean(self, healthy_company, config):
        """One extreme peer must not drag the reference."""
        candidates = [
            c_anchors.PeerCandidate(f"P{i}", m, 60e9, 0.05, "G")
            for i, m in enumerate([18.0, 19.0, 20.0, 21.0, 200.0])
        ]
        reading = c_anchors.compute_c2(
            healthy_company, config, 15.0, candidates, "G", 0.05
        )
        assert reading.reference_multiple == pytest.approx(20.0)

    def test_c3_pegy_is_never_imputed(self, config):
        """Where undefined, log n/a — never substitute a number."""
        company = build_company(forward_eps_growth=None)
        pegy, flagged = c_anchors.compute_c3_pegy(company, config, 20.0)
        assert pegy is None and not flagged

    def test_c4_divergence_refuses_a_combined_verdict(self, healthy_company, config):
        c1 = c_anchors.AnchorReading("C1", True, 14.0, 20.0, implied_discount=0.40)
        c2 = c_anchors.AnchorReading("C2", True, 14.0, 15.0, implied_discount=0.05)
        result = c_anchors.triangulate(healthy_company, config, c1, c2)
        assert result.anchors_disagree
        assert result.divergence == pytest.approx(0.35)

    def test_c5_both_uncomputable_is_an_automatic_fail(self, healthy_company, config):
        c1 = c_anchors.AnchorReading("C1", False, reason="no history")
        c2 = c_anchors.AnchorReading("C2", False, reason="no peers")
        result = c_anchors.triangulate(healthy_company, config, c1, c2)
        assert result.mode is c_anchors.AnchorMode.NONE
        assert not result.both_confirm_undervaluation

    def test_c5_single_anchor_raises_the_buy_threshold_by_10pp(self, config):
        """A CORE-STABLE name needs 35% instead of 25%."""
        assert config.buy_threshold(Classification.CORE_STABLE, False) == 0.25
        assert config.buy_threshold(Classification.CORE_STABLE, True) == pytest.approx(0.35)
        assert config.buy_threshold(Classification.SPEC_GROWTH, True) == pytest.approx(0.50)


# -- Module D -------------------------------------------------------------


class TestModuleD:
    @pytest.mark.parametrize(
        "discount,expected",
        [(0.25, 0.0), (0.30, 6.0), (0.35, 12.0), (0.40, 18.0), (0.45, 24.0), (0.50, 30.0)],
    )
    def test_d2_reproduces_the_specs_table(self, discount, expected, config):
        score = d_conviction.score_valuation_excess(discount, 0.25, config)
        assert score.points == pytest.approx(expected)

    def test_d2_is_capped_at_full_marks(self, config):
        assert d_conviction.score_valuation_excess(0.80, 0.25, config).points == 30.0

    def test_d3_scores_the_conservative_anchor_not_the_agreement(
        self, healthy_company, config
    ):
        """v3 gave 15 points for 'both anchors confirm', which the gate already
        required, so every passer scored 15 and the component said nothing."""
        barely = c_anchors.triangulate(
            healthy_company,
            config,
            c_anchors.AnchorReading("C1", True, 14.0, 20.0, implied_discount=0.26),
            c_anchors.AnchorReading("C2", True, 14.0, 20.0, implied_discount=0.30),
        )
        deeply = c_anchors.triangulate(
            healthy_company,
            config,
            c_anchors.AnchorReading("C1", True, 10.0, 20.0, implied_discount=0.45),
            c_anchors.AnchorReading("C2", True, 10.0, 20.0, implied_discount=0.50),
        )
        barely_points = d_conviction.score_anchor_conservatism(barely, config).points
        deep_points = d_conviction.score_anchor_conservatism(deeply, config).points
        assert deep_points > barely_points, (
            "both pairs confirm; only the depth distinguishes them"
        )

    def test_d3_is_halved_in_single_anchor_mode(self, healthy_company, config):
        """One anchor cannot corroborate itself."""
        dual = c_anchors.triangulate(
            healthy_company,
            config,
            c_anchors.AnchorReading("C1", True, 10.0, 20.0, implied_discount=0.40),
            c_anchors.AnchorReading("C2", True, 10.0, 20.0, implied_discount=0.40),
        )
        single = c_anchors.triangulate(
            healthy_company,
            config,
            c_anchors.AnchorReading("C1", True, 10.0, 20.0, implied_discount=0.40),
            c_anchors.AnchorReading("C2", False, reason="only 2 peers"),
        )
        assert d_conviction.score_anchor_conservatism(dual, config).points == 15.0
        assert d_conviction.score_anchor_conservatism(single, config).points == 7.5

    def test_d5_awards_neutral_when_fewer_than_three_passers(self, config):
        """Ranking a set of one or two is meaningless."""
        for returns in ({"A": 0.2}, {"A": 0.2, "B": 0.1}):
            score = d_conviction.score_momentum("A", returns, config)
            assert score.points == 5.0
            assert score.detail["neutral"] is True

    def test_d5_ranks_once_three_passers_exist(self, config):
        returns = {"A": 0.30, "B": 0.10, "C": 0.20}
        assert d_conviction.score_momentum("A", returns, config).points == 10.0
        assert d_conviction.score_momentum("B", returns, config).points == 0.0
        assert d_conviction.score_momentum("C", returns, config).points == 5.0

    def test_d4_share_count_scale_matches_the_spec(self, config, market):
        """5 pts shrinking >=2%/yr · 3 pts flat to -2% · 1 pt growing <=3% · 0 above."""
        from gcfp.modules.b_discount import build_discount_rate

        expectations = [(-0.05, 5.0), (-0.01, 3.0), (0.02, 1.0), (0.10, 0.0)]
        for growth, expected in expectations:
            company = build_company(quarterly_kw=dict(share_growth=growth))
            rate = build_discount_rate(
                company, market, config, Classification.CORE_STABLE
            )
            score = d_conviction.score_business_quality(
                company, market, config, Classification.CORE_STABLE, rate
            )
            assert score.detail["share_count"] == expected, f"at {growth:+.0%}"

    def test_conviction_is_capped_at_70_in_single_anchor_mode(
        self, market, config
    ):
        company = build_company()
        ledger = AuditLedger("STABLECO", date.today())
        health = a_health.run_module_a(company, market, config, ledger)
        single = c_anchors.triangulate(
            company,
            config,
            c_anchors.AnchorReading("C1", True, 8.0, 20.0, implied_discount=0.60),
            c_anchors.AnchorReading("C2", False, reason="only 1 peer"),
        )
        score = d_conviction.score_conviction(
            company, market, config, Classification.CORE_STABLE, health, single,
            actual_discount=0.60, gate_threshold=0.35, discount_rate=None,
            momentum_returns={"STABLECO": 0.5, "B": 0.1, "C": 0.2},
        )
        assert score.total <= 70.0
        assert score.capped_at == 70.0


# -- Module E -------------------------------------------------------------


class TestModuleE:
    def test_earnings_blackout_suppresses_the_alert_not_the_pass(self, config):
        soon = date.today() + timedelta(days=5)
        assert e_triggers.in_earnings_blackout(soon, config)
        assert not e_triggers.in_earnings_blackout(
            date.today() + timedelta(days=40), config
        )
        assert not e_triggers.in_earnings_blackout(None, config)

    def test_buy_requires_all_three_conditions(self, config):
        from gcfp.modules.c_anchors import AnchorMode, AnchorReading, TriangulationResult
        from gcfp.modules.d_conviction import ConvictionScore

        fv = b_valuation.FairValue(
            symbol="X", classification=Classification.CORE_STABLE, method="B1",
            fair_value_per_share=100.0, currency="USD",
        )
        confirming = TriangulationResult(
            symbol="X",
            c1=AnchorReading("C1", True, 14.0, 20.0, implied_discount=0.30),
            c2=AnchorReading("C2", True, 14.0, 20.0, implied_discount=0.30),
            c3_pegy=None, c3_flag=False, mode=AnchorMode.DUAL,
            anchors_disagree=False, divergence=0.0, conservative_discount=0.30,
        )
        high = ConvictionScore("X", 75.0, [], "standard")
        low = ConvictionScore("X", 55.0, [], "marginal")

        # 30% discount, both anchors confirm, conviction 75 -> BUY.
        assert e_triggers.evaluate_buy(
            "X", Classification.CORE_STABLE, 70.0, fv, confirming, high, config
        ).signal is e_triggers.SignalType.BUY

        # Same, but conviction below 60 -> no action.
        assert e_triggers.evaluate_buy(
            "X", Classification.CORE_STABLE, 70.0, fv, confirming, low, config
        ).signal is e_triggers.SignalType.NO_ACTION

        # Same conviction, but only a 10% discount -> no action.
        assert e_triggers.evaluate_buy(
            "X", Classification.CORE_STABLE, 90.0, fv, confirming, high, config
        ).signal is e_triggers.SignalType.NO_ACTION

    def test_mixed_sell_signal_is_a_review_not_a_trigger(self, config):
        from gcfp.modules.c_anchors import AnchorMode, AnchorReading, TriangulationResult

        fv = b_valuation.FairValue(
            symbol="X", classification=Classification.CORE_STABLE, method="B1",
            fair_value_per_share=100.0, currency="USD",
        )
        # Price 30% above fair value, but anchors say undervalued.
        mixed = TriangulationResult(
            symbol="X",
            c1=AnchorReading("C1", True, 14.0, 20.0, implied_discount=0.30),
            c2=AnchorReading("C2", True, 14.0, 20.0, implied_discount=0.30),
            c3_pegy=None, c3_flag=False, mode=AnchorMode.DUAL,
            anchors_disagree=False, divergence=0.0, conservative_discount=0.30,
        )
        signal = e_triggers.evaluate_sell(
            "X", Classification.CORE_STABLE, 130.0, fv, mixed, config,
            module_a_failed=False,
        )
        assert signal.signal is e_triggers.SignalType.REVIEW


def _debt_free_company(basis: str = "summed"):
    """A company with no debt at all — total_debt 0.0, no interest expense."""
    return build_company(
        annual_kw={
            "total_debt": 0.0,
            "debt_basis": basis,
            "interest_expense": None,
        },
        quarterly_kw={"total_debt": 0.0, "debt_basis": basis},
    )


class TestDebtFreeDiscountRate:
    """A company with no debt has no cost of debt — that is an answer, not a gap."""

    def test_debt_free_wacc_is_cost_of_equity_and_is_not_flagged_as_missing(self):
        from gcfp.modules import b_discount

        data = _debt_free_company()
        dr = b_discount.build_discount_rate(
            data, MarketData(risk_free_rate=0.04), Config(), Classification.CORE_STABLE
        )
        assert dr.method == "cost_of_equity_debt_free"
        assert dr.rate == pytest.approx(dr.cost_of_equity)
        assert not any("NOT COMPUTABLE" in f for f in dr.flags), (
            "a debt-free balance sheet is not a data gap"
        )
        assert any("DEBT-FREE" in f for f in dr.flags)

    def test_an_inferred_zero_says_so_in_the_flag(self):
        from gcfp.modules import b_discount

        data = _debt_free_company(basis="inferred_zero")
        dr = b_discount.build_discount_rate(
            data, MarketData(risk_free_rate=0.04), Config(), Classification.CORE_STABLE
        )
        assert any("BY INFERENCE" in f for f in dr.flags)


class TestClassificationOnThinData:
    """Absence of evidence must not become evidence of unprofitability.

    TSM is profitable, trillion-dollar, and files 20-F rather than 10-Q, so it
    has no quarterly facts at all. The probe routed it to SPEC-GROWTH — the
    single riskiest classification — because an empty quarter list summed to
    zero, and zero is not greater than zero.
    """

    def test_an_empty_quarter_list_is_not_a_zero_ttm(self):
        data = build_company(quarterly_kw={"count": 0})
        assert a_health._ttm(data, "net_income") is None
        assert a_health.is_profitable_ttm(data) is None

    def test_a_partial_year_is_not_a_trailing_twelve_months(self):
        """Three quarters summed and called TTM understates a year by a
        quarter, and nothing downstream can tell."""
        data = build_company(quarterly_kw={"count": 3})
        assert a_health._ttm(data, "net_income") is None

    def test_a_profitable_filer_with_no_quarterly_data_is_not_spec_growth(self):
        data = build_company(quarterly_kw={"count": 0})
        tag, considered, reasons = a_health.classify(data, Config())
        assert Classification.SPEC_GROWTH not in considered, reasons
        assert tag is not Classification.SPEC_GROWTH

    def test_refusing_to_classify_names_the_missing_inputs(self):
        """"Satisfied no criteria" reads as a verdict on the company. It is
        usually a verdict on the data, and the two need different responses."""
        data = build_company(quarterly_kw={"count": 0}, annual_kw={"count": 0})
        _, _, reasons = a_health.classify(data, Config())
        assert "data gap" in " ".join(reasons)


class TestUnderstatedDebt:
    """A partial debt read is worse than a missing one.

    A chain that reads none of a filer's debt refuses the gate. A chain that
    reads *some* of it hands A2 a plausible number, and nothing downstream can
    tell it is a fraction of the real figure. Realty Income came back with
    $0.8bn of debt against roughly $20bn of liabilities, and passed.
    """

    @staticmethod
    def company(debt, assets, equity):
        bs = {"total_debt": debt, "total_assets": assets, "total_equity": equity}
        return build_company(annual_kw=dict(bs), quarterly_kw=dict(bs))

    def test_a_leveraged_company_reading_almost_no_debt_is_flagged(self):
        flag = a_health._debt_looks_understated(
            self.company(0.85e9, 60e9, 40e9)
        )
        assert flag is not None and "part of the balance sheet" in flag

    def test_a_genuinely_debt_free_company_is_not_flagged(self):
        """Its liabilities are small too, so there is nothing for the debt to
        be a suspicious fraction of."""
        assert a_health._debt_looks_understated(
            self.company(0.0, 10e9, 9.5e9)
        ) is None

    def test_a_normally_leveraged_company_is_not_flagged(self):
        assert a_health._debt_looks_understated(
            self.company(15e9, 60e9, 40e9)
        ) is None

    def test_the_flag_reports_and_does_not_block(self):
        """A2 must still return its verdict — this is a note on the record,
        not a refusal, because the debt figure may simply be right."""
        ledger = AuditLedger("TEST", date(2026, 1, 1))
        result, _ = a_health.gate_a2_leverage(
            self.company(0.85e9, 60e9, 40e9),
            MarketData(
                group_net_debt_ebitda_median={"Machinery": 2.0},
                group_member_counts={"Machinery": 22},
            ),
            Config(),
            ledger,
        )
        assert result.outcome is not Outcome.NOT_COMPUTABLE
        assert any("UNDERSTATED" in n for n in ledger.notes)


class TestGatesThatDoNotApply:
    """Module A is built for operating companies. Banks are not one.

    JPMorgan's probe run returned a 14.7-month cash runway, net debt of
    -$237bn, and TTM operating cash flow of -$253bn. Every one of those is a
    correctly-read number that means nothing, and two of them look like
    findings. A gate that cannot ask the right question must say so rather
    than answer the wrong one.
    """

    @staticmethod
    def bank():
        return build_company(profile=stable_profile(is_bank=True))

    def test_a1_does_not_test_a_bank_for_solvency_by_current_ratio(self):
        result = a_health.gate_a1_solvency(self.bank(), Config())
        assert result.outcome is Outcome.NOT_APPLICABLE
        assert "capital adequacy" in result.reason

    def test_a2_does_not_call_deposits_leverage(self):
        result, _ = a_health.gate_a2_leverage(
            self.bank(), MarketData(), Config(), None
        )
        assert result.outcome is Outcome.NOT_APPLICABLE

    def test_a3_does_not_compute_a_cash_runway_for_a_bank(self):
        result = a_health.gate_a3_earnings_quality(self.bank(), Config())
        assert result.outcome is Outcome.NOT_APPLICABLE

    def test_not_applicable_is_not_a_data_gap(self):
        """NOT_COMPUTABLE says 'find better data'. NOT_APPLICABLE says 'the
        question is wrong'. Conflating them sends someone hunting for a tag
        that will never exist."""
        result = a_health.gate_a1_solvency(self.bank(), Config())
        assert result.computable
        assert not result.applicable

    def test_a_bank_is_not_blocked_merely_for_being_a_bank(self):
        ledger = AuditLedger("JPM", date(2026, 1, 1))
        assessment = a_health.run_module_a(
            self.bank(),
            MarketData(
                group_net_debt_ebitda_median={"Machinery": 2.0},
                group_member_counts={"Machinery": 22},
            ),
            Config(),
            ledger,
            as_of=date(2026, 1, 1),
        )
        assert "A1" in assessment.inapplicable_gates
        assert not assessment.screened_fully

    def test_the_thinner_screening_is_stated_on_the_record(self):
        """A pass on four gates is weaker than a pass on six, and nothing
        downstream can tell unless it is said."""
        ledger = AuditLedger("JPM", date(2026, 1, 1))
        a_health.run_module_a(
            self.bank(), MarketData(), Config(), ledger, as_of=date(2026, 1, 1)
        )
        assert any("do not apply" in n for n in ledger.notes)

    def test_an_operating_company_still_runs_every_gate(self):
        ledger = AuditLedger("CAT", date(2026, 1, 1))
        assessment = a_health.run_module_a(
            build_company(),
            MarketData(
                group_net_debt_ebitda_median={"Machinery": 2.0},
                group_member_counts={"Machinery": 22},
            ),
            Config(),
            ledger,
            as_of=date(2026, 1, 1),
        )
        assert assessment.screened_fully


class TestC1WindowMeasurement:
    """A filer with complete history must not read as short on history.

    Stop condition 2 tripped on eight of nine targets across every run, and
    two separate off-by-a-quarter errors were doing it — neither of them in
    the data.
    """

    END = date(2026, 9, 1)

    def series(self, years_of_filings, drop=None):
        from gcfp.data.reconstruct import reconstruct_series
        from gcfp.types import PeriodFinancials, PricePoint

        quarters = []
        for i in range(int(years_of_filings * 4)):
            if i == drop:
                continue
            end = self.END - timedelta(days=91 * i)
            quarters.append(
                PeriodFinancials(
                    period_end=end, filing_date=end + timedelta(days=40),
                    fiscal_year=end.year, net_income=1e9, shares_diluted=1e9,
                )
            )
        prices = [
            PricePoint(price_date=self.END - timedelta(days=d), close=100.0)
            for d in range(0, int(365.25 * 11))
        ]
        obs = reconstruct_series(
            quarters, prices, "trailing_pe", end=self.END, years=7
        )
        if len(obs) < 2:
            return 0.0
        return (obs[0].observation_date - obs[-1].observation_date).days / 365.25

    @property
    def bar(self):
        return 7 - c_anchors._WINDOW_TOLERANCE_YEARS

    def test_complete_history_clears_the_bar(self):
        """A quarter is lost at each end independently — the first filing
        after the cutoff, and the last before today. A one-quarter tolerance
        set the bar at the theoretical best case, which nothing reaches."""
        assert self.series(9) >= self.bar

    def test_a_single_missing_quarter_does_not_truncate_the_series(self):
        """With only a year of warm-up the oldest observation consumed the
        last four quarters fetched, so one gap anywhere shortened the span."""
        assert self.series(9, drop=30) >= self.bar

    def test_a_genuinely_short_listing_is_still_reported(self):
        """The tolerance must not be so wide that it stops discriminating."""
        assert self.series(5) < self.bar


class TestReitIssuanceIsNotDilution:
    """Issuing equity is how a REIT funds acquisitions.

    Realty Income grew its share count 14.8% a year and is not diluting
    anyone. A4's raw 15%/yr threshold fires on nearly every REIT, which makes
    it noise on that path rather than a signal. The question that matters is
    whether the issuance was accretive.
    """

    @staticmethod
    def company(share_growth, revenue_growth, *, is_reit):
        """Shares and revenue each compounding at their own annual rate."""
        profile = stable_profile(is_reit=is_reit)
        shares = [500e6 * (1 + share_growth) ** -i for i in range(3)]
        revenue = [10e9 * (1 + revenue_growth) ** -i for i in range(3)]
        return build_company(
            profile=profile,
            annual_kw={"count": 3},
            quarterly_kw={"count": 12, "shares": 500e6, "share_growth": share_growth},
            annual_overrides=[
                {"shares_diluted": s, "shares_outstanding": s, "revenue": r}
                for s, r in zip(shares, revenue)
            ],
        )

    def reit(self, share_growth, revenue_growth):
        return self.company(share_growth, revenue_growth, is_reit=True)

    def test_accretive_issuance_passes(self):
        """Shares up 25%/yr, revenue up 35%/yr — every holder owns less of a
        business that grew more than their stake shrank."""
        result = a_health.gate_a4_red_flags(
            self.reit(0.25, 0.35), Config(), date(2026, 1, 1)
        )
        assert result.outcome is not Outcome.FAIL, result.reason

    def test_dilutive_issuance_still_fails(self):
        """Shares up 25%/yr, revenue up 5% — the tolerance must not be a
        blanket exemption for REITs."""
        result = a_health.gate_a4_red_flags(
            self.reit(0.25, 0.05), Config(), date(2026, 1, 1)
        )
        assert result.outcome is Outcome.FAIL
        assert "not accretive" in result.reason

    def test_an_operating_company_is_judged_on_the_raw_threshold(self):
        """The accretion test is for businesses funded by issuance. An
        industrial issuing 25% more shares a year is diluting."""
        data = self.company(0.25, 0.35, is_reit=False)
        result = a_health.gate_a4_red_flags(data, Config(), date(2026, 1, 1))
        assert result.outcome is Outcome.FAIL
        assert "share count +" in result.reason


class TestA2NetCashAndSplitDepreciation:
    """A2 blocked 81 of 134 names in the first real screen — 60% of everything
    the universe offered, and the top two causes were both fixable here.
    """

    def test_net_cash_clears_without_an_ebitda(self):
        """Seventeen names were blocked for negative EBITDA, several of them
        sitting on net cash. A company holding more cash than debt cannot be
        over-levered, and the ratio it would be judged on does not exist."""
        data = build_company(
            quarterly_kw={"total_debt": 1e9, "cash_and_equivalents": 5e9,
                          "ebitda": None},
            annual_kw={"total_debt": 1e9, "cash_and_equivalents": 5e9},
        )
        result, _ = a_health.gate_a2_leverage(
            data, MarketData(), Config(), None
        )
        assert result.outcome is Outcome.PASS
        assert result.branch == "net_cash"

    def test_net_debt_with_no_ebitda_still_refuses(self):
        """The net-cash branch must not become a way around the gate for a
        company that genuinely carries debt."""
        data = build_company(
            quarterly_kw={"total_debt": 5e9, "cash_and_equivalents": 1e9,
                          "ebitda": None},
            annual_kw={"total_debt": 5e9, "cash_and_equivalents": 1e9},
        )
        result, _ = a_health.gate_a2_leverage(
            data, MarketData(), Config(), None
        )
        assert result.outcome is Outcome.NOT_COMPUTABLE

    def test_depreciation_and_amortisation_are_summed_when_split(self):
        """Many filers never tag a combined figure. A first-match chain then
        finds neither line, and 34 names could not produce EBITDA at all."""
        from unittest.mock import Mock

        from gcfp.data.edgar import EdgarAdapter

        def dur(val, end, start, filed, tag):
            return tag, {"val": val, "end": end, "start": start,
                         "filed": filed, "form": "10-Q"}

        facts = {}
        for tag, entry in [
            dur(1000, "2025-03-31", "2025-01-01", "2025-04-30", "Revenues"),
            dur(100, "2025-03-31", "2025-01-01", "2025-04-30", "OperatingIncomeLoss"),
            dur(30, "2025-03-31", "2025-01-01", "2025-04-30", "Depreciation"),
            dur(12, "2025-03-31", "2025-01-01", "2025-04-30",
                "AmortizationOfIntangibleAssets"),
        ]:
            facts.setdefault(tag, {"units": {"USD": []}})["units"]["USD"].append(entry)

        adapter = EdgarAdapter(user_agent="t t@example.com", session=Mock())
        adapter._facts = lambda symbol: {
            "cik": 1, "entityName": "X", "facts": {"us-gaap": facts}
        }
        period = adapter._periods("X", annual=False, limit=4)[0]
        assert period.ebitda == 142, "100 operating income + 30 depreciation + 12 amortisation"

    @staticmethod
    def adapter_with(entries):
        from unittest.mock import Mock

        from gcfp.data.edgar import EdgarAdapter

        facts = {}
        for tag, val, end, start, filed in entries:
            facts.setdefault(tag, {"units": {"USD": []}})["units"]["USD"].append(
                {"val": val, "end": end, "start": start, "filed": filed, "form": "10-Q"}
            )
        adapter = EdgarAdapter(user_agent="t t@example.com", session=Mock())
        adapter._facts = lambda symbol: {
            "cik": 1, "entityName": "X", "facts": {"us-gaap": facts}
        }
        return adapter

    def test_split_depreciation_is_differenced_out_of_year_to_date(self):
        """The cash flow statement reports depreciation year to date. Read as
        a quarter, the six-month figure doubled Q2's EBITDA add-back."""
        adapter = self.adapter_with([
            ("Revenues", 1000, "2025-03-31", "2025-01-01", "2025-04-30"),
            ("Revenues", 1000, "2025-06-30", "2025-04-01", "2025-07-30"),
            ("OperatingIncomeLoss", 100, "2025-06-30", "2025-04-01", "2025-07-30"),
            ("Depreciation", 30, "2025-03-31", "2025-01-01", "2025-04-30"),
            ("Depreciation", 61, "2025-06-30", "2025-01-01", "2025-07-30"),
        ])
        q2 = adapter._periods("X", annual=False, limit=4)[0]
        assert q2.period_end == date(2025, 6, 30)
        assert q2.ebitda == 131, "100 operating income + (61 - 30) depreciation"

    def test_no_operating_income_line_falls_back_to_pretax_plus_interest(self):
        """ADM, Emerson and Dillard's present costs and expenses with no
        operating-income subtotal; A2 read them as not computable."""
        adapter = self.adapter_with([
            ("Revenues", 1000, "2025-03-31", "2025-01-01", "2025-04-30"),
            ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
             80, "2025-03-31", "2025-01-01", "2025-04-30"),
            ("InterestExpense", 15, "2025-03-31", "2025-01-01", "2025-04-30"),
            ("DepreciationDepletionAndAmortization", 25, "2025-03-31", "2025-01-01", "2025-04-30"),
        ])
        period = adapter._periods("X", annual=False, limit=4)[0]
        assert period.ebitda == 120, "80 pre-tax + 15 interest + 25 D&A"
        assert period.ebitda_basis == "pretax_plus_interest"
        assert period.ebit == 95, "and EBIT, for D4's ROIC, is 80 + 15"

    def test_roic_uses_ebit_when_there_is_no_operating_income_line(self):
        from dataclasses import replace

        from gcfp.modules.d_conviction import _roic

        data = build_company()
        latest = replace(data.annual[0], operating_income=None, ebit=6.5e9)
        data_without = replace(data, annual=[latest, *data.annual[1:]])
        assert _roic(data_without) == pytest.approx(_roic(data))

    def test_goods_and_services_costs_are_summed_when_split(self):
        """GE reports the two on separate lines; taking the goods line alone
        overstated gross profit by the whole cost of services."""
        adapter = self.adapter_with([
            ("Revenues", 1000, "2025-03-31", "2025-01-01", "2025-04-30"),
            ("CostOfGoodsSold", 500, "2025-03-31", "2025-01-01", "2025-04-30"),
            ("CostOfServices", 300, "2025-03-31", "2025-01-01", "2025-04-30"),
        ])
        period = adapter._periods("X", annual=False, limit=4)[0]
        assert period.gross_profit == 200

    def test_a_net_interest_figure_is_not_added_back(self):
        """Net interest mixes income with expense and its sign varies by
        filer; adding it back could inflate or deflate EBIT either way."""
        adapter = self.adapter_with([
            ("Revenues", 1000, "2025-03-31", "2025-01-01", "2025-04-30"),
            ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
             80, "2025-03-31", "2025-01-01", "2025-04-30"),
            ("InterestIncomeExpenseNet", -15, "2025-03-31", "2025-01-01", "2025-04-30"),
            ("DepreciationDepletionAndAmortization", 25, "2025-03-31", "2025-01-01", "2025-04-30"),
        ])
        period = adapter._periods("X", annual=False, limit=4)[0]
        assert period.ebitda is None


class TestRunawayGrowthAndImplausibleDiscount:
    """Consolidated Water passed the screen at a $422.74 fair value against a
    $27 share price, and conviction scored the resulting 93.4% discount a
    perfect 30/30. Two separate defects, and the second amplified the first.
    """

    def test_stage_one_growth_is_capped_at_the_classification_ceiling(self):
        """A6 routes to CORE-STABLE only below 20% growth. Projecting more
        than that contradicts the classification that chose the method."""
        from gcfp.modules.b_valuation import _discounted_dcf, _linear_fade

        cap = Config().valuation.b1_stage_one_growth_cap

        def value(growth):
            path = [growth] * 5 + _linear_fade(growth, 0.025, 5)
            return _discounted_dcf(25e6, path, 0.025, 0.085)[0]

        runaway = value(0.45)
        capped = value(min(0.45, cap))
        assert capped < runaway / 3, (
            "a 45% trailing CAGR projected forward still dominates the value"
        )

    def test_a_fast_past_is_flagged_rather_than_silently_trimmed(self):
        """The operator needs to know the cap bound — otherwise the fair value
        looks like a measurement rather than a ceiling."""
        data = build_company(
            annual_kw={"revenue_growth": 0.45},
            quarterly_kw={"revenue_growth": 0.45},
        )
        result = b_valuation.value_b1_core_stable(
            data, MarketData(risk_free_rate=0.04), Config()
        )
        assert any("GROWTH CAPPED" in f for f in result.flags), result.flags

    def test_an_implausible_discount_is_named_on_the_score(self):
        """D2 saturates: 90% and 60% discounts both score 30/30, so the
        larger the valuation error the more confident the system becomes."""
        score = d_conviction.score_valuation_excess(0.934, 0.35, Config())
        assert score.points == 30.0
        assert "IMPLAUSIBLE" in score.basis
        assert score.detail["implausible"] is True

    def test_an_ordinary_discount_is_not_flagged(self):
        score = d_conviction.score_valuation_excess(0.45, 0.35, Config())
        assert "IMPLAUSIBLE" not in score.basis
        assert score.detail["implausible"] is False


class TestShareCountFallsBackToSharesOutstanding:
    """About one company-date in five never tags a weighted-average share
    count in its 10-Qs, so A4 and D4 read "share count history unavailable"
    for a company whose shares outstanding are on every cover page."""

    @staticmethod
    def company(growth):
        quarters = [
            {"shares_diluted": None, "shares_outstanding": 500e6 * (1 + growth) ** (-i / 4)}
            for i in range(12)
        ]
        return build_company(quarterly_overrides=quarters)

    def test_dilution_is_measured_on_shares_outstanding(self):
        assert a_health.share_count_cagr(self.company(0.25)) == pytest.approx(0.25)

    def test_a_diluting_company_still_fails_a4(self):
        result = a_health.gate_a4_red_flags(self.company(0.25), Config(), date(2026, 1, 1))
        assert result.outcome is Outcome.FAIL
        assert "share count +" in result.reason

    def test_with_neither_series_it_stays_not_computable(self):
        quarters = [{"shares_diluted": None, "shares_outstanding": None}] * 12
        assert a_health.share_count_cagr(build_company(quarterly_overrides=quarters)) is None
