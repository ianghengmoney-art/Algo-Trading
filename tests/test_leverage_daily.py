"""Crash protection on daily data (docs/LEVERAGE_STRATEGY.md)."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from gcfp.backtest import leverage_daily as ld


def trading_days(start: date, n: int) -> list[int]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.year * 10000 + d.month * 100 + d.day)
        d += timedelta(days=1)
    return out


def market_with_crash(n=252 * 4, crash_at=None, seed=3):
    rnd = random.Random(seed)
    days = trading_days(date(1990, 1, 1), n)
    market = {d: rnd.gauss(0.0006, 0.008) for d in days}
    if crash_at is not None:
        for k in range(10):  # a 10-day crash, about -40% in all
            market[days[crash_at + k]] = -0.05
    return market, {d: 0.0001 for d in days}, days


class TestParse:
    def test_reads_the_daily_table_only(self):
        text = "preamble\n\n,Mkt-RF,SMB,HML,RF\n19260701,0.10,-0.25,-0.27,0.009\n" \
               "19260702,0.45,-0.33,-0.06,0.009\n\nAnnual\n,Mkt-RF\n1927,10\n"
        table = ld.parse_french_daily(text)
        assert table["Mkt-RF"] == pytest.approx({19260701: 0.001, 19260702: 0.0045})
        assert table["RF"][19260702] == pytest.approx(0.00009)


class TestSimulate:
    def test_buy_and_hold_1x_is_the_market(self):
        market, rf, days = market_with_crash()
        run = ld.simulate(market, rf, "bh", trend=False, max_leverage=1.0, costs=False)
        expected = 1.0
        for d in days:
            expected *= 1 + market[d]
        assert run.equity[days[-1]] == pytest.approx(expected, rel=1e-9)

    def test_2x_buy_and_hold_doubles_a_small_day(self):
        days = trading_days(date(1990, 1, 1), 3)
        market = {days[0]: 0.0, days[1]: 0.01, days[2]: 0.0}
        rf = {d: 0.0 for d in days}
        run = ld.simulate(market, rf, "2x", trend=False, max_leverage=2.0, costs=False)
        # Exposure 2 rises 1%; the loan of 1 has paid two days' spread.
        expected = 2 * 1.01 - (1 + ld.BORROW_SPREAD / 252) ** 2
        assert run.equity[days[1]] == pytest.approx(expected, rel=1e-9)

    def test_a_deep_fast_crash_triggers_a_margin_call_at_2x(self):
        market, rf, days = market_with_crash(crash_at=500)
        run = ld.simulate(market, rf, "2x", trend=False, max_leverage=2.0)
        assert run.margin_calls, "a 40% fall at 2x must breach 25% maintenance"

    def test_the_stop_gets_out_during_the_crash(self):
        market, rf, days = market_with_crash(crash_at=500)
        plain = ld.simulate(market, rf, "L2")
        stopped = ld.simulate(market, rf, "L2-VS", vol_scaled=True, stop=True)
        crash = (days[499], days[515])
        assert stopped.stops >= 1
        loss_plain = plain.equity[crash[1]] / plain.equity[crash[0]] - 1
        loss_stopped = stopped.equity[crash[1]] / stopped.equity[crash[0]] - 1
        assert loss_stopped > loss_plain

    def test_volatility_scaling_cuts_leverage_in_rough_markets(self):
        market, rf, days = market_with_crash(crash_at=500)
        run = ld.simulate(market, rf, "L2-V", vol_scaled=True)
        assert run.min_ratio > ld.simulate(market, rf, "L2").min_ratio


class TestReport:
    def test_runs_end_to_end(self):
        rnd = random.Random(9)
        days = trading_days(date(1985, 1, 1), 252 * 30)
        market = {d: rnd.gauss(0.0004, 0.011) for d in days}
        rf = {d: 0.00012 for d in days}
        text, verdicts = ld.evaluate(market, rf)
        assert set(verdicts) == {"L2", "L2-V", "L2-VS"}
        assert "THE CRASHES, ONE BY ONE" in text and "1987 Black Monday" in text
        assert "SELECTION" in text
