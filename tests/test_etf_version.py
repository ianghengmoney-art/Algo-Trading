"""MA through leveraged ETFs, for a small account."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from gcfp.backtest import etf_version as ev


def test_two_stock_sleeves_fit_and_four_are_scaled_down():
    two = ev.targets({"US stocks": True, "Intl stocks": True, "Treasuries": False,
                      "Gold": False}, 1000.0)
    # 0.955x via UPRO (3x) and EFO (2x): 318 + 478 dollars, the rest in T-bills.
    assert two["US stocks"] == pytest.approx(1000 * 0.955 / 3, rel=1e-3)
    assert two["Intl stocks"] == pytest.approx(1000 * 0.955 / 2, rel=1e-3)
    four = ev.targets({a: True for a in ev.ETFS}, 1000.0)
    assert sum(four.values()) == pytest.approx(1000.0)
    gross = sum(four[a] * ev.ETFS[a][1] for a in ev.ETFS) / 1000
    assert gross == pytest.approx(3.82 / 1.59, rel=0.01)


def cal(n: int) -> list[int]:
    out, d = [], date(1999, 1, 4)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.year * 10000 + d.month * 100 + d.day)
        d += timedelta(days=1)
    return out


def test_simulation_and_report():
    rnd = random.Random(6)
    days = cal(252 * 6)
    rf = {d: 0.0001 for d in days}
    assets = {a: {d: rnd.gauss(0.0004, 0.01) for d in days} for a in ev.ETFS}
    out = ev.simulate(assets, rf, 0)
    assert out["orders"] > 0 and out["commissions"] == out["orders"] * ev.ORDER_COST
    text = ev.evaluate(assets, rf, ["synthetic"])
    assert "MA AS LEVERAGED ETFs" in text and "orders:" in text
