"""The point-in-time universe: dead companies in, and only while alive."""

from __future__ import annotations

from datetime import date
from unittest.mock import Mock

import pytest

from gcfp.data.adapter import DataUnavailable
from gcfp.data.pit_universe import (
    FilerIndex,
    load_filer_index,
    parse_xbrl_index,
    quarters_between,
    sample_symbols,
    survivorship_coverage,
    symbol_cik,
)
from gcfp.types import PricePoint

IDX = """Description:           XBRL Index of EDGAR Dissemination Feed
Last Data Received:    March 31, 2015

CIK|Company Name|Form Type|Date Filed|Filename
--------------------------------------------------------------------------------
18230|CATERPILLAR INC|10-K|2015-02-17|edgar/data/18230/0000018230-15-000011.txt
18230|CATERPILLAR INC|8-K|2015-03-01|edgar/data/18230/x.txt
726728|RADIOSHACK CORP|10-Q|2015-01-14|edgar/data/726728/y.txt
999|FOREIGN CO|20-F|2015-03-30|edgar/data/999/z.txt
"""


class TestIndexParsing:
    def test_only_periodic_reports_are_kept(self):
        rows = parse_xbrl_index(IDX)
        assert rows == {18230: ["2015-02-17"], 726728: ["2015-01-14"]}

    def test_quarters_span_year_boundaries(self):
        assert quarters_between(date(2014, 11, 1), date(2015, 4, 1)) == [
            (2014, 4), (2015, 1), (2015, 2),
        ]


def index() -> FilerIndex:
    # 1 files throughout; 2 dies in early 2015; 3 appears in 2016.
    return FilerIndex({
        1: [date(2014, 2, 1), date(2015, 2, 1), date(2016, 2, 1), date(2017, 2, 1)],
        2: [date(2014, 2, 1), date(2014, 11, 1), date(2015, 2, 1)],
        3: [date(2016, 5, 1), date(2017, 2, 1)],
    })


class TestLiveness:
    def test_a_company_that_later_dies_is_live_before_it_dies(self):
        assert index().is_live(2, date(2015, 6, 30))

    def test_and_gone_once_it_stops_filing(self):
        assert not index().is_live(2, date(2016, 6, 30))

    def test_a_company_is_not_live_before_its_first_filing(self):
        # No lookahead: the 2016 filing must not make it visible in 2015.
        assert not index().is_live(3, date(2015, 6, 30))
        assert index().is_live(3, date(2016, 6, 30))

    def test_dead_companies_are_in_the_sample_pool(self):
        pool = index().ever_live(date(2015, 1, 1), date(2017, 6, 30))
        assert pool == [1, 2, 3]
        assert index().stopped_filing(2, date(2017, 6, 30))
        assert not index().stopped_filing(1, date(2017, 6, 30))

    def test_sampling_is_seeded_and_addressed_by_cik(self):
        a = sample_symbols(index(), date(2015, 1, 1), date(2017, 6, 30), 2)
        b = sample_symbols(index(), date(2015, 1, 1), date(2017, 6, 30), 2)
        assert a == b and len(a) == 2
        assert all(symbol_cik(s) in (1, 2, 3) for s in a)


class TestLoading:
    def test_closed_quarters_are_cached(self, tmp_path):
        calls: list[str] = []

        def fetch(url: str) -> str:
            calls.append(url)
            return IDX

        args = (date(2015, 1, 1), date(2015, 3, 31))
        kw = dict(cache_dir=tmp_path, today=date(2026, 1, 1), lookback_days=0)
        first = load_filer_index(fetch, *args, **kw)
        second = load_filer_index(fetch, *args, **kw)
        assert len(calls) == 1
        assert first.filings == second.filings
        assert first.is_live(726728, date(2015, 1, 14))


class TestCoverage:
    def test_unpriced_dead_names_are_counted_and_flagged(self):
        symbols = ["CIK0000000001", "CIK0000000002", "CIK0000000003"]
        cov = survivorship_coverage(
            index(), symbols, date(2017, 6, 30),
            has_prices=lambda s: s != "CIK0000000002",
        )
        assert (cov.died, cov.died_priced, cov.survived) == (1, 0, 2)
        assert any("upper bound" in line for line in cov.lines())


