"""The holdings monitoring loop.

The two things worth proving: cadence follows the holding's own reporting
frequency rather than the calendar, and conviction is compared against what it
was at purchase — which only works because the store appends rather than
updates.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from gcfp.classification import Classification, Regime
from gcfp.modules.d_conviction import ConvictionScore
from gcfp.modules.f_sizing import Bucket, PortfolioState
from gcfp.modules.h_monitor import MonitorFlag
from gcfp.modules.k_currency import FxTable
from gcfp.monitor import (
    Holding,
    discipline_report,
    load_holdings,
    review_holding,
    run_monitor,
)
from gcfp.pipeline import CandidateInputs
from gcfp.storage import Store
from gcfp.types import ReportingFrequency
from tests.conftest import build_company, stable_profile


@pytest.fixture
def store(tmp_path, config):
    """A store holding one open position with a purchase-time score."""
    store = Store(tmp_path / "gcfp.sqlite")
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO holdings (symbol, classification, opened_on, shares, "
            "cost_base_currency, base_currency, thesis_invalidation, anchor_mode, "
            "config_fingerprint) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "STABLECO", "CORE-STABLE", "2026-01-15", 1000.0, 150_000.0, "SGD",
                "ROIC-WACC spread below zero for two consecutive years",
                "DUAL", config.fingerprint,
            ),
        )
    store.record_conviction(
        ConvictionScore("STABLECO", 72.0, [], "standard"),
        "DUAL", config.fingerprint, scored_on=date(2026, 1, 15),
    )
    return store


@pytest.fixture
def book():
    return PortfolioState(
        total_value=1_000_000.0,
        bucket_values={
            Bucket.BALLAST: 500_000.0,
            Bucket.CORE_PICKS: 350_000.0,
            Bucket.GROWTH_PICKS: 50_000.0,
        },
        position_values={"STABLECO": 150_000.0, "OTHER": 200_000.0},
        position_sectors={"STABLECO": "Industrials", "OTHER": "Technology"},
        position_regimes={"STABLECO": Regime.CORE, "OTHER": Regime.CORE},
    )


@pytest.fixture
def fx():
    return FxTable({"USDSGD": 1.29}, date.today())


def holding(**kw) -> Holding:
    defaults = dict(
        symbol="STABLECO",
        classification=Classification.CORE_STABLE,
        opened_on=date(2026, 1, 15),
        shares=1000.0,
        cost_base_currency=150_000.0,
        thesis_invalidation="ROIC-WACC spread below zero for two years",
        conviction_at_purchase=72.0,
    )
    defaults.update(kw)
    return Holding(**defaults)


class TestLoadHoldings:
    def test_open_positions_carry_their_purchase_conviction(self, store):
        holdings = load_holdings(store)
        assert len(holdings) == 1
        assert holdings[0].symbol == "STABLECO"
        assert holdings[0].conviction_at_purchase == 72.0

    def test_closed_positions_are_not_loaded(self, store):
        with store.connect() as conn:
            conn.execute("UPDATE holdings SET closed_on = '2026-06-01'")
        assert load_holdings(store) == []


class TestCadence:
    def test_a_holding_with_nothing_new_filed_is_not_re_run(
        self, market, config, book, fx
    ):
        """A rigid calendar re-run either processes stale data or silently
        skips; H1 keys off the holding's own reporting instead."""
        from gcfp.data.fixtures import FixtureAdapter, FixtureCompany

        last_report = date.today() - timedelta(days=20)
        data = build_company(
            profile=stable_profile(last_report_date=last_report)
        )
        adapter = FixtureAdapter()
        adapter.companies["STABLECO"] = FixtureCompany(
            profile=data.profile, annual=list(data.annual),
            quarterly=list(data.quarterly), prices=list(data.prices),
        )
        review = review_holding(
            holding(last_full_rerun=last_report), adapter, market, config,
            CandidateInputs(), book, fx,
        )
        assert not review.cadence.should_full_rerun
        assert review.evaluation is None
        assert review.verdict.flag is MonitorFlag.GREEN

    def test_a_newly_filed_report_triggers_a_full_re_run(
        self, market, config, book, fx
    ):
        from gcfp.data.fixtures import FixtureAdapter, FixtureCompany

        data = build_company(
            profile=stable_profile(last_report_date=date.today() - timedelta(days=5))
        )
        adapter = FixtureAdapter()
        adapter.companies["STABLECO"] = FixtureCompany(
            profile=data.profile, annual=list(data.annual),
            quarterly=list(data.quarterly), prices=list(data.prices),
            multiples={"trailing_pe": list(data.multiples)},
        )
        review = review_holding(
            holding(last_full_rerun=date.today() - timedelta(days=120)),
            adapter, market, config,
            CandidateInputs(current_multiple=13.0), book, fx,
        )
        assert review.cadence.should_full_rerun
        assert review.evaluation is not None
        assert review.conviction_now is not None

    def test_a_semiannual_reporter_gets_a_midpoint_check_not_a_re_run(
        self, market, config, book, fx
    ):
        from gcfp.data.fixtures import FixtureAdapter, FixtureCompany

        last_report = date.today() - timedelta(days=200)
        data = build_company(
            profile=stable_profile(
                last_report_date=last_report,
                reporting_frequency=ReportingFrequency.SEMIANNUAL,
            )
        )
        adapter = FixtureAdapter()
        adapter.companies["STABLECO"] = FixtureCompany(
            profile=data.profile, annual=list(data.annual),
            quarterly=list(data.quarterly), prices=list(data.prices),
        )
        review = review_holding(
            holding(last_full_rerun=last_report), adapter, market, config,
            CandidateInputs(), book, fx,
        )
        assert review.cadence.should_price_and_news_check
        assert not review.cadence.should_full_rerun


