"""Paper trading of the leveraged trend strategy, and its margin alarm."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from gcfp.backtest import leverage_paper as lp


def daily(start: date, days: int, level: float, step: float) -> list[tuple[date, float]]:
    out, d = [], start
    for _ in range(days):
        if d.weekday() < 5:
            out.append((d, level))
            level *= 1 + step
        d += timedelta(days=1)
    return out


class TestMarginMath:
    def test_the_call_comes_after_a_third_at_2x_and_25_percent(self):
        assert lp.call_move(2.0, 0.25) == pytest.approx(-1 / 3)
        assert lp.call_move(2.0, 0.07) == pytest.approx((0.14 - 1) / (2 * 0.93))

    def state(self) -> lp.LeverageState:
        return lp.LeverageState(started_on=date(2026, 1, 2), leverage=2.0, value=100_000,
                                level_at_rebalance=1000.0, rebalanced_on=date(2026, 1, 2),
                                risk_on=True, signal_month="2025-12", start_level=1000.0)

    def test_grades_and_remedies(self):
        s = self.state()
        assert lp.margin(s, 1050.0, 0.25).level == lp.OK
        m = lp.margin(s, 800.0, 0.25)  # -20%: 60% of the way to -33%
        assert m.level == "WARNING" and m.used == pytest.approx(0.6)
        # Adding $2,000 or selling $4,000 per $10k restores 2x.
        assert m.inject_per_10k == pytest.approx(2_000)
        assert m.sell_per_10k == pytest.approx(4_000)
        assert lp.margin(s, 740.0, 0.25).level == "URGENT"
        assert lp.margin(s, 660.0, 0.25).level == "MARGIN CALL LEVEL"

    def test_no_margin_risk_in_t_bills(self):
        s = self.state()
        s.risk_on = False
        assert lp.margin(s, 500.0, 0.25) is None


class TestStep:
    def test_a_rising_market_goes_risk_on_and_says_so_once(self):
        closes = daily(date(2025, 1, 1), 400, 1000.0, 0.001)
        today = closes[-1][0]
        state, instruction = lp.step(None, closes, 0.04, today, 2.0, 100_000)
        assert state.risk_on and instruction == "HOLD 2.0x THE S&P 500"
        again, instruction = lp.step(state, closes, 0.04, today, 2.0, 100_000)
        assert instruction is None and len(again.marks) == 1

    def test_a_falling_market_goes_to_t_bills(self):
        closes = daily(date(2025, 1, 1), 400, 1000.0, -0.001)
        state, instruction = lp.step(None, closes, 0.04, closes[-1][0], 2.0, 100_000)
        assert not state.risk_on and instruction == "HOLD T-BILLS (no stocks)"

    def test_the_current_month_never_sets_the_signal(self):
        closes = daily(date(2025, 1, 1), 400, 1000.0, 0.001)
        today = closes[-1][0]
        months = lp.month_end_closes(closes, today)
        assert months[-1][0] != f"{today.year}-{today.month:02d}"

    def test_the_alert_file_leads_with_the_level(self, tmp_path):
        closes = daily(date(2025, 1, 1), 400, 1000.0, 0.001)
        today = closes[-1][0]
        state, instruction = lp.step(None, closes, 0.04, today, 2.0, 100_000)
        text, alert = lp.report(state, closes, 0.04, today, 0.25, instruction)
        assert alert.splitlines()[0] == lp.OK
        assert "POSITION CHANGE: HOLD 2.0x THE S&P 500" in alert
        # A 25% fall after the rebalance is past half way to the call.
        crashed = closes + [(today + timedelta(days=1), closes[-1][1] * 0.75)]
        text, alert = lp.report(state, crashed, 0.04, today, 0.25, None)
        assert alert.splitlines()[0] == "URGENT"
        assert "add $" in text and "sell $" in text

    def test_the_book_round_trips(self, tmp_path):
        closes = daily(date(2025, 1, 1), 400, 1000.0, 0.001)
        state, _ = lp.step(None, closes, 0.04, closes[-1][0], 2.0, 100_000)
        lp.save(state, tmp_path / "b.json")
        assert lp.load(tmp_path / "b.json").to_json() == state.to_json()


class TestDailyRuns:
    def test_the_full_screen_runs_weekly_and_on_the_schedule(self, tmp_path, monkeypatch):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import run_screen

        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
        today = date(2026, 10, 14)
        assert run_screen.full_screen_due(tmp_path, today)  # never ran
        (tmp_path / "2026-10-11-new-passers.txt").write_text("x")
        assert not run_screen.full_screen_due(tmp_path, today)  # 3 days ago
        assert run_screen.full_screen_due(tmp_path, date(2026, 10, 18))  # a week on
        monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
        assert run_screen.full_screen_due(tmp_path, today)
