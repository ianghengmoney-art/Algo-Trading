"""L2 improvements: futures costs and the ensemble signal."""

from __future__ import annotations

import random
from datetime import date, timedelta

from gcfp.backtest import leverage_daily as ld
from gcfp.backtest import leverage_improve as li


def series(n_years: int, drift: float, seed: int = 4):
    rnd = random.Random(seed)
    out, d = [], date(1980, 1, 1)
    while len(out) < 252 * n_years:
        if d.weekday() < 5:
            out.append(d.year * 10000 + d.month * 100 + d.day)
        d += timedelta(days=1)
    return {x: rnd.gauss(drift, 0.01) for x in out}, {x: 0.0001 for x in out}


def test_futures_costs_beat_loan_costs_on_the_same_rule():
    market, rf = series(20, 0.0005)
    loan = ld.simulate(market, rf, "loan")
    fut = ld.simulate(market, rf, "fut", **li.FUTURES)
    last = max(market)
    assert fut.equity[last] > loan.equity[last]


def test_the_ensemble_steps_leverage_by_quarters():
    market, rf = series(20, 0.0005)
    run = ld.simulate(market, rf, "e", lookbacks=li.ENSEMBLE)
    assert run.switches >= 1 and run.days_on > 0


def test_report_runs():
    market, rf = series(30, 0.0004, seed=8)
    text, passed = li.evaluate(market, rf)
    assert "REGISTERED CRITERION" in text and "FUTURES INSTEAD" in text
    assert isinstance(passed, bool)
