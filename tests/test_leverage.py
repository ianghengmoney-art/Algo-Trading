"""The leveraged trend strategy (docs/LEVERAGE_STRATEGY.md)."""

from __future__ import annotations

import pytest

from gcfp.backtest import leverage


def months(n: int, start: int = 199001) -> list[int]:
    out, y, m = [], start // 100, start % 100
    for _ in range(n):
        out.append(y * 100 + m)
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


class TestTrend:
    def test_risk_on_in_a_steady_rise_and_off_in_a_steady_fall(self):
        ms = months(40)
        market = {m: (0.02 if i < 20 else -0.03) for i, m in enumerate(ms)}
        rf = {m: 0.001 for m in ms}
        path = leverage.trend(market, rf, 1.0, "t")
        first = min(path.returns)
        assert first == ms[10]  # the signal needs 10 month-ends
        assert path.risk_on[ms[15]] is True
        assert path.risk_on[ms[39]] is False
        # Risk-off months earn T-bills.
        assert path.returns[ms[39]] == pytest.approx(0.001)

    def test_the_signal_uses_only_past_closes(self):
        ms = months(30)
        market = {m: 0.01 for m in ms}
        rf = {m: 0.0 for m in ms}
        before = leverage.trend(market, rf, 1.0, "a").risk_on
        market[ms[20]] = -0.5  # a crash in month 20 cannot change month 20's position
        after = leverage.trend(market, rf, 1.0, "b").risk_on
        assert before[ms[20]] == after[ms[20]]

    def test_leverage_pays_for_its_borrowing_and_cannot_lose_more_than_all(self):
        assert leverage._levered(0.0, 0.0, 2.0) == pytest.approx(
            -(leverage.BORROW_SPREAD + leverage.RUNNING_COST) / 12)
        assert leverage._levered(-0.7, 0.0, 2.0) == -1.0

    def test_switches_are_charged(self):
        ms = months(40)
        market = {m: (0.02 if (i // 12) % 2 == 0 else -0.02) for i, m in enumerate(ms)}
        rf = {m: 0.0 for m in ms}
        path = leverage.trend(market, rf, 1.0, "t")
        assert path.switches >= 2


class TestReport:
    def test_runs_end_to_end_on_a_synthetic_century(self):
        import random

        rnd = random.Random(7)
        ms = months(1200, 192607)
        market = {m: rnd.gauss(0.008, 0.05) for m in ms}
        rf = {m: 0.003 for m in ms}
        text, verdicts = leverage.evaluate(market, rf)
        assert set(verdicts) == {"L1", "L2"}
        assert "PRE-REGISTERED CRITERIA" in text and "SELECTION" in text
        assert "since 2008" in text


def test_the_margin_call_threshold():
    """2x with 25% maintenance: debt is half the assets, so equity hits 25%
    of assets after a one-third fall; 1.5x needs a fall of about 56%."""
    import random

    rnd = random.Random(1)
    ms = months(240, 199001)
    market = {m: rnd.gauss(0.01, 0.03) for m in ms}
    text, _ = leverage.evaluate(market, {m: 0.002 for m in ms})
    assert "a 33% market fall in one month" in text
    assert "a 56% market fall in one month" in text
