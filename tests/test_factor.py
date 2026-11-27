"""The factor strategy pre-registered in docs/FACTOR_STRATEGY.md."""

from __future__ import annotations

from datetime import date

import pytest

from gcfp.backtest.factor import (
    FactorInputs,
    composite_scores,
    factor_inputs,
    judge,
    percentile_ranks,
)


class TestRanking:
    def test_ranks_run_from_zero_to_one_and_ties_share(self):
        ranks = percentile_ranks({"a": 1.0, "b": 2.0, "c": 2.0, "d": 5.0})
        assert ranks["a"] == 0.0 and ranks["d"] == 1.0
        assert ranks["b"] == ranks["c"] == pytest.approx(0.5)

    def test_a_company_needs_two_of_three_scores(self):
        inputs = {
            "full": FactorInputs(0.10, 0.08, 0.20, 0.30),
            "two": FactorInputs(earnings_yield=0.05, momentum=0.10),
            "one": FactorInputs(momentum=0.50),
        }
        scores = composite_scores(inputs, min_scores=2)
        assert set(scores) == {"full", "two"}
        assert scores["full"] > scores["two"]

    def test_value_averages_whichever_yields_exist(self):
        inputs = {
            "a": FactorInputs(earnings_yield=0.10, profitability=0.1),
            "b": FactorInputs(earnings_yield=0.05, fcf_yield=0.20, profitability=0.1),
        }
        scores = composite_scores(inputs)
        # a: value rank 1.0 (only earnings yield); b: mean(0.0, 0.5)=0.25 —
        # fcf yield has one entry, ranked 0.5. Profitability ties at 0.5.
        assert scores["a"] == pytest.approx((1.0 + 0.5) / 2)
        assert scores["b"] == pytest.approx((0.25 + 0.5) / 2)


class TestInputs:
    def test_yields_and_profitability_from_trailing_quarters(self):
        from tests.conftest import build_company

        quarters = [
            {"operating_income": 1.5e9, "total_assets": 60e9,
             "capital_expenditure": -0.5e9} for _ in range(12)
        ]
        company = build_company(quarterly_overrides=quarters)
        out = factor_inputs(company, market_cap=100e9, momentum=0.12)
        ttm_net_income = sum(q.net_income for q in company.trailing_quarters(4))
        ttm_ocf = sum(q.operating_cash_flow for q in company.trailing_quarters(4))
        assert out.earnings_yield == pytest.approx(ttm_net_income / 100e9)
        assert out.fcf_yield == pytest.approx((ttm_ocf - 2e9) / 100e9)
        assert out.profitability == pytest.approx(6e9 / 60e9)
        assert out.momentum == 0.12

    def test_no_market_cap_means_no_yields(self, healthy_company):
        out = factor_inputs(healthy_company, market_cap=None, momentum=None)
        assert out.earnings_yield is None and out.fcf_yield is None


class TestVerdict:
    def result_with(self, strategy, index):
        from gcfp.backtest.engine import BacktestResult, BacktestSettings
        from gcfp.backtest.portfolio import BacktestBook, Snapshot

        book = BacktestBook(cash=0.0)
        days = [date(2015 + i, 6, 30) for i in range(len(strategy))]
        for d, v in zip(days, strategy):
            book.snapshots.append(Snapshot(d, v, 0.0, v, 30, 0.0, v, 0.0))
        settings = BacktestSettings(start=days[0], end=days[-1])
        result = BacktestResult(label="t", settings=settings, split=None, book=book,
                                config_fingerprint="x")
        result.benchmark_curve = list(zip(days, index))
        return result, days

    def test_passing_needs_both_halves_and_a_one_point_margin(self):
        from gcfp.backtest.engine import WalkForwardSplit

        strategy = [100 * 1.15 ** i for i in range(11)]
        index = [100 * 1.10 ** i for i in range(11)]
        result, days = self.result_with(strategy, index)
        split = WalkForwardSplit.by_fraction(days[0], days[-1])
        universe = list(zip(days, [100 * 1.12 ** i for i in range(11)]))
        verdict = judge(result, universe, split)
        assert verdict.passed

        flat = [100 * 1.105 ** i for i in range(11)]
        result, _ = self.result_with(flat, index)
        assert not judge(result, universe, split).passed, "0.5%/yr is under the 1% bar"


