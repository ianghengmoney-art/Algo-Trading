"""The backtest engine, its bookkeeping, and its point-in-time discipline.

The most important test here is the lookahead one. A backtest with lookahead
does not raise — it produces a *better* result — so the only way to know the
discipline holds is to assert that an engine pinned to a date genuinely cannot
see past it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from gcfp.backtest.engine import (
    Backtester,
    BacktestSettings,
    WalkForwardSplit,
    month_ends,
)
from gcfp.backtest.fixtures import build_synthetic_market
from gcfp.backtest.metrics import (
    annualised_return,
    distribution,
    max_drawdown,
    summarise,
)
from gcfp.backtest.portfolio import BacktestBook, ClosedPosition
from gcfp.backtest.sweep import HoldoutLeak, ParameterSweep, SweepPoint, run_sweep
from gcfp.backtest.variants import build_variants, MISSING_BENCHMARKS, v3_circular_conviction
from gcfp.classification import Classification
from gcfp.runner import free_stack_config


@pytest.fixture(scope="module")
def market():
    return build_synthetic_market(date(2005, 1, 1), date(2020, 12, 31))


@pytest.fixture(scope="module")
def bt_config():
    return free_stack_config()


class TestPointInTime:
    """§13.8. A lookahead bug improves results silently, so it gets its own tests."""

    def test_the_fixture_hides_filings_that_had_not_happened(self, market):
        market.as_of = date(2018, 6, 30)
        try:
            quarters = market.get_quarterly_financials("IND0", 20)
            assert quarters, "no quarters visible at all"
            newest = quarters[0]
            assert newest.filing_date <= date(2018, 6, 30)
            # The March quarter files in mid-May; the June quarter has not
            # been filed on 30 June and must not be visible.
            assert newest.period_end == date(2018, 3, 31)
        finally:
            market.as_of = None

    def test_more_history_is_visible_later(self, market):
        try:
            market.as_of = date(2015, 6, 30)
            early = len(market.get_quarterly_financials("IND0", 200))
            market.as_of = date(2019, 6, 30)
            later = len(market.get_quarterly_financials("IND0", 200))
        finally:
            market.as_of = None
        assert later > early

    def test_the_reconstructed_c1_series_carries_no_future_observations(self, market):
        try:
            market.as_of = date(2016, 6, 30)
            series = market.get_historical_multiples("IND0", "trailing_pe", 7)
        finally:
            market.as_of = None
        assert series
        assert all(o.observation_date <= date(2016, 6, 30) for o in series)

    def test_the_engine_pins_the_adapter_to_each_rebalance_date(
        self, market, bt_config
    ):
        settings = BacktestSettings(
            start=date(2014, 1, 1), end=date(2014, 6, 30), initial_capital=50_000.0
        )
        engine = Backtester(market, bt_config, settings, ["IND0"])
        engine._pin(date(2013, 3, 31))
        assert market.as_of == date(2013, 3, 31)
        market.as_of = None


class TestWalkForward:
    def test_an_overlapping_split_is_rejected(self):
        with pytest.raises(ValueError, match="overlap"):
            WalkForwardSplit(
                date(2010, 1, 1), date(2020, 1, 1),
                date(2019, 1, 1), date(2025, 1, 1),
            )

    def test_by_fraction_produces_disjoint_periods(self):
        split = WalkForwardSplit.by_fraction(date(2010, 1, 1), date(2020, 1, 1))
        assert split.train_end < split.test_start
        assert split.contains_train(date(2012, 1, 1))
        assert not split.contains_test(date(2012, 1, 1))

    def test_a_sweep_reaching_into_the_holdout_is_an_error(self, bt_config):
        """§13.3: no tuning on the holdout. A leak here would not fail loudly
        on its own, so it is made an error."""
        split = WalkForwardSplit.by_fraction(date(2010, 1, 1), date(2020, 1, 1))
        with pytest.raises(HoldoutLeak, match="no tuning on the holdout"):
            run_sweep(
                lambda cfg: None,
                bt_config,
                split=split,
                period_start=split.train_start,
                period_end=split.test_end,
            )


class TestBookkeeping:
    def test_cash_is_conserved_across_a_round_trip(self):
        book = BacktestBook(cash=10_000.0)
        book.buy("A", Classification.CORE_STABLE, 4_000.0, 40.0, date(2020, 1, 1),
                 conviction=70.0, intended_weight=0.04, anchor_mode="DUAL",
                 thesis_invalidation="x")
        assert book.cash == pytest.approx(6_000.0)
        book.sell("A", 50.0, date(2021, 1, 1), "test exit")
        # 100 shares bought at 40, sold at 50.
        assert book.cash == pytest.approx(11_000.0)
        assert not book.positions
        assert len(book.closed) == 1
        assert book.closed[0].total_return == pytest.approx(0.25)

    def test_a_buy_cannot_spend_cash_the_book_does_not_have(self):
        book = BacktestBook(cash=1_000.0)
        book.buy("A", Classification.CORE_STABLE, 5_000.0, 10.0, date(2020, 1, 1),
                 conviction=70.0, intended_weight=0.05, anchor_mode="DUAL",
                 thesis_invalidation="x")
        assert book.cash == pytest.approx(0.0)
        assert book.positions["A"].cost_basis == pytest.approx(1_000.0)

    def test_a_partial_sell_trims_rather_than_closes(self):
        book = BacktestBook(cash=10_000.0)
        book.buy("A", Classification.CORE_STABLE, 4_000.0, 40.0, date(2020, 1, 1),
                 conviction=70.0, intended_weight=0.04, anchor_mode="DUAL",
                 thesis_invalidation="x")
        book.sell("A", 40.0, date(2020, 6, 1), "TRIM-TO-CAP", fraction=0.5)
        assert "A" in book.positions
        assert book.positions["A"].cost_basis == pytest.approx(2_000.0)
        assert not book.closed, "a trim must not record a closed position"

    def test_a_position_with_no_price_is_held_at_cost_not_dropped(self):
        """Silently removing an unpriceable position would flatter the result."""
        book = BacktestBook(cash=10_000.0)
        book.buy("A", Classification.CORE_STABLE, 4_000.0, 40.0, date(2020, 1, 1),
                 conviction=70.0, intended_weight=0.04, anchor_mode="DUAL",
                 thesis_invalidation="x")
        assert book.total_value({}) == pytest.approx(10_000.0)

    def test_per_position_drawdown_tracks_the_peak(self):
        book = BacktestBook(cash=10_000.0)
        book.buy("A", Classification.CORE_STABLE, 4_000.0, 40.0, date(2020, 1, 1),
                 conviction=70.0, intended_weight=0.04, anchor_mode="DUAL",
                 thesis_invalidation="x")
        book.mark({"A": 60.0})
        book.mark({"A": 30.0})
        book.sell("A", 30.0, date(2021, 1, 1), "exit")
        assert book.closed[0].max_drawdown == pytest.approx(-0.5)


class TestMetrics:
    def test_drawdown_depth_and_recovery(self):
        d = date(2020, 1, 31)
        curve = [
            (d + timedelta(days=30 * i), float(v))
            for i, v in enumerate([100, 120, 84, 90, 125])
        ]
        episode = max_drawdown(curve)
        assert episode.depth == pytest.approx(-0.30)
        assert episode.recovered_on is not None

    def test_an_unrecovered_drawdown_says_so(self):
        d = date(2020, 1, 31)
        curve = [
            (d + timedelta(days=30 * i), float(v))
            for i, v in enumerate([100, 120, 84, 90])
        ]
        assert max_drawdown(curve).recovered_on is None

    def test_the_median_is_reported_and_the_mean_is_secondary(self):
        """§13.6 asks for the distribution, not the mean — one outlier must not
        be able to carry the headline number."""
        closed = [
            ClosedPosition("A", Classification.CORE_STABLE, date(2020, 1, 1),
                           date(2021, 1, 1), 1000, 900, 70, "DUAL", "x", -0.2),
            ClosedPosition("B", Classification.CORE_STABLE, date(2020, 1, 1),
                           date(2021, 1, 1), 1000, 950, 70, "DUAL", "x", -0.1),
            ClosedPosition("C", Classification.CORE_STABLE, date(2020, 1, 1),
                           date(2021, 1, 1), 1000, 5000, 70, "DUAL", "x", -0.1),
        ]
        stats = distribution(closed)
        assert stats.median_return == pytest.approx(-0.05)
        assert stats.mean_return > 1.0, "the outlier should dominate the mean"
        assert stats.loss_rate == pytest.approx(2 / 3)

    def test_total_losses_are_counted_separately_from_losses(self):
        closed = [
            ClosedPosition("A", Classification.SPEC_GROWTH, date(2020, 1, 1),
                           date(2021, 1, 1), 1000, 20, 70, "DUAL", "x", -0.98),
            ClosedPosition("B", Classification.SPEC_GROWTH, date(2020, 1, 1),
                           date(2021, 1, 1), 1000, 800, 70, "DUAL", "x", -0.2),
        ]
        stats = distribution(closed)
        assert stats.loss_rate == pytest.approx(1.0)
        assert stats.total_loss_rate == pytest.approx(0.5)

    def test_annualised_return_over_two_years(self):
        curve = [(date(2020, 1, 1), 100.0), (date(2022, 1, 1), 121.0)]
        assert annualised_return(curve) == pytest.approx(0.10, abs=0.001)


class TestSweepAnalysis:
    def _sweep(self, values):
        return ParameterSweep(
            "p", None,
            [SweepPoint(v, r, -0.2, 0.4, 30) for v, r in values],
        )

    def test_a_broad_region_is_reported_as_a_plateau(self):
        sweep = self._sweep(
            [(0.15, 0.02), (0.20, 0.05), (0.25, 0.082),
             (0.30, 0.085), (0.35, 0.083), (0.40, 0.04)]
        )
        assert not sweep.is_spike
        assert len(sweep.plateau) >= 3
        assert sweep.recommended is not None

    def test_a_lone_winner_is_reported_as_a_spike_and_recommends_nothing(self):
        """Distrust spikes: a value that only wins alone has fitted noise."""
        sweep = self._sweep(
            [(0.15, 0.03), (0.20, 0.031), (0.25, 0.14), (0.30, 0.032), (0.35, 0.029)]
        )
        assert sweep.is_spike
        assert sweep.recommended is None
        assert any("SPIKE" in l for l in sweep.as_report_lines())

    def test_the_recommendation_is_the_middle_of_the_plateau_not_the_peak(self):
        sweep = self._sweep(
            [(0.10, 0.01), (0.20, 0.080), (0.30, 0.085), (0.40, 0.081), (0.50, 0.01)]
        )
        assert sweep.best.value == 0.30
        assert sweep.recommended == 0.30
        assert len(sweep.plateau) == 3


class TestVariants:
    def test_six_benchmarks_are_runnable_and_the_seventh_is_declared_missing(
        self, bt_config
    ):
        variants = build_variants(bt_config)
        assert len(variants) == 6
        assert "vs old GCFP v2 rules" in MISSING_BENCHMARKS
        assert "not supplied" in MISSING_BENCHMARKS["vs old GCFP v2 rules"]

    def test_each_variant_names_what_it_proves(self, bt_config):
        for variant in build_variants(bt_config):
            assert variant.proves

    def test_the_single_anchor_variant_makes_c2_uncomputable(self, bt_config):
        variant = next(
            v for v in build_variants(bt_config) if "single-anchor" in v.name
        )
        assert variant.config.anchors.peer_min > 1000

    def test_the_no_momentum_variant_zeroes_d5(self, bt_config):
        variant = next(v for v in build_variants(bt_config) if "momentum" in v.name)
        assert variant.config.conviction.momentum_max == 0.0

    def test_v3_scoring_awards_points_the_gate_already_consumed(
        self, bt_config, market
    ):
        """The whole point of the D2/D3 correction.

        v3 gave a flat 15 for "both anchors confirm" — a fact Module E's gate
        already requires of every passer — and scored the absolute discount
        rather than the excess above the gate. So a name sitting exactly on its
        gate scored well under v3 and scores near zero under v4.
        """
        from gcfp.backtest.variants import v3_circular_conviction
        from gcfp.ledger import AuditLedger
        from gcfp.modules import a_health, d_conviction
        from gcfp.modules.c_anchors import (
            AnchorMode, AnchorReading, TriangulationResult,
        )
        from tests.conftest import build_company

        data = build_company()
        market_data = __import__(
            "gcfp.types", fromlist=["MarketData"]
        ).MarketData(
            risk_free_rate=0.042,
            group_net_debt_ebitda_median={"Machinery": 1.8},
            group_member_counts={"Machinery": 22},
        )
        health = a_health.run_module_a(
            data, market_data, bt_config, AuditLedger("STABLECO", date.today())
        )
        at_the_gate = TriangulationResult(
            symbol="STABLECO",
            c1=AnchorReading("C1", True, 14.0, 20.0, implied_discount=0.26),
            c2=AnchorReading("C2", True, 14.0, 20.0, implied_discount=0.27),
            c3_pegy=None, c3_flag=False, mode=AnchorMode.DUAL,
            anchors_disagree=False, divergence=0.01, conservative_discount=0.26,
        )

        kwargs = dict(
            data=data, market=market_data, config=bt_config,
            classification=Classification.CORE_STABLE, health=health,
            triangulation=at_the_gate,
            actual_discount=0.26, gate_threshold=0.25, discount_rate=None,
            momentum_returns={"STABLECO": 0.1, "B": 0.2, "C": 0.3},
        )
        v3 = v3_circular_conviction(**kwargs)
        v4 = d_conviction.score_conviction(**kwargs)

        v4_valuation = v4.component("valuation excess").points
        v4_anchor = v4.component("anchor conservatism").points
        v3_valuation = next(
            c for c in v3.components if c.name == "valuation (v3 absolute)"
        ).points
        v3_anchor = next(
            c for c in v3.components if c.name == "anchor agreement (v3 flat)"
        ).points

        # A name one point above its gate: v4 says that is worth almost nothing,
        # v3 rewarded it for clearing a bar the gate had already enforced.
        assert v4_valuation < 2.0, "v4 should score a name at its gate near zero"
        assert v3_valuation > 10.0, "v3 scored the absolute discount"
        assert v3_anchor == 15.0, "v3's flat award carried no information"
        assert v4_anchor < v3_anchor
        assert v3.total > v4.total

class TestEngineRun:
    def test_a_short_run_completes_and_produces_snapshots(self, market, bt_config):
        settings = BacktestSettings(
            start=date(2013, 1, 1), end=date(2013, 12, 31), initial_capital=100_000.0
        )
        result = Backtester(
            market, bt_config, settings, [f"IND{i}" for i in range(9)]
        ).run()
        assert len(result.rebalances) == 12
        assert len(result.book.snapshots) == 12
        assert result.equity_curve

    def test_month_ends_lands_on_the_last_day_of_each_month(self):
        days = month_ends(date(2020, 1, 1), date(2020, 3, 31))
        assert days == [date(2020, 1, 31), date(2020, 2, 29), date(2020, 3, 31)]

    def test_every_position_is_closed_by_the_end_of_the_run(self, market, bt_config):
        """Otherwise the return distribution silently excludes the winners
        still open at the end."""
        settings = BacktestSettings(
            start=date(2013, 1, 1), end=date(2014, 12, 31), initial_capital=100_000.0
        )
        result = Backtester(
            market, bt_config, settings, [f"IND{i}" for i in range(9)]
        ).run()
        assert not result.book.positions


class TestDelistedHoldings:
    """A holding whose price stops has usually delisted. It used to be valued
    at zero for that month and then never closed, so it vanished from every
    return statistic — a company that went bust never counted as a loss."""

    @staticmethod
    def book_with(symbol="DEAD", price=50.0):
        from gcfp.backtest.portfolio import BacktestBook
        from gcfp.classification import Classification

        book = BacktestBook(cash=10_000.0)
        book.buy(symbol, Classification.CORE_STABLE, 5_000.0, price, date(2020, 1, 31),
                 conviction=65.0, intended_weight=0.05, anchor_mode="DUAL",
                 thesis_invalidation="test")
        return book

    def test_a_missing_month_is_carried_at_the_last_close_not_at_zero(self):
        book = self.book_with()
        book.mark({"DEAD": 40.0})
        book.mark({})
        assert book.invested_value({}) == pytest.approx(100 * 40.0)
        assert book.positions["DEAD"].missed_marks == 1

    def test_a_holding_unpriced_past_the_grace_period_is_closed_at_a_loss(self):
        from gcfp.backtest.engine import DELISTED_REASON, BacktestResult, Backtester, BacktestSettings
        from gcfp.config import Config

        book = self.book_with()
        book.mark({"DEAD": 40.0})
        settings = BacktestSettings(start=date(2020, 1, 1), end=date(2021, 1, 1))
        engine = Backtester(adapter=None, config=Config(), settings=settings, symbols=[])
        result = BacktestResult(label="t", settings=settings, split=None, book=book,
                                config_fingerprint="x")
        engine._close_as_delisted(book, "DEAD", date(2020, 6, 30), result)

        assert "DEAD" not in book.positions, "a dead holding must not stay open"
        closed = book.closed[-1]
        assert DELISTED_REASON in closed.exit_reason
        assert closed.total_return < 0, "a delisting must count as a loss"
        assert result.assumed_delistings == 1

    def test_the_assumed_delisting_return_is_configurable(self):
        from gcfp.backtest.engine import BacktestResult, Backtester, BacktestSettings
        from gcfp.config import Config

        book = self.book_with(price=50.0)
        settings = BacktestSettings(start=date(2020, 1, 1), end=date(2021, 1, 1),
                                    delisting_return=-1.0)
        engine = Backtester(adapter=None, config=Config(), settings=settings, symbols=[])
        result = BacktestResult(label="t", settings=settings, split=None, book=book,
                                config_fingerprint="x")
        engine._close_as_delisted(book, "DEAD", date(2020, 6, 30), result)
        assert book.closed[-1].total_return == pytest.approx(-1.0)


class TestEligibility:
    def test_a_symbol_is_screened_only_while_it_was_filing(self, monkeypatch):
        from gcfp.backtest import engine as engine_module
        from gcfp.backtest.engine import Backtester, BacktestSettings
        from gcfp.config import Config

        from gcfp.universe import Universe

        seen: list[list[str]] = []

        def fake_build(adapter, symbols, config, as_of=None, progress=False):
            seen.append(list(symbols))
            return Universe(as_of=as_of)

        monkeypatch.setattr(engine_module, "build_universe", fake_build)
        settings = BacktestSettings(start=date(2020, 1, 1), end=date(2021, 1, 1))
        engine = Backtester(
            None, Config(), settings, ["ALIVE", "DEAD"],
            eligibility=lambda s, as_of: s == "ALIVE" or as_of < date(2020, 6, 1),
        )
        engine._default_universe(None, Config(), date(2020, 3, 31))
        engine._default_universe(None, Config(), date(2020, 9, 30))
        assert seen == [["ALIVE", "DEAD"], ["ALIVE"]]


class TestIndustryPeers:
    """C2 needs same-industry peers; a random sample rarely has any."""

    def test_peers_join_the_universe_but_never_the_candidate_list(self, monkeypatch):
        from gcfp.backtest import engine as engine_module
        from gcfp.backtest.engine import Backtester, BacktestSettings
        from gcfp.config import Config
        from gcfp.universe import Universe, UniverseMember

        def member(symbol):
            return UniverseMember(
                symbol=symbol, name=symbol, market_cap=1e9, adv_3m_usd=1e7,
                sector="S", industry="I", sub_industry=None,
                net_debt_to_ebitda=None, gross_margin_stdev=None,
                revenue_growth=None, beta=None,
            )

        calls: list[list[str]] = []

        def fake_build(adapter, symbols, config, as_of=None, progress=False):
            calls.append(list(symbols))
            return Universe(as_of=as_of, members=[member(s) for s in symbols])

        monkeypatch.setattr(engine_module, "build_universe", fake_build)
        settings = BacktestSettings(start=date(2020, 1, 1), end=date(2021, 1, 1))
        cache: dict = {}
        engine = Backtester(None, Config(), settings, ["A"],
                            peer_pool=lambda s: ["P1", "P2", "A"],
                            member_cache=cache)
        base = Universe(as_of=date(2020, 3, 31), members=[member("A")])

        merged = engine._with_industry_peers(None, base, ["A"], date(2020, 3, 31))
        assert [m.symbol for m in merged.members] == ["A", "P1", "P2"]
        assert [m.symbol for m in base.members] == ["A"], "the base universe was mutated"

        # A benchmark run on the same date reuses the screened peers.
        again = Backtester(None, Config(), settings, ["A"],
                           peer_pool=lambda s: ["P1", "P2"], member_cache=cache)
        again._with_industry_peers(None, base, ["A"], date(2020, 3, 31))
        assert calls == [["P1", "P2"]]


class TestDeadline:
    def test_a_run_past_its_deadline_stops_and_says_how_far_it_got(self):
        import time

        from gcfp.backtest.fixtures import build_synthetic_market
        from gcfp.backtest.engine import Backtester, BacktestSettings
        from gcfp.config import Config

        market = build_synthetic_market(date(2010, 1, 1), date(2015, 12, 31))
        symbols = [s for s in sorted(market.companies) if not s.startswith("^")]
        settings = BacktestSettings(start=date(2015, 1, 1), end=date(2015, 12, 31))
        result = Backtester(market, Config(), settings, symbols).run(
            deadline=time.monotonic() - 1
        )
        assert len(result.rebalances) == 0
        assert any("STOPPED EARLY" in n for n in result.notes)
        assert not result.book.positions, "positions must still be closed out"

    def test_a_month_that_stalls_is_interrupted_not_waited_for(self, monkeypatch):
        """A month stuck inside a download outlasted three jobs; the timer
        must cut it off and the run must still close out and report."""
        import time

        from gcfp.backtest.fixtures import build_synthetic_market
        from gcfp.backtest.engine import Backtester, BacktestSettings
        from gcfp.config import Config

        market = build_synthetic_market(date(2010, 1, 1), date(2015, 12, 31))
        symbols = [s for s in sorted(market.companies) if not s.startswith("^")]
        settings = BacktestSettings(start=date(2015, 1, 1), end=date(2015, 12, 31))
        engine = Backtester(market, Config(), settings, symbols)
        real = engine._rebalance

        def stalls_in_month_three(book, as_of, result):
            if len(result.rebalances) == 2:
                time.sleep(30)  # far past the deadline below
            return real(book, as_of, result)

        monkeypatch.setattr(engine, "_rebalance", stalls_in_month_three)
        started = time.monotonic()
        result = engine.run(deadline=time.monotonic() + 3)
        assert time.monotonic() - started < 15, "the stalled month was waited for"
        assert len(result.rebalances) == 2
        assert result.stopped_early_at == date(2015, 3, 31)
        assert any("STOPPED EARLY during 2015-03-31" in n for n in result.notes)
        assert not result.book.positions


class TestAccuracyDeadline:
    def test_routings_past_the_deadline_are_counted_not_dropped_silently(self):
        import time

        from gcfp.backtest.accuracy import measure_accuracy
        from gcfp.classification import Classification
        from gcfp.config import Config

        routings = [("A", Classification.CORE_STABLE, date(2015, 1, 31))] * 5
        report = measure_accuracy(None, Config(), routings, deadline=time.monotonic() - 1)
        assert report.not_judged_for_time == 5
        assert any("NOT judged" in line for line in report.as_report_lines())
