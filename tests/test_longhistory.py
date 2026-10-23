"""The long-history premise check, against files shaped like French's."""

from __future__ import annotations

import io
import zipfile

import pytest

from gcfp.backtest import longhistory as lh

FACTORS = """This file was created by CMPT_ME_BEME_RETS using the 202508 CRSP database.
The 1-month TBill return is from Ibbotson and Associates, Inc.

,Mkt-RF,SMB,HML,RF
196307,   -0.39,   -0.41,   -0.97,    0.27
196308,    5.07,   -0.80,    1.80,    0.25

 Annual Factors: January-December
,Mkt-RF,SMB,HML,RF
  1964,   12.00,    1.00,    2.00,    3.00
"""

SIX = """This file was created by CMPT_ME_EP_RETS using the 202508 CRSP database.

  Average Value Weighted Returns -- Monthly
,SMALL LoEP,ME1 EP2,SMALL HiEP,BIG LoEP,ME2 EP2,BIG HiEP
196307,   1.00,   1.00,   2.00,  -0.50,   0.50,   3.00
196308,   1.00,   1.00, -99.99,   0.50,   0.50,   4.00

  Average Equal Weighted Returns -- Monthly
,SMALL LoEP,ME1 EP2,SMALL HiEP,BIG LoEP,ME2 EP2,BIG HiEP
196307,  50.00,  50.00,  50.00,  50.00,  50.00,  50.00
"""


class TestParsing:
    def test_only_the_first_monthly_table_is_read(self):
        table = lh.parse_french_csv(SIX)
        assert table["BIG HiEP"] == {196307: 0.03, 196308: 0.04}
        assert 196308 not in table["SMALL HiEP"], "-99.99 is missing, not a return"

    def test_the_market_return_includes_the_risk_free_rate(self):
        tables = {"market": lh.parse_french_csv(FACTORS)}
        market, _ = lh.build_proxies(tables)
        assert market[196308] == pytest.approx(0.0507 + 0.0025)
        assert 1964 not in market, "annual rows are not months"

    def test_the_csv_is_read_out_of_the_zip_and_cached(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("6_Portfolios_ME_EP_2x3.CSV", SIX)
        calls = []

        def get(url):
            calls.append(url)
            return buf.getvalue()

        first = lh.fetch_french("6_Portfolios_ME_EP_2x3", get, tmp_path)
        second = lh.fetch_french("6_Portfolios_ME_EP_2x3", get, tmp_path)
        assert first == second and len(calls) == 1
        assert calls[0].endswith("/6_Portfolios_ME_EP_2x3_CSV.zip")

    def test_columns_are_picked_by_size_and_side(self):
        table = lh.parse_french_csv(SIX)
        assert lh.pick(table, size="BIG", high=True, tag="ep") == table["BIG HiEP"]
        assert lh.pick(table, size="SMALL", high=False, tag="ep") == table["SMALL LoEP"]


class TestVerdict:
    @staticmethod
    def series(monthly, start=196301, end=202512):
        out, y, m = {}, start // 100, start % 100
        while y * 100 + m <= end:
            out[y * 100 + m] = monthly
            m += 1
            if m == 13:
                y, m = y + 1, 1
        return out

    def test_a_steady_edge_supports_the_premise(self):
        market = self.series(0.008)
        proxy = lh.Proxy("Proxy A, big companies", self.series(0.010))
        lines, verdict = lh.report(market, [proxy])
        assert verdict is True
        assert any("2 of 3 decades [PASS: 3 of 3]" in l for l in lines)

    def test_falling_behind_over_30_years_does_not(self):
        market = self.series(0.010)
        proxy = lh.Proxy("Proxy A, big companies", self.series(0.009))
        _, verdict = lh.report(market, [proxy])
        assert verdict is False

    def test_costs_are_taken_off(self):
        out = lh.net_of_costs({200001: 0.0}, 0.012)
        assert out[200001] == pytest.approx((1 - 0.012) ** (1 / 12) - 1)

    def test_a_missing_market_file_is_reported_not_raised(self, tmp_path):
        def get(url):
            raise ConnectionError("blocked")

        text, verdict = lh.run(get, tmp_path)
        assert verdict is None and "not run" in text