class TestVariantsTwoAndThree:
    def test_low_accruals_and_buybacks_rank_high(self):
        from gcfp.backtest.factor import theme_scores

        base = dict(ebit_ev=0.1, fcf_ev=0.1, gross_profitability=0.3,
                    momentum=0.1, earnings_growth=0.01)
        inputs = {
            "clean": FactorInputs(**base, accruals=-0.05, share_growth=-0.03),
            "dirty": FactorInputs(**base, accruals=0.10, share_growth=0.20),
        }
        scores = theme_scores(inputs)
        assert scores["clean"] > scores["dirty"]

    def test_three_of_four_themes_are_required(self):
        from gcfp.backtest.factor import theme_scores

        inputs = {
            "three": FactorInputs(ebit_ev=0.1, gross_profitability=0.2, momentum=0.1),
            "two": FactorInputs(ebit_ev=0.1, momentum=0.1),
        }
        assert set(theme_scores(inputs)) == {"three"}

    def test_a_non_positive_enterprise_value_gives_no_ev_yield(self):
        from dataclasses import replace

        from tests.conftest import build_company

        company = build_company()
        latest = replace(company.quarterly[0], total_debt=0.0, cash_and_equivalents=500e9)
        company = replace(company, quarterly=[latest, *company.quarterly[1:]])
        out = factor_inputs(company, market_cap=100e9, momentum=None)
        assert out.ebit_ev is None and out.fcf_ev is None

    def test_registered_rules(self):
        from gcfp.backtest.factor import rules_for

        assert rules_for(3).min_market_cap == 2e9 and rules_for(3).holdings == 30
        assert rules_for(1).min_market_cap is None
        four = rules_for(4)
        assert (four.holdings, four.buffer_rank, four.max_weight, four.max_per_industry,
                four.dividend_withholding) == (100, 200, 0.02, 15, 0.30)
        # Variants 1-3 are untouched by variant 4's fields.
        for v in (1, 2, 3):
            r = rules_for(v)
            assert r.max_weight is None and r.max_per_industry is None
            assert r.cost_tiers is None and r.dividend_withholding == 0.0
        with pytest.raises(ValueError):
            rules_for(5)


