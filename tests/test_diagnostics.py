"""Instrumentation: rejection taxonomy, peer availability, independence,
boundaries, costs, sensitivity, and 8-K corporate actions.

The thing these tests protect is the ability to tell a disciplined screen from
a broken one. A diagnostic that mis-attributes a cause is worse than none,
because it sends you to fix the wrong thing with confidence.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from unittest.mock import Mock

import pytest

from gcfp.boundaries import NEAR_MARGIN, RoundTripCost, near_boundaries
from gcfp.classification import Classification
from gcfp.diagnostics import (
    ConvictionIndependence,
    PeerDiagnostic,
    RejectionLedger,
    classify_peer_reason,
)
from gcfp.modules.c_anchors import PeerCandidate, PeerDecision
from tests.conftest import build_company


class TestRejectionTaxonomy:
    def test_a5_rejections_name_the_missing_field(self, market, config):
        """"A5 failed" is useless across 4,000 names; "missing: revenue" is a
        two-line fix worth thousands of candidates."""
        from gcfp.modules.f_sizing import PortfolioState
        from gcfp.modules.k_currency import FxTable
        from gcfp.pipeline import CandidateInputs, evaluate_candidate

        broken = dataclasses.replace(build_company(), current_price=None)
        evaluation = evaluate_candidate(
            broken, market, config, CandidateInputs(),
            PortfolioState(total_value=1e6), FxTable({}, date.today()),
        )
        ledger = RejectionLedger()
        rejection = ledger.record(evaluation)
        assert rejection.stage == "A5"
        assert "current_price" in rejection.cause

    def test_the_dominant_cause_is_identified(self):
        ledger = RejectionLedger()
        for i in range(30):
            ledger.rejections.append(
                __import__("gcfp.diagnostics", fromlist=["Rejection"]).Rejection(
                    f"S{i}", "A5", "missing: revenue"
                )
            )
        for i in range(3):
            ledger.rejections.append(
                __import__("gcfp.diagnostics", fromlist=["Rejection"]).Rejection(
                    f"T{i}", "E", "discount not met"
                )
            )
        stage, cause, n = ledger.dominant_cause
        assert (stage, cause, n) == ("A5", "missing: revenue", 30)

    def test_a_data_dominated_screen_warns_it_is_measuring_the_parser(self):
        from gcfp.diagnostics import Rejection

        ledger = RejectionLedger()
        ledger.passed.append("GOOD")
        for i in range(50):
            ledger.rejections.append(Rejection(f"S{i}", "A5", "missing: revenue"))
        text = "\n".join(ledger.as_report_lines())
        assert "measuring the parser" in text

    def test_a_price_dominated_screen_says_the_system_is_working(self):
        from gcfp.diagnostics import Rejection

        ledger = RejectionLedger()
        for i in range(20):
            ledger.rejections.append(Rejection(f"S{i}", "E", "discount not met"))
        text = "\n".join(ledger.as_report_lines())
        assert "Prime Directive 6" in text
        assert "not a fault" in text

    def test_unreachable_names_are_recorded_separately(self):
        ledger = RejectionLedger()
        ledger.record_unreachable("SIVBQ", "not in the SEC ticker index")
        assert ledger.rejections[0].stage == "UNREACHABLE"


class TestPeerDiagnostic:
    def _decision(self, symbol, included, reason):
        return PeerDecision(PeerCandidate(symbol, 15.0, 1e10, 0.05, "G"), included, reason)

    def test_reasons_map_to_the_screen_that_rejected_them(self):
        assert classify_peer_reason("market cap 5.20x subject, outside 0.3-3.0x") == "size band"
        assert classify_peer_reason("revenue growth 22.0pp from subject, outside 10pp band") == "growth band"
        assert classify_peer_reason("different grouping (X vs Y)") == "grouping mismatch"
        assert classify_peer_reason("multiple not computable or non-positive") == "multiple not computable"

    def test_the_binding_constraint_is_identified(self, config):
        diagnostic = PeerDiagnostic()
        for _ in range(10):
            diagnostic.record(
                [
                    self._decision("A", False, "revenue growth 22.0pp from subject, outside 10pp band"),
                    self._decision("B", False, "revenue growth 30.0pp from subject, outside 10pp band"),
                    self._decision("C", False, "market cap 5.20x subject, outside 0.3-3.0x"),
                    self._decision("D", True, "meets grouping, size, and growth screen"),
                ],
                config,
            )
        cause, n = diagnostic.binding_constraint
        assert cause == "growth band"
        assert n == 20

    def test_a_low_formation_rate_flags_a_design_level_finding(self, config):
        diagnostic = PeerDiagnostic()
        for _ in range(10):
            diagnostic.record(
                [self._decision("A", False, "revenue growth 30.0pp from subject, outside 10pp band")],
                config,
            )
        text = "\n".join(diagnostic.as_report_lines(config))
        assert "BINDING CONSTRAINT" in text
        assert "design-level finding" in text

    def test_a_healthy_formation_rate_does_not_cry_wolf(self, config):
        diagnostic = PeerDiagnostic()
        for _ in range(10):
            diagnostic.record(
                [self._decision(f"P{i}", True, "meets screen") for i in range(6)],
                config,
            )
        assert diagnostic.formation_rate == 1.0
        assert "BINDING CONSTRAINT" not in "\n".join(diagnostic.as_report_lines(config))


class TestConvictionIndependence:
    def _record(self, check, values):
        from gcfp.modules.d_conviction import ComponentScore, ConvictionScore

        evaluation = Mock()
        evaluation.conviction = ConvictionScore(
            "X", sum(values.values()), 
            [ComponentScore(k, v, 30.0, "") for k, v in values.items()],
            "standard",
        )
        check.record(evaluation)

    def test_a_small_sample_gives_no_redundancy_verdict(self):
        """Three points can produce r = -1.00 by coincidence. Reporting that
        as a finding is overfitting in a different costume."""
        check = ConvictionIndependence()
        for i in range(3):
            self._record(check, {"a": float(i), "b": float(-i)})
        assert check.correlations()
        assert check.redundant_pairs == []
        assert "noise" in "\n".join(check.as_report_lines())

    def test_perfectly_correlated_components_are_flagged_once_there_is_data(self):
        check = ConvictionIndependence()
        for i in range(25):
            self._record(check, {"a": float(i), "b": float(i) * 2.0})
        assert check.has_enough_samples
        redundant = check.redundant_pairs
        assert redundant and abs(redundant[0][2]) > 0.99
        assert "REDUNDANT" in "\n".join(check.as_report_lines())

    def test_independent_components_are_not_flagged(self):
        check = ConvictionIndependence()
        values = [3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5, 8, 9, 7, 9, 3, 2, 3, 8, 4, 6, 2, 6, 4, 3]
        other = [8, 2, 7, 1, 8, 2, 8, 1, 8, 2, 8, 4, 5, 9, 0, 4, 5, 2, 3, 5, 3, 6, 0, 2, 8]
        for a, b in zip(values, other):
            self._record(check, {"a": float(a), "b": float(b)})
        assert check.redundant_pairs == []


class TestBoundaries:
    def test_a_name_just_under_the_growth_line_is_flagged(self, config):
        near = build_company(annual_kw=dict(revenue_growth=0.19))
        found = near_boundaries(near, config, Classification.CORE_STABLE)
        assert any(p.criterion == "revenue growth YoY" for p in found)

    def test_a_name_comfortably_clear_is_not_flagged(self, config):
        clear = build_company(annual_kw=dict(revenue_growth=0.05))
        growth_flags = [
            p for p in near_boundaries(clear, config, Classification.CORE_STABLE)
            if p.criterion == "revenue growth YoY"
        ]
        assert not growth_flags

    def test_the_flag_states_what_would_change(self, config):
        near = build_company(annual_kw=dict(revenue_growth=0.19))
        found = near_boundaries(near, config, Classification.CORE_STABLE)
        line = next(
            p.as_report_line() for p in found if p.criterion == "revenue growth YoY"
        )
        assert "CORE-GROWTH" in line
        assert "25%" in line and "35%" in line, "the gate change must be visible"

    def test_the_margin_is_relative_to_the_threshold(self, config):
        from gcfp.boundaries import BoundaryProximity

        just_inside = BoundaryProximity(
            "growth", 0.19, 0.20, False, Classification.CORE_GROWTH, ""
        )
        far = BoundaryProximity(
            "growth", 0.05, 0.20, False, Classification.CORE_GROWTH, ""
        )
        assert just_inside.is_near
        assert not far.is_near


class TestRoundTripCost:
    def test_a_discount_inside_the_cost_of_trading_is_flagged(self):
        line = RoundTripCost(0.251).as_report_line(0.25)
        assert "NOT net" in line

    def test_a_comfortable_discount_is_not_flagged(self):
        assert "NOT net" not in RoundTripCost(0.35).as_report_line(0.25)

    def test_the_net_discount_subtracts_both_legs(self):
        cost = RoundTripCost(0.30, one_way_cost=0.002)
        assert cost.round_trip == pytest.approx(0.004)
        assert cost.net_discount == pytest.approx(0.296)


class TestSensitivity:
    def test_a_verdict_that_crosses_the_price_is_flagged(self, market, config):
        from gcfp.modules import b_valuation
        from gcfp.sensitivity import analyse

        data = build_company()
        base = b_valuation.value_b1_core_stable(data, market, config)
        # Priced just under fair value: any adverse assumption flips it.
        report = analyse(
            data, market, config, Classification.CORE_STABLE, base,
            price=base.fair_value_per_share * 0.97,
        )
        assert report.verdict_survives is False
        assert "DOES NOT SURVIVE" in "\n".join(report.as_report_lines())

    def test_a_robust_verdict_survives(self, market, config):
        from gcfp.modules import b_valuation
        from gcfp.sensitivity import analyse

        data = build_company()
        base = b_valuation.value_b1_core_stable(data, market, config)
        report = analyse(
            data, market, config, Classification.CORE_STABLE, base,
            price=base.fair_value_per_share * 0.40,
        )
        assert report.verdict_survives is True

    def test_the_discount_rate_perturbation_actually_moves_the_value(
        self, market, config
    ):
        from gcfp.modules import b_valuation
        from gcfp.sensitivity import analyse

        data = build_company()
        base = b_valuation.value_b1_core_stable(data, market, config)
        report = analyse(data, market, config, Classification.CORE_STABLE, base)
        shifts = [
            p for p in report.perturbations if p.label.startswith("discount rate")
        ]
        assert len(shifts) == 2
        assert any(p.change and abs(p.change) > 0.01 for p in shifts)


class TestEdgarCorporateActions:
    def _adapter(self, items, forms, dates):
        from gcfp.data.edgar import EdgarAdapter

        adapter = EdgarAdapter(user_agent="t t@example.com", session=Mock())
        adapter._ticker_map = {"TESTCO": 1}
        adapter._submissions_cache[1] = {
            "name": "TESTCO", "sic": "3531", "sicDescription": "Machinery",
            "exchanges": ["NYSE"],
            "filings": {"recent": {"form": forms, "filingDate": dates, "items": items}},
        }
        return adapter

    def test_item_2_01_is_detected(self):
        adapter = self._adapter(
            ["2.01,9.01"], ["8-K"], ["2023-11-02"]
        )
        actions = adapter.get_corporate_actions("TESTCO", 7)
        assert len(actions) == 1
        assert actions[0].effective_date == date(2023, 11, 2)

    def test_other_8k_items_are_ignored(self):
        """Item 2.02 is earnings, 5.02 is officer changes — neither changes
        what the company *is*."""
        adapter = self._adapter(
            ["2.02,9.01", "5.02"], ["8-K", "8-K"], ["2023-11-02", "2023-12-01"]
        )
        assert adapter.get_corporate_actions("TESTCO", 7) == ()

    def test_non_8k_forms_are_ignored(self):
        adapter = self._adapter(["", ""], ["10-K", "10-Q"], ["2023-11-02", "2024-02-01"])
        assert adapter.get_corporate_actions("TESTCO", 7) == ()

    def test_as_of_hides_filings_that_had_not_happened(self):
        adapter = self._adapter(
            ["2.01", "2.01"], ["8-K", "8-K"], ["2021-08-09", "2023-11-02"]
        )
        assert len(adapter.get_corporate_actions("TESTCO", 7)) == 2
        adapter.as_of = date(2023, 1, 1)
        assert len(adapter.get_corporate_actions("TESTCO", 7)) == 1

    def test_the_capability_is_declared_with_its_limits(self):
        adapter = self._adapter([], [], [])
        capability = {c.name: c for c in adapter.capabilities()}["corporate_actions"]
        assert capability.supported
        assert "over-reports" in capability.detail.lower()


class TestXbrlCoverage:
    def test_the_revenue_chain_covers_legacy_and_industry_tags(self):
        from gcfp.data.xbrl import TAG_CHAINS

        chain = TAG_CHAINS["revenue"]
        assert "RevenueFromContractWithCustomerExcludingAssessedTax" in chain
        assert "SalesRevenueNet" in chain, "older filings use this"
        assert "InterestAndDividendIncomeOperating" in chain, "banks use this"
        assert len(chain) >= 10

    def test_cost_of_revenue_is_available_for_deriving_gross_profit(self):
        from gcfp.data.xbrl import DURATION_FIELDS, TAG_CHAINS

        assert "cost_of_revenue" in TAG_CHAINS
        assert "cost_of_revenue" in DURATION_FIELDS


class TestUniversePeerAvailability:
    """Stop condition 4 asks whether the dual-anchor premise holds for a
    universe. It was answering from the §18 roster — nine companies chosen
    for being extreme, none of which has a size-matched peer for reasons that
    are about those companies rather than about the market.
    """

    @staticmethod
    def member(symbol, cap, industry):
        from gcfp.universe import UniverseMember

        return UniverseMember(
            symbol=symbol, name=symbol, market_cap=cap, adv_3m_usd=50e6,
            sector="Sector", industry=industry, sub_industry=None,
            net_debt_to_ebitda=1.0, gross_margin_stdev=0.02,
            revenue_growth=0.05, beta=1.0,
        )

    def universe(self, members):
        from gcfp.universe import Universe

        u = Universe(as_of=date(2026, 1, 1), source="test")
        u.members.extend(members)
        return u

    def measure(self, members):
        from gcfp.config import Config
        from gcfp.diagnostics import measure_universe_peer_availability

        return measure_universe_peer_availability(self.universe(members), Config())

    def test_a_crowded_industry_has_peers_for_everyone(self):
        members = [self.member(f"M{i}", 20e9 + i * 1e9, "Machinery") for i in range(6)]
        result = self.measure(members)
        assert result.rate == 0.0
        assert result.would_be_single_anchor == 0

    def test_a_company_with_nobody_its_size_has_none(self):
        """NVDA is worth several trillion. C2 wants peers between 0.3x and 3x
        of that, and there are not four such companies on earth."""
        members = [
            self.member("GIANT", 5_000e9, "Semis"),
            *[self.member(f"S{i}", 2e9 + i * 1e8, "Semis") for i in range(8)],
        ]
        result = self.measure(members)
        giant_only = [c for m, c in zip(members, result.counts) if m.symbol == "GIANT"]
        assert giant_only == [0]

    def test_a_lone_filer_in_its_industry_is_counted_separately(self):
        """No same-industry name at all is a sampling result, not a structural
        one, and conflating the two makes the directive unearned."""
        members = [
            self.member("ALONE", 10e9, "Rare"),
            *[self.member(f"M{i}", 20e9, "Machinery") for i in range(6)],
        ]
        assert self.measure(members).no_industry_peers == 1

    def test_the_rate_is_over_the_whole_sample_not_the_extremes(self):
        members = [
            *[self.member(f"M{i}", 20e9 + i * 1e9, "Machinery") for i in range(8)],
            self.member("GIANT", 5_000e9, "Semis"),
            self.member("TINY", 1e8, "Semis"),
        ]
        result = self.measure(members)
        assert result.assessed == 10
        assert result.would_be_single_anchor == 2, "only the two extremes"
        assert result.rate == pytest.approx(0.2)

    def test_it_is_reported_as_an_upper_bound(self):
        """Grouping and size are computable from a universe row; the growth
        band and the multiple test are not. Claiming otherwise would overstate
        how many peer sets will really form."""
        lines = " ".join(
            self.measure([self.member("M", 10e9, "Machinery")]).report_lines()
        )
        assert "upper bound" in lines


class TestSampleLimitedDetection:
    """The same market reads 96% single-anchor at eight names per industry and
    0% at forty-five. Without a way to tell those apart, stop condition 4
    condemns the dual-anchor premise on the sample size.
    """

    @staticmethod
    def market(per_industry, seed=7):
        """One market — identical size distribution — sampled at two depths."""
        import random

        from gcfp.config import Config
        from gcfp.diagnostics import measure_universe_peer_availability
        from gcfp.universe import Universe, UniverseMember

        rng = random.Random(seed)
        u = Universe(as_of=date(2026, 1, 1), source="test")
        for industry in range(9):
            for i in range(per_industry):
                u.members.append(
                    UniverseMember(
                        symbol=f"I{industry}N{i}", name="x",
                        market_cap=10 ** rng.uniform(8.5, 12.5),
                        adv_3m_usd=50e6, sector="S", industry=f"Ind{industry}",
                        sub_industry=None, net_debt_to_ebitda=1.0,
                        gross_margin_stdev=0.02, revenue_growth=0.05, beta=1.0,
                    )
                )
        result = measure_universe_peer_availability(u, Config())
        result.requested = per_industry * 9
        return result

    def test_a_thin_sample_is_flagged(self):
        assert self.market(8).sample_limited

    def test_a_deep_sample_of_the_same_market_is_not(self):
        assert not self.market(45).sample_limited

    def test_the_headline_rate_swings_on_depth_alone(self):
        """This is the whole reason the flag has to exist."""
        thin, deep = self.market(8), self.market(45)
        assert thin.rate > 0.9 and deep.rate < 0.1

    def test_the_required_industry_size_barely_moves(self):
        """Unlike the rate, this is sample-independent — it measures the
        market's size dispersion, which is what the design question is about."""
        thin, deep = self.market(8), self.market(45)
        assert abs(thin.industry_size_needed - deep.industry_size_needed) < 12

    def test_a_thin_sample_suggests_a_bigger_one(self):
        suggested = self.market(8).suggested_peer_sample
        assert suggested and suggested > 8 * 9

    def test_a_deep_sample_suggests_nothing(self):
        assert self.market(45).suggested_peer_sample is None