class TestPriceTicker:
    def adapter(self, subs: dict):
        from gcfp.data.edgar import EdgarAdapter

        a = EdgarAdapter(user_agent="t t@example.com", session=Mock())
        a._ticker_map = {"CAT": 18230}
        a._submissions = lambda symbol: subs
        return a

    def test_a_listed_company_keeps_its_ticker(self):
        assert self.adapter({}).price_ticker("CIK0000018230") == ("CAT", None)

    def test_a_dead_company_is_found_by_its_filenames_with_a_cutoff(self):
        subs = {"tickers": [], "filings": {"recent": {
            "form": ["10-Q", "10-K", "8-K"],
            "filingDate": ["2014-12-10", "2014-06-10", "2015-02-01"],
            "primaryDocument": ["rsh-20141101.htm", "rsh-20140503.htm", "ex99.htm"],
        }}}
        ticker, cutoff = self.adapter(subs).price_ticker("CIK0000726728")
        assert ticker == "RSH"
        assert cutoff == date(2015, 4, 9)  # last periodic report + 120 days

    def test_an_index_symbol_is_priced_as_itself(self):
        # ^GSPC has no CIK; it must not be looked up in the SEC index.
        adapter = self.adapter({})
        adapter.ticker_to_cik = Mock(side_effect=DataUnavailable("cik", "no"))
        assert adapter.price_ticker("^GSPC") == ("^GSPC", None)
        adapter.ticker_to_cik.assert_not_called()

    def test_no_route_means_unpriceable(self):
        subs = {"tickers": [], "filings": {"recent": {
            "form": ["10-K"], "filingDate": ["2014-06-10"],
            "primaryDocument": ["form10k.htm"],
        }}}
        assert self.adapter(subs).price_ticker("CIK0000000777") is None


class TestCompositeMapping:
    def composite(self, resolved):
        from gcfp.data.composite import CompositeAdapter

        fundamentals = Mock()
        fundamentals.name = "edgar"
        fundamentals.price_ticker = lambda s: resolved
        prices = Mock()
        prices.name = "feed"
        prices.get_prices = Mock(return_value=(
            PricePoint(date(2015, 5, 1), 2.0),   # after the cutoff: someone else
            PricePoint(date(2015, 3, 1), 1.0),
        ))
        return CompositeAdapter(fundamentals=fundamentals, prices=prices), prices

    def test_prices_are_requested_by_ticker_and_cut_off(self):
        adapter, prices = self.composite(("RSH", date(2015, 4, 9)))
        out = adapter.get_prices("CIK0000726728", date(2015, 1, 1), date(2015, 12, 31))
        assert [p.close for p in out] == [1.0]
        assert prices.get_prices.call_args[0] == ("RSH", date(2015, 1, 1), date(2015, 4, 9))

    def test_no_ticker_is_a_data_gap_not_an_empty_series(self):
        adapter, _ = self.composite(None)
        with pytest.raises(DataUnavailable):
            adapter.get_prices("CIK0000000777", date(2015, 1, 1), date(2015, 12, 31))


class TestRiskFreeRateIsPointInTime:
    """A backtest must value each date with that date's yield, not today's."""

    def test_the_yield_on_the_as_of_date_is_used_and_fetched_once(self):
        from gcfp.data.composite import CompositeAdapter

        fundamentals = Mock()
        fundamentals.name = "edgar"
        prices = Mock()
        prices.name = "feed"
        prices.get_prices = Mock(return_value=(
            PricePoint(date(2026, 9, 30), 4.5),
            PricePoint(date(2015, 6, 30), 2.3),
        ))
        prices.get_index_level = Mock(return_value=4.5)
        adapter = CompositeAdapter(
            fundamentals=fundamentals, prices=prices, as_of=date(2015, 7, 3)
        )
        market = adapter.get_market_data()
        assert market.risk_free_rate == pytest.approx(0.023)
        assert market.risk_free_rate_date == date(2015, 6, 30)

        adapter.as_of = date(2026, 10, 1)
        assert adapter.get_market_data().risk_free_rate == pytest.approx(0.045)
        assert prices.get_prices.call_count == 1
        prices.get_index_level.assert_not_called()

    def test_no_quote_near_the_date_is_a_gap_not_a_stale_value(self):
        from gcfp.data.composite import CompositeAdapter

        fundamentals = Mock()
        fundamentals.name = "edgar"
        prices = Mock()
        prices.name = "feed"
        prices.get_prices = Mock(return_value=(PricePoint(date(2026, 9, 30), 4.5),))
        adapter = CompositeAdapter(
            fundamentals=fundamentals, prices=prices, as_of=date(2015, 7, 3)
        )
        assert adapter.get_market_data().risk_free_rate is None