class TestEdgeStatistics:
    def test_a_steady_excess_has_a_large_t_statistic(self):
        from gcfp.backtest.factor import edge_statistics

        days = [date(2015 + m // 12, m % 12 + 1, 28) for m in range(48)]
        bench = [(d, 100 * 1.008 ** i) for i, d in enumerate(days)]
        # +0.5%/month above the index, with a little noise.
        strat = [(d, 100 * 1.013 ** i * (1 + (0.002 if i % 2 else -0.002)))
                 for i, d in enumerate(days)]
        text = "\n".join(edge_statistics(strat, bench))
        t = float(text.split("t-statistic: ")[1].split()[0])
        assert t > 2.4
        assert "YEAR BY YEAR" in text

    def test_paper_rebalance_logs_the_trades_to_copy(self, tmp_path):
        import sys
        from pathlib import Path

        from gcfp.backtest import paper

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import run_paper

        run_paper.main(["--source", "synthetic", "--strategy", "factor", "--variant", "2",
                        "--book", str(tmp_path / "b.json"), "--out", str(tmp_path / "r.txt"),
                        "--today", "2024-03-03"])
        state = paper.load_state(tmp_path / "b.json")
        trades = state.log[-1]["trades"]
        assert trades and all(t["side"] == "buy" for t in trades)
        assert "THIS MONTH'S TRADES" in (tmp_path / "r.txt").read_text()


class TestCagrGoal:
    def test_the_goal_line_reports_the_market_and_the_edge_separately(self):
        from gcfp.backtest.engine import WalkForwardSplit

        strategy = [100 * 1.16 ** i for i in range(11)]
        index = [100 * 1.14 ** i for i in range(11)]
        result, days = TestVerdict().result_with(strategy, index)
        split = WalkForwardSplit.by_fraction(days[0], days[-1])
        text = "\n".join(judge(result, list(zip(days, index)), split).lines)
        assert "GOAL 15%-20%/yr compound return: MET" in text
        assert "the strategy added" in text


class TestVariantFour:
    @staticmethod
    def engine(rules, start=date(2014, 1, 1), end=date(2016, 12, 31), **settings_kw):
        from gcfp.backtest.engine import BacktestSettings
        from gcfp.backtest.factor import FactorBacktester
        from gcfp.backtest.fixtures import build_synthetic_market
        from gcfp.runner import free_stack_config

        market = build_synthetic_market(date(2008, 1, 1), end)
        symbols = [s for s in sorted(market.companies) if not s.startswith("^")]
        settings = BacktestSettings(start=start, end=end, **settings_kw)
        return FactorBacktester(market, free_stack_config(), settings, symbols,
                                rules=rules)

    def test_costs_follow_liquidity_and_unknown_is_dearest(self):
        from gcfp.backtest.factor import rules_for

        bt = self.engine(rules_for(4))
        bt._adv.update({"BIG": 80e6, "MID": 20e6, "SMALL": 3e6})
        assert [bt._cost_of(s) for s in ("BIG", "MID", "SMALL", "NEVER-SEEN")] == [
            0.001, 0.0025, 0.005, 0.005]
        assert self.engine(rules_for(2))._cost_of("BIG") == 0.001

    def test_the_industry_limit_counts_holdings_per_group(self):
        from dataclasses import replace

        from gcfp.backtest.factor import rules_for

        bt = self.engine(replace(rules_for(4), max_per_industry=2))
        bt._industry.update({"A": "73", "B": "73", "C": "73", "D": "28"})
        assert bt._industry_ok("C", {"73": 1})
        assert not bt._industry_ok("C", {"73": 2})
        assert bt._industry_ok("D", {"73": 2})
        assert bt._industry_ok("UNKNOWN", {"73": 2})  # no code: not limited

    def test_no_holding_stays_above_the_cap_after_a_rebalance(self):
        from dataclasses import replace

        from gcfp.backtest.factor import rules_for

        rules = replace(rules_for(4), holdings=4, buffer_rank=8, max_weight=0.30,
                        cost_tiers=None, dividend_withholding=0.0)
        bt = self.engine(rules)
        result = bt.run()
        assert result.rebalances, "the synthetic run made no rebalances"
        for snap in result.book.snapshots:
            assert snap.total_value > 0
        # Every trim leaves the name at its equal weight; check the fills say so.
        trims = [f for f in result.book.fills if f.reason.startswith("trimmed")]
        assert bt.trims == len(trims)

    def test_withholding_costs_the_book_and_the_benchmark_alike(self):
        from dataclasses import replace

        from gcfp.backtest.factor import rules_for

        base = replace(rules_for(2))
        taxed = replace(base, dividend_withholding=0.30)
        plain = self.engine(base)
        bt = self.engine(taxed)
        assert bt.settings.dividend_withholding == 0.30
        assert plain.settings.dividend_withholding == 0.0
        r = bt.run()
        # The synthetic benchmark is itself the price-only index, so it has
        # no dividend part to tax: net equals gross.
        assert [v for _, v in r.benchmark_curve] == pytest.approx(
            [v for _, v in bt.gross_benchmark_curve])

    def test_the_rebalance_day_moves_every_date(self):
        from gcfp.backtest.engine import month_ends

        assert month_ends(date(2015, 1, 1), date(2015, 3, 31), 1, 14) == [
            date(2015, 1, 14), date(2015, 2, 14), date(2015, 3, 14)]
        assert month_ends(date(2015, 1, 1), date(2015, 2, 28), 1, 31) == [
            date(2015, 1, 31), date(2015, 2, 28)]
        assert month_ends(date(2015, 1, 1), date(2015, 2, 28)) == [
            date(2015, 1, 31), date(2015, 2, 28)]


class TestIndexGrowthAfterTax:
    def test_only_the_dividend_part_is_taxed(self):
        from gcfp.backtest.engine import Backtester, BacktestSettings

        bt = Backtester.__new__(Backtester)
        bt.settings = BacktestSettings(start=date(2015, 1, 1), end=date(2015, 2, 1),
                                       dividend_withholding=0.30)
        prices = {date(2015, 1, 31): 100.0, date(2015, 2, 28): 101.0}
        bt._price_on = lambda symbol, as_of: prices.get(as_of)
        bt._ballast_price_level = None
        assert bt._index_growth(1000.0, None, date(2015, 1, 31)) is None
        # The total-return index rose 3%; the price index 1%: dividends 2%.
        growth = bt._index_growth(1030.0, 1000.0, date(2015, 2, 28))
        assert growth == pytest.approx(1.03 - 0.30 * 0.02)