class TestVerdicts:
    def test_missing_data_is_a_review_not_a_silent_pass(
        self, market, config, book, fx
    ):
        """A position whose data has vanished is not healthy by default."""
        from gcfp.data.fixtures import FixtureAdapter

        review = review_holding(
            holding(), FixtureAdapter(), market, config,
            CandidateInputs(), book, fx,
        )
        assert review.verdict.flag is MonitorFlag.REVIEW
        assert review.error is not None

    def test_a_late_filing_is_a_sell_even_without_a_re_run(
        self, market, config, book, fx
    ):
        from gcfp.data.fixtures import FixtureAdapter, FixtureCompany

        last_report = date.today() - timedelta(days=200)
        data = build_company(
            profile=stable_profile(
                last_report_date=last_report,
                next_report_due_date=date.today() - timedelta(days=40),
            )
        )
        adapter = FixtureAdapter()
        adapter.companies["STABLECO"] = FixtureCompany(
            profile=data.profile, annual=list(data.annual),
            quarterly=list(data.quarterly), prices=list(data.prices),
        )
        review = review_holding(
            holding(last_full_rerun=last_report), adapter, market, config,
            CandidateInputs(), book, fx,
        )
        assert review.verdict.flag is MonitorFlag.SELL
        assert any("late" in r for r in review.verdict.sell_reasons)


class TestRunMonitor:
    def test_the_loop_persists_a_new_conviction_row(self, store, market, config, book, fx):
        """Appending rather than updating is what makes the purchase-time
        comparison possible at all."""
        from gcfp.data.fixtures import FixtureAdapter, FixtureCompany

        data = build_company(
            profile=stable_profile(last_report_date=date.today() - timedelta(days=5))
        )
        adapter = FixtureAdapter()
        adapter.companies["STABLECO"] = FixtureCompany(
            profile=data.profile, annual=list(data.annual),
            quarterly=list(data.quarterly), prices=list(data.prices),
            multiples={"trailing_pe": list(data.multiples)},
        )
        before = store.count("conviction_history")
        reviews = run_monitor(
            store, adapter, market, config, book, fx,
            {"STABLECO": CandidateInputs(current_multiple=13.0)},
        )
        assert len(reviews) == 1
        assert store.count("conviction_history") == before + 1
        assert store.conviction_at_purchase("STABLECO") == 72.0, (
            "the purchase score was overwritten"
        )

    def test_audit_entries_are_written_for_each_re_run(
        self, store, market, config, book, fx
    ):
        from gcfp.data.fixtures import FixtureAdapter, FixtureCompany

        data = build_company(
            profile=stable_profile(last_report_date=date.today() - timedelta(days=5))
        )
        adapter = FixtureAdapter()
        adapter.companies["STABLECO"] = FixtureCompany(
            profile=data.profile, annual=list(data.annual),
            quarterly=list(data.quarterly), prices=list(data.prices),
            multiples={"trailing_pe": list(data.multiples)},
        )
        run_monitor(store, adapter, market, config, book, fx,
                    {"STABLECO": CandidateInputs(current_multiple=13.0)})
        assert store.count("audit_entries") > 0


class TestDisciplineReport:
    def test_a_position_past_its_cap_is_flagged_for_trimming(self, book, config):
        """Grew past its cap on appreciation: an unmanaged risk the portfolio
        did not choose to take at that size."""
        oversized = PortfolioState(
            total_value=1_000_000.0,
            position_values={"STABLECO": 400_000.0, "OTHER": 100_000.0},
            position_regimes={"STABLECO": Regime.CORE, "OTHER": Regime.CORE},
        )
        from gcfp.modules.h_monitor import MonitorVerdict
        from gcfp.monitor import HoldingReview
        from gcfp.modules.h_monitor import CadenceDecision

        review = HoldingReview(
            holding(),
            MonitorVerdict("STABLECO", MonitorFlag.GREEN),
            CadenceDecision(False, False, ReportingFrequency.QUARTERLY, "n/a"),
        )
        report = discipline_report([review], oversized, config)
        assert "STABLECO" in report.trim_to_cap

    def test_thesis_broken_positions_are_surfaced(self, book, config):
        from gcfp.modules.h_monitor import CadenceDecision, MonitorVerdict
        from gcfp.monitor import HoldingReview

        review = HoldingReview(
            holding(thesis_broken=True),
            MonitorVerdict("STABLECO", MonitorFlag.SELL),
            CadenceDecision(False, False, ReportingFrequency.QUARTERLY, "n/a"),
        )
        report = discipline_report([review], book, config)
        assert "STABLECO" in report.thesis_broken
        assert any("independent of price" in l for l in report.as_report_lines())
