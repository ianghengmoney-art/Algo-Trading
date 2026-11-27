"""MA's daily crash check."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from gcfp.backtest import multi_asset_daily as md


def cal(start: date, n: int) -> list[int]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(md._key(d))
        d += timedelta(days=1)
    return out


def test_align_compounds_days_the_calendar_lacks():
    asset = {20200102: 0.01, 20200104: 0.02, 20200106: 0.03}
    out = md.align(asset, [20200102, 20200106])
    assert out[20200102] == pytest.approx(0.01)
    assert out[20200106] == pytest.approx(1.02 * 1.03 - 1)


def test_one_sleeve_at_1x_tracks_the_asset_when_in_trend():
    days = cal(date(2000, 1, 3), 252 * 3)
    asset = {d: 0.001 for d in days}
    rf = {d: 0.0 for d in days}
    out = md.simulate({"A": asset}, rf, 1.0, 0)
    eq = out["equity"]
    ds = sorted(eq)
    # Once in, equity grows about 0.1% a day (less tiny costs).
    assert eq[ds[-1]] / eq[ds[-21]] == pytest.approx(1.001 ** 20, rel=1e-3)


def test_a_fast_crash_shows_on_daily_marks_and_margin():
    rnd = random.Random(2)
    days = cal(date(2000, 1, 3), 252 * 3)
    crash = days[600:610]
    asset = {d: (-0.08 if d in crash else rnd.gauss(0.0008, 0.005)) for d in days}
    rf = {d: 0.0001 for d in days}
    out = md.simulate({"A": asset}, rf, 3.82, 0)
    assert out["min_ratio"] < 0.08 and out["calls"], "3.82x in one market must breach 8%"
    s = md.stats(out["equity"], min(out["equity"]), max(out["equity"]))
    assert s["worst_day"] < -0.25


def test_report_runs():
    rnd = random.Random(4)
    days = cal(date(1999, 1, 4), 252 * 6)
    rf = {d: 0.0001 for d in days}
    assets = {n: {d: rnd.gauss(0.0004, 0.01) for d in days} for n in
              ("US stocks", "Intl stocks", "Treasuries", "Gold")}
    text, ok = md.evaluate(assets, rf, ["synthetic"])
    assert "MA DAILY CRASH CHECK" in text and "CRASH WINDOWS" in text
    assert isinstance(ok, bool)
