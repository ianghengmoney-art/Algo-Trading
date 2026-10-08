"""Paper trading: the backtest's monthly step on live data, carried week to
week in a file. The file is the only memory between runs, so it must round-
trip exactly, and a run that is cut short must leave it untouched."""

from __future__ import annotations

from datetime import date

import pytest

from gcfp.backtest import paper
from gcfp.backtest.portfolio import BacktestBook
from gcfp.classification import Classification


def a_book() -> BacktestBook:
    book = BacktestBook(cash=10_000.0)
    book.buy("A", Classification.CORE_STABLE, 4_000.0, 40.0, date(2026, 10, 4),
             conviction=65.0, intended_weight=0.04, anchor_mode="DUAL",
             thesis_invalidation="t")
    book.buy("B", Classification.CORE_GROWTH, 2_000.0, 20.0, date(2026, 10, 4),
             conviction=70.0, intended_weight=0.02, anchor_mode="SINGLE (C1 only)",
             thesis_invalidation="t")
    book.mark({"A": 44.0, "B": 18.0})
    book.sell("B", 18.0, date(2026, 11, 1), "Module A outright FAIL")
    book.snapshot(date(2026, 11, 1), {"A": 44.0}, 5_000.0)
    return book


class TestTheBookFile:
    def test_a_book_round_trips_exactly(self, tmp_path):
        state = paper.PaperState(
            started_on=date(2026, 10, 4), symbols=["A", "B", "C"], book=a_book(),
            last_rebalance=date(2026, 11, 1), ballast_level=5_000.0,
            log=[{"date": "2026-11-01", "evaluated": 3, "buy_signals": 0,
                  "buys": [], "sells": [["B", "x"]]}],
        )
        paper.save_state(state, tmp_path / "book.json")
        again = paper.load_state(tmp_path / "book.json")

        assert again.to_json() == state.to_json()
        assert again.book.positions["A"].classification is Classification.CORE_STABLE
        assert again.book.positions["A"].opened_on == date(2026, 10, 4)
        assert again.book.closed[0].total_return == pytest.approx(-0.10)
        assert again.book.total_value({"A": 44.0}) == pytest.approx(
            state.book.total_value({"A": 44.0})
        )

    def test_no_file_means_paper_trading_has_not_started(self, tmp_path):
        assert paper.load_state(tmp_path / "missing.json") is None


class TestCadence:
    def test_rebalance_once_per_calendar_month(self):
        state = paper.PaperState(date(2026, 10, 4), [], BacktestBook(cash=1.0),
                                 last_rebalance=date(2026, 10, 4))
        assert not state.due(date(2026, 10, 25))
        assert state.due(date(2026, 11, 1))
        assert paper.PaperState(date(2026, 10, 4), [], BacktestBook(cash=1.0)).due(
            date(2026, 10, 4)
        )


class TestWeeklyRuns:
    def run(self, tmp_path, day, *extra):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import run_paper

        return run_paper.main([
            "--source", "synthetic", "--book", str(tmp_path / "book.json"),
            "--out", str(tmp_path / "report.txt"), "--today", day, *extra,
        ])

    def test_the_book_carries_from_week_to_week(self, tmp_path):
        self.run(tmp_path, "2024-03-03")
        first = paper.load_state(tmp_path / "book.json")
        self.run(tmp_path, "2024-03-10")
        second = paper.load_state(tmp_path / "book.json")

        assert first.last_rebalance == second.last_rebalance == date(2024, 3, 3)
        assert len(second.log) == 1, "a second run in the month only marks to market"
        assert len(second.book.snapshots) == len(first.book.snapshots) + 1
        assert set(second.book.positions) == set(first.book.positions)
        assert "PAPER TRADING" in (tmp_path / "report.txt").read_text()

    def test_a_rebalance_cut_short_leaves_the_book_unchanged(self, tmp_path, monkeypatch):
        from gcfp.backtest.timebudget import TimeBudgetExceeded

        self.run(tmp_path, "2024-03-03")
        before = paper.load_state(tmp_path / "book.json")

        def stalls(*args, **kwargs):
            before_trade = args[1]
            before_trade.book.cash = -1.0   # a half-done month
            raise TimeBudgetExceeded("stalled")

        monkeypatch.setattr(paper, "rebalance", stalls)
        self.run(tmp_path, "2024-04-07")
        after = paper.load_state(tmp_path / "book.json")

        assert after.last_rebalance == before.last_rebalance
        assert after.book.cash >= 0
        assert after.book.positions.keys() == before.book.positions.keys()
        assert "did not finish" in (tmp_path / "report.txt").read_text()


class TestStageTwoStatus:
    def test_three_clean_rebalances_pass(self):
        from gcfp.backtest.portfolio import Snapshot

        state = paper.PaperState(date(2026, 10, 11), [], BacktestBook(cash=100.0))
        state.log = [{"date": f"2026-{m}-01", "crashes": {}} for m in (10, 11, 12)]
        state.book.snapshots = [
            Snapshot(date(2026, 10, 11), 100.0, 100.0, 0.0, 0, 100.0, 0.0, 0.0, 5000.0),
            Snapshot(date(2027, 1, 3), 104.0, 104.0, 0.0, 0, 104.0, 0.0, 0.0, 5100.0),
        ]
        assert "PASSED" in "\n".join(paper.stage_two_status(state))

    def test_falling_far_behind_triggers_a_review(self):
        from gcfp.backtest.portfolio import Snapshot

        state = paper.PaperState(date(2026, 10, 11), [], BacktestBook(cash=100.0))
        state.log = [{"crashes": {}}] * 3
        state.book.snapshots = [
            Snapshot(date(2026, 10, 11), 100.0, 100.0, 0.0, 0, 100.0, 0.0, 0.0, 5000.0),
            Snapshot(date(2027, 1, 3), 80.0, 80.0, 0.0, 0, 80.0, 0.0, 0.0, 5000.0),
        ]
        assert "REVIEW" in "\n".join(paper.stage_two_status(state))
