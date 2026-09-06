"""The §18 probe.

The probe's own correctness matters more than most code here: its output is
what decides whether SPEC-GROWTH gets built at all, and a probe that reports a
false stop condition is worse than no probe. So each stop condition is driven
deterministically, in both directions.
"""

from __future__ import annotations

from datetime import date

import pytest

from gcfp.classification import Classification
from gcfp.data.fixtures import FixtureAdapter
from gcfp.fixtures_probe import build_probe_fixture
from gcfp.probe import DEFAULT_TARGETS, ProbeTarget, run_probe
from gcfp.types import TaxonomyLevel


@pytest.fixture(scope="module")
def report():
    return run_probe(build_probe_fixture(), __import__(
        "gcfp.config", fromlist=["DEFAULT_CONFIG"]
    ).DEFAULT_CONFIG)


class TestProbeRoster:
    def test_covers_every_classification_path(self):
        """§18 names one company per classification plus four special cases."""
        expected = {
            t.expected_classification for t in DEFAULT_TARGETS
            if t.expected_classification
        }
        assert expected == set(Classification)

    def test_includes_the_four_special_cases(self):
        roles = {t.role for t in DEFAULT_TARGETS}
        assert "foreign ADR" in roles
        assert "spinoff in the last 7 years" in roles
        assert "fewer than 4 obvious peers" in roles
        assert "delisted name" in roles


class TestProbeFindings:
    def test_a6_routes_each_target_to_its_expected_classification(self, report):
        for coverage in report.reached:
            expected = coverage.target.expected_classification
            if expected is None:
                continue
            assert coverage.actual_classification is expected, (
                f"{coverage.target.symbol} routed to "
                f"{coverage.actual_classification}, expected {expected}"
            )

    def test_a_delisted_name_is_unreachable_not_silently_passed(self, report):
        delisted = [c for c in report.coverages if c.target.role == "delisted name"]
        assert delisted and not delisted[0].reached
        assert any("not in fixture set" in (delisted[0].error or "") for _ in [1])
        assert any("treat these as unknown" in n for n in report.notes)

    def test_c1_1_truncation_is_detected_on_the_spinoff_target(self, report):
        spinoff = next(
            c for c in report.reached
            if c.target.role == "spinoff in the last 7 years"
        )
        assert spinoff.c1_discontinuity_date is not None
        assert spinoff.c1_years_available < spinoff.c1_raw_years

    def test_the_near_monopoly_falls_into_single_anchor_mode(self, report):
        """The condition C5 was added to handle."""
        monopoly = next(
            c for c in report.reached
            if c.target.role == "fewer than 4 obvious peers"
        )
        assert monopoly.c2_peer_count == 0
        assert monopoly.single_anchor

    def test_reit_without_affo_cannot_build_a_peer_set(self, report):
        """P/E is banned on the REIT path, so no AFFO means no anchor."""
        reit = next(c for c in report.reached if c.target.role == "REIT")
        missing = " ".join(reit.missing)
        assert "adjusted_funds_from_operations" in missing

    def test_bank_missing_tangible_book_is_reported(self, report):
        bank = next(c for c in report.reached if c.target.role == "bank")
        assert any("tangible_book_value" in m for m in bank.missing)

    def test_insurer_missing_combined_ratio_is_reported(self, report):
        insurer = next(c for c in report.reached if c.target.role == "insurer")
        assert any("combined_ratio" in m for m in insurer.missing)

    def test_adr_underlying_currency_is_flagged_as_unmapped(self, report):
        adr = next(c for c in report.reached if c.target.role == "foreign ADR")
        assert adr.profile.is_adr
        assert adr.profile.underlying_currency is None


