"""Paper trading of MA, and its margin alarm."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from gcfp.backtest import multi_asset_paper as mp


def daily(drift: float, start=date(2025, 1, 1), days=420, level=100.0):
    out, d = [], start
    for _ in range(days):
        if d.weekday() < 5:
            out.append((d, level))
            level *= 1 + drift
        d += timedelta(days=1)
    return out


def market(drifts=(0.001, 0.001, -0.0005, 0.001)):
    return {name: daily(dr) for (name, _), dr in zip(mp.ASSETS, drifts)}


class TestStep:
    def test_sleeves_follow_their_own_trends(self):
        closes = market()
        today = closes["US stocks"][-1][0]
        state, changes = mp.step(None, closes, 0.04, today, 3.82, 100_000)
        assert state.in_trend == {"US stocks": True, "Intl stocks": True,
                                  "Treasuries": False, "Gold": True}
        assert len(changes) == 4
        # Three sleeves in at 3.82/4 each: 2.865x gross.
        assert sum(state.exposure.values()) / state.value == pytest.approx(3 * 3.82 / 4)

    def test_a_second_run_the_same_month_changes_nothing(self):
        closes = market()
        today = closes["US stocks"][-1][0]
        state, _ = mp.step(None, closes, 0.04, today, 3.82, 100_000)
        state, changes = mp.step(state, closes, 0.04, today, 3.82, 100_000)
        assert changes == [] and len(state.log) == 1

    def test_equity_moves_with_the_sleeves(self):
        closes = market((0.001, 0.001, 0.001, 0.001))
        today = closes["US stocks"][-1][0]
        state, _ = mp.step(None, closes, 0.0, today, 3.82, 100_000)
        day, prices = mp.latest(closes)
        up = {n: p * 1.10 for n, p in prices.items()}
        # Every sleeve +10% at 3.82x gross: equity +38.2%, less tiny costs.
        assert mp.equity_now(state, up, 0.0, day) == pytest.approx(
            state.value * 1.382, rel=1e-3)


class TestMargin:
    def test_grades_and_remedies(self):
        closes = market((0.001, 0.001, 0.001, 0.001))
        today = closes["US stocks"][-1][0]
        state, _ = mp.step(None, closes, 0.0, today, 3.82, 100_000)
        day, prices = mp.latest(closes)
        assert mp.margin(state, prices, 0.0, day, 0.08).level == mp.OK
        # Everything -12%: equity 1 - 3.82 x 0.12 = 54% of start; exposure 88%.
        down = {n: p * 0.88 for n, p in prices.items()}
        m = mp.margin(state, down, 0.0, day, 0.08)
        assert m.ratio == pytest.approx(0.5416 / (3.82 * 0.88), rel=1e-2)
        assert m.level in ("WARNING", "URGENT")
        assert m.inject_per_10k > 0 and m.sell_per_10k > 0

    def test_dividend_rescaling_is_read_on_todays_scale(self):
        closes = market((0.0005, 0.0005, 0.0005, 0.0005))
        today = closes["US stocks"][-1][0]
        state, _ = mp.step(None, closes, 0.0, today, 3.82, 100_000)
        # A 2% payout: Yahoo rescales every earlier adjusted close down by 2%.
        rescaled = {n: [(d, c * 0.98) for d, c in s] for n, s in closes.items()}
        later = today + timedelta(days=3)
        for n in rescaled:
            rescaled[n].append((later, closes[n][-1][1]))
        state, _ = mp.step(state, rescaled, 0.0, later, 3.82, 100_000)
        day, prices = mp.latest(rescaled)
        # The payout counts: each in-trend sleeve is up about 2%.
        assert mp.equity_now(state, prices, 0.0, day) > state.value * 1.05


def test_report_and_alert():
    closes = market()
    today = closes["US stocks"][-1][0]
    state, changes = mp.step(None, closes, 0.04, today, 3.82, 100_000)
    text, alert = mp.report(state, closes, 0.04, today, 0.08, changes)
    assert alert.splitlines()[0] == mp.OK
    assert "POSITION CHANGE" in alert and "Treasuries: stay out (T-bills)" in alert
    assert "MULTI-ASSET TREND" in text


def test_trade_ticket_for_a_small_account():
    in_trend = {"US stocks": True, "Intl stocks": True, "Treasuries": False, "Gold": False}
    prices = {"UPRO": 100.0, "EFO": 50.0, "TYD": 40.0, "UGL": 80.0}
    text = mp.trade_ticket(in_trend, 4000, "SGD", 1.29, prices)
    usd = 4000 / 1.29
    assert f"{usd * 0.955 / 3:,.0f}" in text  # UPRO dollars
    assert "SGOV" in text and "1.91x" in text
