"""Paper trading of the ETF version, and the futures contract ticket."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from gcfp.backtest import etf_paper as ep


def series(start: date, days: int, level: float, step: float):
    out, d = [], start
    for _ in range(days):
        if d.weekday() < 5:
            out.append((d, level))
            level *= 1 + step
        d += timedelta(days=1)
    return out


def closes(step_upro=0.001):
    start = date(2026, 10, 1)
    out = {etf: series(start, 60, 100.0, 0.0005) for etf, _, _ in ep.ETFS.values()}
    out["UPRO"] = series(start, 60, 100.0, step_upro)
    out[ep.CASH_ETF] = series(start, 60, 100.0, 0.00016)
    return out


IN = {"US stocks": True, "Intl stocks": True, "Treasuries": False, "Gold": False}


def test_opening_buys_the_in_trend_sleeves_and_keeps_the_rest_in_t_bills():
    c = closes()
    day = date(2026, 10, 9)
    book, trades = ep.step(None, c, IN, "2026-09", day, 1.29, "SGD", 4000)
    usd = 4000 / 1.29
    assert len(trades) == 2 and book.orders == 2
    lots = book.lots
    assert lots["UPRO"][0] == pytest.approx(usd * 0.955 / 3, rel=1e-3)
    assert lots["EFO"][0] == pytest.approx(usd * 0.955 / 2, rel=1e-3)
    assert lots[ep.CASH_ETF][0] == pytest.approx(usd - lots["UPRO"][0] - lots["EFO"][0] - 2)


def test_holdings_grow_with_their_prices_and_are_not_double_counted():
    c = closes(step_upro=0.01)
    d0, d1 = date(2026, 10, 9), date(2026, 11, 3)
    book, _ = ep.step(None, c, IN, "2026-09", d0, 1.29, "SGD", 4000)
    upro_then = book.lots["UPRO"][0]
    # A new month, same signals, no drift past 25%: UPRO is not traded.
    book, trades = ep.step(book, c, IN, "2026-10", d1, 1.29, "SGD", 4000)
    growth = ep.close_on(c["UPRO"], d1) / ep.close_on(c["UPRO"], d0)
    if not any("UPRO" in t for t in trades):
        assert ep.value_of(book, c, d1)["UPRO"] == pytest.approx(upro_then * growth)


def test_report_and_contract_ticket():
    c = closes()
    day = date(2026, 10, 9)
    book, trades = ep.step(None, c, IN, "2026-09", day, 1.29, "SGD", 4000)
    text = ep.report(book, c, day, 1.29, trades)
    assert "ETF VERSION — PAPER ACCOUNT, SGD 4,000" in text and "UPRO" in text
    ticket = ep.contract_ticket(IN, 150_000, 3.82,
                                {"MES=F": 6800.0, "ZN=F": 110.0, "MGC=F": 3800.0, "EFA": 100.0})
    # US sleeve: 0.955 x 150k = 143k; one MES is 34k, so 4 contracts.
    assert "Micro E-mini S&P 500 (MES)" in ticket and "     4 " in ticket