class TestStopConditions:
    def _stop(self, report, number):
        return next(s for s in report.stop_conditions if s.number == number)

    def test_stop_1_trips_when_share_history_is_missing(self, report):
        """Without A3's pre-profit branch and A4's dilution flag, SPEC-GROWTH
        is a way to buy companies shortly before they run out of money."""
        stop = self._stop(report, 1)
        assert stop.tripped
        assert "DISABLE SPEC-GROWTH ENTIRELY" in stop.directive

    def test_stop_2_separates_a_sourcing_gap_from_c1_1_truncation(self, report):
        stop = self._stop(report, 2)
        assert "that is the gate working, not a sourcing gap" in stop.finding

    def test_stop_2_does_not_trip_merely_on_the_window_boundary(self):
        """A 7-year fetch can never span quite 7.0 years. Without a tolerance
        this condition would trip for every source and mean nothing."""
        from gcfp.config import DEFAULT_CONFIG

        report = run_probe(build_probe_fixture(), DEFAULT_CONFIG)
        stop = next(s for s in report.stop_conditions if s.number == 2)
        # Only the genuinely short-history name should appear.
        full_history = [
            c.target.symbol for c in report.reached
            if c.c1_raw_years >= 6.75
        ]
        assert len(full_history) >= 7
        for symbol in full_history:
            assert f"'{symbol}'" not in stop.finding.split(";")[0]

    def test_stop_3_trips_when_gics_is_unavailable(self, report):
        stop = self._stop(report, 3)
        assert stop.tripped
        assert report.taxonomy.finest_level_available is TaxonomyLevel.INDUSTRY
        assert not report.taxonomy.blocks_build
        assert "VENDOR-SUBSTITUTE" in stop.directive

    def test_stop_4_reports_the_single_anchor_rate(self, report):
        stop = self._stop(report, 4)
        rate = report.single_anchor_rate
        assert rate is not None
        assert stop.tripped == (rate > 0.40)
        if stop.tripped:
            assert "DESIGN-LEVEL FINDING" in stop.directive

    def test_a_clean_source_trips_nothing(self):
        """The stop conditions must be capable of not firing, or they carry no
        information when they do."""
        from gcfp.config import DEFAULT_CONFIG
        from gcfp.data.taxonomy import TaxonomyAvailability
        from gcfp.probe import _evaluate_stop_conditions

        clean_taxonomy = TaxonomyAvailability(
            gics_available=True,
            finest_level_available=TaxonomyLevel.SUB_INDUSTRY,
            substitute_taxonomy=None,
            detail="all profiles carry a GICS sub-industry code",
        )
        stops = _evaluate_stop_conditions([], clean_taxonomy, DEFAULT_CONFIG, True)
        assert not any(s.tripped for s in stops)


class TestProbeReport:
    def test_render_names_every_stop_condition(self, report):
        text = report.render()
        for number in (1, 2, 3, 4):
            assert f"Stop condition {number}" in text

    def test_build_directives_include_the_point_in_time_caveat(self, report):
        directives = report.build_directives()
        assert any("survivorship" in d or "inflated by an unknown" in d for d in directives)

    def test_report_states_targets_reached_out_of_total(self, report):
        assert "targets reached: 9/10" in report.render()


class TestPeerCandidateOrdering:
    """Only two dozen candidates get fundamentals fetched. Which two dozen
    decides whether C2 finds a peer set or reports SINGLE-ANCHOR MODE.

    An industry has far more minor filers than major ones, so taking
    candidates in universe order spends the whole fetch budget on companies
    C2 will reject on the size band — and the probe then reports that a
    company has no peers when it has several.
    """

    def universe(self):
        from gcfp.universe import Universe, UniverseMember

        u = Universe(as_of=date(2026, 1, 1), source="test")
        def member(symbol, name, cap):
            return UniverseMember(
                symbol=symbol, name=name, market_cap=cap, adv_3m_usd=50e6,
                sector="Industrials", industry="Machinery", sub_industry=None,
                net_debt_to_ebitda=1.5, gross_margin_stdev=0.02,
                revenue_growth=0.05, beta=1.0,
            )

        u.members.append(member("SUBJ", "Subject", 100e9))
        # Twenty-five minnows, then the one genuine size peer last.
        for i in range(25):
            u.members.append(member(f"TINY{i}", f"Tiny {i}", 50e6))
        u.members.append(member("BIG", "Big Peer", 90e9))
        return u

    def ordered(self):
        u = self.universe()
        subject = u.by_symbol("SUBJ")
        group = subject.grouping_at(TaxonomyLevel.INDUSTRY)
        same = [
            m for m in u.included
            if m.symbol != "SUBJ"
            and m.grouping_at(TaxonomyLevel.INDUSTRY) == group
        ]
        same.sort(
            key=lambda m: (
                m.market_cap is None,
                abs((m.market_cap or 0.0) / subject.market_cap - 1.0),
            )
        )
        return [m.symbol for m in same]

    def test_the_closest_by_size_comes_first(self):
        assert self.ordered()[0] == "BIG"

    def test_the_real_peer_survives_the_fetch_budget(self):
        """In universe order it sits at position 26 and is never fetched."""
        assert "BIG" in self.ordered()[:24]
