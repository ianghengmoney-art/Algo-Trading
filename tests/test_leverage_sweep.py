"""The Kelly leverage calculation."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from gcfp.backtest import leverage_sweep as ls
from gcfp.backtest.leverage import BORROW_SPREAD


def days(n: int) -> list[int]:
    out, d = [], date(1990, 1, 1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.year * 10000 + d.month * 100 + d.day)
        d += timedelta(days=1)
    return out


def test_kelly_is_mean_over_variance():
    rnd = random.Random(1)
    ds = days(252 * 40)
    rf = {d: 0.0 for d in ds}
    m, s = 0.0004, 0.01
    market = {d: rnd.gauss(m, s) + BORROW_SPREAD / 252 for d in ds}
    l_star, growth, ex, vol = ls.kelly(market, rf, ds)
    assert l_star == pytest.approx(m / s ** 2, rel=0.25)  # sampling noise
    assert vol == pytest.approx(s * 252 ** 0.5, rel=0.05)


def test_report_runs_and_finds_a_best_leverage():
    rnd = random.Random(2)
    ds = days(252 * 25)
    market = {d: rnd.gauss(0.0004, 0.01) for d in ds}
    rf = {d: 0.0001 for d in ds}
    text = ls.evaluate(market, rf)
    assert "Kelly L*" in text and "best CAGR" in text
    assert "4.00x" in text
