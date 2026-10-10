"""MA, the multi-asset trend portfolio."""

from __future__ import annotations

import json
import random

import pytest

from gcfp.backtest import multi_asset as ma


def months(n: int, start: int = 198001) -> list[int]:
    out, m = [], start
    for _ in range(n):
        out.append(m)
        m = ma._next_month(m)
    return out


class TestBonds:
    def test_an_unchanged_yield_earns_about_a_month_of_coupon(self):
        assert ma.bond_return(0.04, 0.04) == pytest.approx(0.04 / 12, rel=0.02)

    def test_rising_yields_lose_about_duration_times_the_rise(self):
        r = ma.bond_return(0.04, 0.05)
        assert -0.085 < r < -0.07  # 10-year duration about 8

    def test_reads_freds_csv_with_holidays(self):
        text = ("observation_date,DGS10\n2024-01-30,4.00\n2024-01-31,4.00\n"
                "2024-02-29,.\n2024-02-28,4.00\n2024-03-29,5.00\n")
        r = ma.treasury_returns(text)
        assert set(r) == {202402, 202403}
        assert r[202402] == pytest.approx(0.04 / 12, rel=0.02)
        assert r[202403] < -0.07


class TestGold:
    def test_datahub_extended_by_yahoo(self):
        datahub = "Date,Price\n2024-01,2000\n2024-02,2100\n"
        stamps = [1704067200, 1706745600, 1709251200]  # Jan, Feb, Mar 2024
        yahoo = {"chart": {"result": [{"timestamp": stamps,
                                        "indicators": {"quote": [{"close": [1000, 1050, 1155]}]}}]}}

        def get(url):
            return datahub.encode() if "datahub" in url else json.dumps(yahoo).encode()

        r, note = ma.gold_returns(get)
        assert r[202402] == pytest.approx(0.05)
        assert r[202403] == pytest.approx(0.10)
        assert "Yahoo" in note


class TestPortfolio:
    def test_one_sleeve_at_1x_matches_the_trend_rule(self):
        ms = months(60)
        asset = {m: 0.02 for m in ms}
        rf = {m: 0.001 for m in ms}
        r = ma.portfolio({"A": asset}, rf, 1.0, ms[10:])
        assert r[ms[20]] == pytest.approx(0.02 - ma.RUNNING / 12)

    def test_sleeves_out_of_trend_earn_t_bills(self):
        ms = months(60)
        up = {m: 0.02 for m in ms}
        down = {m: -0.02 for m in ms}
        rf = {m: 0.001 for m in ms}
        r = ma.portfolio({"Up": up, "Down": down}, rf, 2.0, ms[10:])
        # Half the book in the riser at 2x/2 = 1x, the other half in T-bills.
        assert r[ms[30]] == pytest.approx(0.001 + 1.0 * (0.02 - 0.001) - ma.RUNNING / 12)

    def test_report_runs_and_matches_volatility(self):
        rnd = random.Random(3)
        ms = months(420, 196001)
        rf = {m: 0.003 for m in ms}
        assets = {
            "US stocks": {m: rnd.gauss(0.008, 0.045) for m in ms},
            "Intl stocks": {m: rnd.gauss(0.007, 0.05) for m in ms if m >= 199007},
            "Treasuries": {m: rnd.gauss(0.004, 0.02) for m in ms},
            "Gold": {m: rnd.gauss(0.004, 0.045) for m in ms if m >= 200009},
        }
        text, passed = ma.evaluate(assets, rf, ["synthetic"])
        assert "REGISTERED CRITERION" in text and "CORRELATION" in text
        assert isinstance(passed, bool)


def test_treasuries_fall_back_to_yahoo_when_fred_fails():
    stamps = [1704067200, 1706745600, 1709251200]  # Jan, Feb, Mar 2024
    tnx = {"chart": {"result": [{"timestamp": stamps,
                                  "indicators": {"quote": [{"close": [4.0, 4.0, 5.0]}]}}]}}

    def get(url):
        if "fred" in url:
            raise TimeoutError("slow")
        return json.dumps(tnx).encode()

    r, source = ma.load_treasuries(get)
    assert "Yahoo" in source and set(r) == {202402, 202403}
    assert r[202402] == pytest.approx(0.04 / 12, rel=0.02)
