"""XBRL parsing, against payloads shaped like real SEC companyfacts.

This is the code most likely to be quietly wrong, and it cannot be checked
against live EDGAR from a sandbox, so the awkward cases get explicit tests:
overlapping year-to-date facts, amendments restating a period, filers using
different tags for the same concept, and the point-in-time cutoff.
"""

from __future__ import annotations

from datetime import date

import pytest

from gcfp.data import xbrl


def fact(val, end, start=None, filed="2024-02-01", form="10-K", **kw):
    entry = {"end": end, "val": val, "filed": filed, "form": form}
    if start:
        entry["start"] = start
    entry.update(kw)
    return entry


def payload(**tags):
    return {
        "cik": 18230,
        "entityName": "TESTCO",
        "facts": {"us-gaap": {
            tag: {"units": {"USD": entries}} for tag, entries in tags.items()
        }},
    }


class TestPeriodFiltering:
    def test_year_to_date_facts_do_not_masquerade_as_quarters(self):
        """A Q3 10-Q reports both three-month and nine-month revenue under the
        same tag. Taking the wrong one triples the quarter."""
        data = payload(Revenues=[
            fact(100, "2024-03-31", "2024-01-01", form="10-Q"),   # Q1, 90 days
            fact(210, "2024-06-30", "2024-04-01", form="10-Q"),   # Q2, 90 days
            fact(310, "2024-06-30", "2024-01-01", form="10-Q"),   # H1 YTD, 181 days
            fact(950, "2024-12-31", "2024-01-01", form="10-K"),   # FY, 365 days
        ])
        quarters = xbrl.select_facts(data, "revenue", annual=False)
        assert quarters[date(2024, 6, 30)].value == 210, "picked the YTD figure"
        assert date(2024, 12, 31) not in quarters, "a full year is not a quarter"

    def test_annual_selection_takes_only_full_year_periods(self):
        data = payload(Revenues=[
            fact(100, "2024-03-31", "2024-01-01", form="10-Q"),
            fact(950, "2024-12-31", "2024-01-01", form="10-K"),
        ])
        annual = xbrl.select_facts(data, "revenue", annual=True)
        assert set(annual) == {date(2024, 12, 31)}
        assert annual[date(2024, 12, 31)].value == 950

    def test_a_52_53_week_fiscal_year_still_counts_as_annual(self):
        """Retailers file 364- and 371-day years. A strict 365 would drop them."""
        data = payload(Revenues=[
            fact(900, "2024-02-03", "2023-01-29", form="10-K"),  # 371 days
        ])
        assert xbrl.select_facts(data, "revenue", annual=True)


class TestAmendments:
    def test_a_later_amendment_supersedes_the_original(self):
        data = payload(Revenues=[
            fact(950, "2024-12-31", "2024-01-01", filed="2025-02-01", form="10-K"),
            fact(925, "2024-12-31", "2024-01-01", filed="2025-06-01", form="10-K/A"),
        ])
        annual = xbrl.select_facts(data, "revenue", annual=True)
        assert len(annual) == 1, "the restated period must not appear twice"
        assert annual[date(2024, 12, 31)].value == 925

    def test_the_original_still_wins_before_the_amendment_was_filed(self):
        """The point of point-in-time: an evaluation in March 2025 could not
        have known about a restatement filed that June."""
        data = payload(Revenues=[
            fact(950, "2024-12-31", "2024-01-01", filed="2025-02-01", form="10-K"),
            fact(925, "2024-12-31", "2024-01-01", filed="2025-06-01", form="10-K/A"),
        ])
        annual = xbrl.select_facts(
            data, "revenue", annual=True, as_of=date(2025, 3, 1)
        )
        assert annual[date(2024, 12, 31)].value == 950


class TestTagChains:
    def test_the_preferred_tag_wins_over_a_later_one(self):
        data = payload(
            Revenues=[fact(900, "2024-12-31", "2024-01-01")],
            RevenueFromContractWithCustomerExcludingAssessedTax=[
                fact(950, "2024-12-31", "2024-01-01")
            ],
        )
        annual = xbrl.select_facts(data, "revenue", annual=True)
        got = annual[date(2024, 12, 31)]
        assert got.value == 950
        assert got.tag == "RevenueFromContractWithCustomerExcludingAssessedTax"

    def test_an_older_filer_using_a_legacy_tag_still_resolves(self):
        data = payload(SalesRevenueNet=[fact(800, "2016-12-31", "2016-01-01")])
        annual = xbrl.select_facts(data, "revenue", annual=True)
        assert annual[date(2016, 12, 31)].value == 800

    def test_available_fields_reports_which_tag_supplied_each_field(self):
        data = payload(
            Revenues=[fact(900, "2024-12-31", "2024-01-01")],
            Assets=[fact(5000, "2024-12-31")],
        )
        found = xbrl.available_fields(data)
        assert found["revenue"] == "Revenues"
        assert found["total_assets"] == "Assets"
        assert found["funds_from_operations"] is None


class TestInstants:
    def test_latest_instant_picks_the_balance_sheet_at_the_period_end(self):
        data = payload(Assets=[
            fact(4000, "2023-12-31"),
            fact(5000, "2024-12-31"),
            fact(5500, "2025-06-30"),
        ])
        got = xbrl.latest_instant(data, "total_assets", date(2024, 12, 31))
        assert got.value == 5000, "used a balance sheet from after the period end"

    def test_latest_instant_returns_none_when_nothing_precedes(self):
        data = payload(Assets=[fact(5000, "2024-12-31")])
        assert xbrl.latest_instant(data, "total_assets", date(2020, 1, 1)) is None


class TestUnits:
    def test_per_share_units_are_ignored(self):
        """A USD/shares fact mixed into a total would be wrong by orders of
        magnitude and hard to spot downstream."""
        data = {"facts": {"us-gaap": {"NetIncomeLoss": {"units": {
            "USD": [fact(1_000_000, "2024-12-31", "2024-01-01")],
            "USD/shares": [fact(2.15, "2024-12-31", "2024-01-01")],
        }}}}}
        annual = xbrl.select_facts(data, "net_income", annual=True)
        assert annual[date(2024, 12, 31)].value == 1_000_000

    def test_share_counts_come_through_the_shares_unit(self):
        data = {"facts": {"us-gaap": {
            "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {
                "shares": [fact(500_000_000, "2024-12-31", "2024-01-01")]
            }}
        }}}
        annual = xbrl.select_facts(data, "shares_diluted", annual=True)
        assert annual[date(2024, 12, 31)].value == 500_000_000


class TestMalformedInput:
    def test_missing_dates_and_values_are_skipped_not_guessed(self):
        data = payload(Revenues=[
            {"end": "2024-12-31", "start": "2024-01-01", "val": None, "filed": "2025-02-01"},
            {"end": None, "start": "2023-01-01", "val": 900, "filed": "2024-02-01"},
            fact(950, "2022-12-31", "2022-01-01"),
        ])
        annual = xbrl.select_facts(data, "revenue", annual=True)
        assert set(annual) == {date(2022, 12, 31)}

    def test_an_absent_concept_yields_an_empty_result_not_an_error(self):
        assert xbrl.select_facts(payload(), "revenue", annual=True) == {}


class TestFourthQuarter:
    """No filer reports Q4 on its own. It has to be derived or it is lost.

    The damage from losing it is not a sparse series. ``trailing_quarters(4)``
    reaches back past the hole and sums Q1+Q2+Q3 of one year with Q3 of the
    year before — double-counting one quarter and dropping another. Every TTM
    figure in the system is wrong by that difference until Q4 is recovered.
    """

    @staticmethod
    def two_years():
        """Three 10-Qs a year (3-month and YTD facts) and a 10-K stating only
        the full year, which is how filers actually report."""
        return payload(Revenues=[
            fact(100, "2024-03-31", "2024-01-01", filed="2024-04-25", form="10-Q"),
            fact(110, "2024-06-30", "2024-04-01", filed="2024-07-25", form="10-Q"),
            fact(210, "2024-06-30", "2024-01-01", filed="2024-07-25", form="10-Q"),
            fact(120, "2024-09-30", "2024-07-01", filed="2024-10-25", form="10-Q"),
            fact(330, "2024-09-30", "2024-01-01", filed="2024-10-25", form="10-Q"),
            fact(500, "2024-12-31", "2024-01-01", filed="2025-02-14", form="10-K"),
        ])

    def test_the_fourth_quarter_is_recovered_from_the_annual_report(self):
        quarters = xbrl.select_facts(self.two_years(), "revenue", annual=False)
        assert date(2024, 12, 31) in quarters, "Q4 was lost"
        assert quarters[date(2024, 12, 31)].value == 170, "500 FY less 330 nine-month"

    def test_the_four_quarters_sum_to_the_reported_year(self):
        """The one check that catches a double-counted or dropped quarter."""
        quarters = xbrl.select_facts(self.two_years(), "revenue", annual=False)
        annual = xbrl.select_facts(self.two_years(), "revenue", annual=True)
        assert sum(f.value for f in quarters.values()) == annual[date(2024, 12, 31)].value

    def test_the_derived_quarter_is_dated_by_the_10k_not_the_year_end(self):
        """Q4 becomes knowable when the annual report lands, not when the
        quarter closes. Dating it 31 December would hand a backtest six weeks
        of information it could not have had."""
        quarters = xbrl.select_facts(self.two_years(), "revenue", annual=False)
        assert quarters[date(2024, 12, 31)].filed == date(2025, 2, 14)

    def test_it_is_invisible_before_the_annual_report_is_filed(self):
        early = xbrl.select_facts(
            self.two_years(), "revenue", annual=False, as_of=date(2025, 1, 15)
        )
        assert date(2024, 12, 31) not in early

    def test_a_reported_fourth_quarter_is_never_overwritten(self):
        """A few filers do tag Q4. A real fact always beats a derived one."""
        data = payload(Revenues=[
            fact(330, "2024-09-30", "2024-01-01", filed="2024-10-25", form="10-Q"),
            fact(175, "2024-12-31", "2024-10-01", filed="2025-02-14", form="10-K"),
            fact(500, "2024-12-31", "2024-01-01", filed="2025-02-14", form="10-K"),
        ])
        quarters = xbrl.select_facts(data, "revenue", annual=False)
        assert quarters[date(2024, 12, 31)].value == 175

    def test_a_missing_nine_month_figure_yields_no_quarter_rather_than_a_year(self):
        """Without the nine months to subtract there is no Q4 to be had.
        Falling back on the full year would report a 4x quarter."""
        data = payload(Revenues=[
            fact(100, "2024-03-31", "2024-01-01", filed="2024-04-25", form="10-Q"),
            fact(500, "2024-12-31", "2024-01-01", filed="2025-02-14", form="10-K"),
        ])
        quarters = xbrl.select_facts(data, "revenue", annual=False)
        assert date(2024, 12, 31) not in quarters

    def test_cash_flow_fourth_quarter_comes_off_the_cumulative_series(self):
        """Cash-flow statements are year-to-date all the way through, so Q4 is
        the full year less the nine months, same as the income statement."""
        data = payload(NetCashProvidedByUsedInOperatingActivities=[
            fact(90, "2024-03-31", "2024-01-01", filed="2024-04-25", form="10-Q"),
            fact(200, "2024-06-30", "2024-01-01", filed="2024-07-25", form="10-Q"),
            fact(310, "2024-09-30", "2024-01-01", filed="2024-10-25", form="10-Q"),
            fact(450, "2024-12-31", "2024-01-01", filed="2025-02-14", form="10-K"),
        ])
        quarters = xbrl.select_facts(data, "operating_cash_flow", annual=False)
        assert [quarters[e].value for e in sorted(quarters)] == [90, 110, 110, 140]


class TestDebtAssembly:
    """Total debt, and the difference between "no debt" and "no reading".

    Texas Pacific Land carries no debt at all.  A parser that cannot read a
    filer's debt tags produces the same ``None``.  A2 treats those two
    identically unless the adapter distinguishes them, and it must.
    """

    @staticmethod
    def adapter(as_of=None):
        from unittest.mock import Mock

        from gcfp.data.edgar import EdgarAdapter

        return EdgarAdapter(user_agent="t t@example.com", session=Mock(), as_of=as_of)

    @staticmethod
    def balance_sheet(**tags):
        """A payload that always reads cleanly, plus whatever debt tags."""
        base = {
            "Assets": [fact(1000, "2024-12-31")],
            "StockholdersEquity": [fact(600, "2024-12-31")],
        }
        base.update(tags)
        return payload(**base)

    def test_parts_are_summed_without_double_counting(self):
        data = self.balance_sheet(
            LongTermDebtNoncurrent=[fact(300, "2024-12-31")],
            LongTermDebtCurrent=[fact(50, "2024-12-31")],
            CommercialPaper=[fact(25, "2024-12-31")],
        )
        value, basis = self.adapter()._total_debt(data, date(2024, 12, 31))
        assert (value, basis) == (375, "summed")

    def test_long_term_debt_is_not_added_to_its_own_current_portion(self):
        """us-gaap ``LongTermDebt`` already includes current maturities.

        Summing it with ``LongTermDebtCurrent`` counts that portion twice and
        overstates leverage, which fails A2 on companies that should pass.
        """
        data = self.balance_sheet(
            LongTermDebt=[fact(350, "2024-12-31")],
            LongTermDebtCurrent=[fact(50, "2024-12-31")],
        )
        value, basis = self.adapter()._total_debt(data, date(2024, 12, 31))
        assert value == 50, "the non-current bucket must not absorb LongTermDebt"
        assert basis == "summed"

    def test_long_term_debt_alone_is_used_whole(self):
        data = self.balance_sheet(LongTermDebt=[fact(350, "2024-12-31")])
        value, basis = self.adapter()._total_debt(data, date(2024, 12, 31))
        assert (value, basis) == (350, "long_term_only")

    def test_a_genuinely_debt_free_filer_reads_as_zero(self):
        data = self.balance_sheet()
        value, basis = self.adapter()._total_debt(data, date(2024, 12, 31))
        assert (value, basis) == (0.0, "inferred_zero")

    def test_an_unreadable_balance_sheet_refuses_rather_than_inventing_zero(self):
        """No equity tag means the statement did not resolve.  Silence about
        debt is then the parser's, not the filer's."""
        data = payload(Assets=[fact(1000, "2024-12-31")])
        assert self.adapter()._total_debt(data, date(2024, 12, 31)) == (None, None)

    def test_evidence_of_debt_elsewhere_blocks_the_zero(self):
        """Repaying debt proves the filer had some.  Reading none at this
        period end is then a miss, and must not become a zero."""
        data = self.balance_sheet(
            RepaymentsOfLongTermDebt=[
                fact(100, "2023-12-31", "2023-01-01", filed="2024-02-01")
            ]
        )
        assert self.adapter()._total_debt(data, date(2024, 12, 31)) == (None, None)

    def test_evidence_filed_after_the_cutoff_is_not_visible(self):
        """The point-in-time rule holds here too: a 2024 evaluation cannot be
        told about debt first disclosed in 2026."""
        data = self.balance_sheet(
            RepaymentsOfLongTermDebt=[
                fact(100, "2026-12-31", "2026-01-01", filed="2027-02-01")
            ]
        )
        cut = self.adapter(as_of=date(2025, 1, 1))
        assert cut._total_debt(data, date(2024, 12, 31)) == (0.0, "inferred_zero")

    def test_ifrs_borrowings_are_read(self):
        data = {
            "cik": 1, "entityName": "IFRSCO",
            "facts": {"ifrs-full": {
                "Assets": {"units": {"USD": [fact(1000, "2024-12-31")]}},
                "Equity": {"units": {"USD": [fact(600, "2024-12-31")]}},
                "NoncurrentBorrowings": {"units": {"USD": [fact(200, "2024-12-31")]}},
                "CurrentBorrowings": {"units": {"USD": [fact(40, "2024-12-31")]}},
            }},
        }
        value, basis = self.adapter()._total_debt(data, date(2024, 12, 31))
        assert (value, basis) == (240, "summed")


class TestDelistedCompanies:
    """The SEC ticker index lists current registrants only.

    A universe built from it contains survivors and nothing else, and a
    backtest over survivors reports the returns of companies that made it.
    Filings stay addressable by CIK forever, so the escape hatch has to exist.
    """

    @staticmethod
    def adapter(**kw):
        from unittest.mock import Mock

        from gcfp.data.edgar import EdgarAdapter

        a = EdgarAdapter(user_agent="t t@example.com", session=Mock(), **kw)
        a._ticker_map = {"AAPL": 320193}
        return a

    def test_a_cik_can_be_used_directly(self):
        a = self.adapter()
        assert a.ticker_to_cik("CIK0000719739") == 719739
        assert a.ticker_to_cik("CIK:719739") == 719739

    def test_an_override_pins_a_delisted_ticker(self):
        a = self.adapter(cik_overrides={"SIVBQ": 719739})
        assert a.ticker_to_cik("SIVBQ") == 719739

    def test_a_normal_ticker_still_resolves_through_the_index(self):
        assert self.adapter().ticker_to_cik("aapl") == 320193

    def test_the_failure_names_the_survivorship_consequence(self):
        from gcfp.data.adapter import DataUnavailable

        with pytest.raises(DataUnavailable) as exc:
            self.adapter().ticker_to_cik("SIVBQ")
        assert "delisted" in str(exc.value)
        assert "survivors" in str(exc.value)


class TestIndustryPeerDiscovery:
    """C2 needs four comparable names. They have to be found, not stumbled on.

    A few hundred tickers sliced off the market alphabetically contain no
    second oil royalty trader, so C2 reports "no peers" for a reason that is
    about the sample rather than about the market — and stop condition 4 then
    reads as a design finding when it is an artefact of the pool.
    """

    @staticmethod
    def adapter(atom: str):
        from unittest.mock import Mock

        from gcfp.data.edgar import EdgarAdapter

        a = EdgarAdapter(user_agent="t t@example.com", session=Mock())
        a._ticker_map = {"CAT": 18230, "DE": 315189, "TEX": 97216, "AGCO": 880266}
        a._get_text = lambda url, cache_key=None: atom
        return a

    ATOM = """<feed>
      <entry><title>DEERE &amp; CO</title>
        <link href="/cgi-bin/browse-edgar?action=getcompany&amp;CIK=0000315189"/></entry>
      <entry><title>TEREX CORP</title>
        <link href="/cgi-bin/browse-edgar?action=getcompany&amp;CIK=0000097216"/></entry>
      <entry><title>CATERPILLAR INC</title>
        <link href="/cgi-bin/browse-edgar?action=getcompany&amp;CIK=0000018230"/></entry>
    </feed>"""

    def test_an_industry_listing_becomes_tickers(self):
        found = self.adapter(self.ATOM).symbols_by_sic(3531, limit=10)
        assert set(found) == {"DE", "TEX", "CAT"}

    def test_the_multi_company_shape_is_also_read(self):
        """A single match redirects to a filing list whose links carry
        ``CIK=...``; a multi-company match returns ``<CIK>...</CIK>`` elements
        instead. Reading only the first shape found nothing for any industry,
        and the probe fell back to an alphabetical slice without saying so."""
        atom = """<feed>
          <entry><company-info><CIK>0000315189</CIK></company-info></entry>
          <entry><company-info><CIK>0000097216</CIK></company-info></entry>
        </feed>"""
        assert set(self.adapter(atom).symbols_by_sic(3531, limit=10)) == {"DE", "TEX"}

    def test_filers_with_no_ticker_are_skipped_not_guessed(self):
        """A private filer has a CIK and no ticker. It cannot be priced, so it
        cannot be a peer, and inventing a symbol for it would be worse."""
        atom = self.ATOM.replace("0000097216", "0009999999")
        found = self.adapter(atom).symbols_by_sic(3531, limit=10)
        assert "TEX" not in found and len(found) == 2

    def test_the_limit_is_respected(self):
        assert len(self.adapter(self.ATOM).symbols_by_sic(3531, limit=2)) == 2

    def test_a_transport_failure_yields_no_peers_rather_than_raising(self):
        """An empty peer set is a finding C2 knows how to report. A raised
        exception would take down the whole screen instead."""
        from unittest.mock import Mock

        from gcfp.data.edgar import EdgarAdapter

        a = EdgarAdapter(user_agent="t t@example.com", session=Mock())
        a._ticker_map = {"CAT": 18230}

        def boom(url, cache_key=None):
            raise RuntimeError("network")

        a._get_text = boom
        assert a.symbols_by_sic(3531) == []

    def test_a_missing_sic_code_is_not_an_error(self):
        assert self.adapter(self.ATOM).symbols_by_sic(None) == []
        assert self.adapter(self.ATOM).symbols_by_sic("") == []


class TestRealFilingPatternEndToEnd:
    """A calendar-year filer's whole year, through the adapter.

    The unit tests above check ``select_facts``. This checks what the gates
    actually receive, because that is where the missing quarter did its damage:
    CAT's probe run showed TTM net income, TTM cash flow and TTM EBITDA all
    missing at once, and every one of them was the same absent Q4.
    """

    @staticmethod
    def payload():
        def dur(val, end, start, filed, form):
            return {"val": val, "end": end, "start": start, "filed": filed, "form": form}

        rev, ni, ocf = [], [], []
        fy_r, fy_n, fy_o = 64e9, 10e9, 12e9
        # Three 10-Qs: each carries a three-month leg and a cumulative one.
        for end, start, filed, frac in [
            ("2025-03-31", "2025-01-01", "2025-04-30", 0.24),
            ("2025-06-30", "2025-04-01", "2025-07-30", 0.48),
            ("2025-09-30", "2025-07-01", "2025-10-29", 0.72),
        ]:
            rev.append(dur(fy_r * 0.24, end, start, filed, "10-Q"))
            ni.append(dur(fy_n * 0.24, end, start, filed, "10-Q"))
        for end, filed, frac in [
            ("2025-03-31", "2025-04-30", 0.24),
            ("2025-06-30", "2025-07-30", 0.48),
            ("2025-09-30", "2025-10-29", 0.72),
        ]:
            rev.append(dur(fy_r * frac, end, "2025-01-01", filed, "10-Q"))
            ni.append(dur(fy_n * frac, end, "2025-01-01", filed, "10-Q"))
            ocf.append(dur(fy_o * frac, end, "2025-01-01", filed, "10-Q"))
        # The 10-K states the year and no fourth quarter, which is the point.
        for series, v in ((rev, fy_r), (ni, fy_n), (ocf, fy_o)):
            series.append(dur(v, "2025-12-31", "2025-01-01", "2026-02-11", "10-K"))
        # Two quarters into the next year.
        for end, start, filed in [
            ("2026-03-31", "2026-01-01", "2026-04-29"),
            ("2026-06-30", "2026-04-01", "2026-08-05"),
        ]:
            rev.append(dur(16e9, end, start, filed, "10-Q"))
            ni.append(dur(2.6e9, end, start, filed, "10-Q"))
        for end, filed, frac in [("2026-03-31", "2026-04-29", 0.25),
                                 ("2026-06-30", "2026-08-05", 0.5)]:
            ocf.append(dur(fy_o * frac, end, "2026-01-01", filed, "10-Q"))

        return {"cik": 18230, "entityName": "TESTCO", "facts": {"us-gaap": {
            "Revenues": {"units": {"USD": rev}},
            "NetIncomeLoss": {"units": {"USD": ni}},
            "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": ocf}},
        }}}

    def quarters(self):
        from unittest.mock import Mock

        from gcfp.data.edgar import EdgarAdapter

        a = EdgarAdapter(user_agent="t t@example.com", session=Mock())
        a._facts = lambda symbol: self.payload()
        return a._periods("TESTCO", annual=False, limit=8)

    def test_the_trailing_year_has_four_quarters_with_no_hole(self):
        ttm = self.quarters()[:4]
        assert len(ttm) == 4
        assert all(q.net_income is not None for q in ttm), "a quarter is missing"
        assert all(q.operating_cash_flow is not None for q in ttm)

    def test_the_fourth_quarter_carries_the_10k_filing_date(self):
        """Dating it 31 December would give a backtest six weeks of
        information it could not have had."""
        q4 = next(q for q in self.quarters() if q.period_end == date(2025, 12, 31))
        assert q4.filing_date == date(2026, 2, 11)

    def test_the_calendar_year_reconciles_to_what_the_10k_reported(self):
        year = [q for q in self.quarters() if q.period_end.year == 2025]
        assert len(year) == 4
        assert sum(q.net_income for q in year) == pytest.approx(10e9)
        assert sum(q.operating_cash_flow for q in year) == pytest.approx(12e9)


class TestFirstFiledDate:
    """A period is known from the day it was first published, not the last
    day it was reprinted as a comparative.

    Every 10-Q repeats last year's quarter and every 10-K repeats two prior
    years, so taking the latest filing dated Caterpillar's Q1 2016 to February
    2018. Live, that scrambled which quarters the C1 reconstruction treated as
    known and cut CAT's usable history from about 6.8 years to 5.5.
    """

    @staticmethod
    def reprinted():
        return payload(Revenues=[
            fact(100, "2024-03-31", "2024-01-01", filed="2024-04-30", form="10-Q"),
            # The same quarter, reprinted as a comparative a year later.
            fact(100, "2024-03-31", "2024-01-01", filed="2025-04-29", form="10-Q"),
        ])

    def test_the_period_is_dated_by_its_first_filing(self):
        q = xbrl.select_facts(self.reprinted(), "revenue", annual=False)
        assert q[date(2024, 3, 31)].filed == date(2024, 4, 30)

    def test_a_restated_value_still_wins_but_keeps_the_original_date(self):
        data = payload(Revenues=[
            fact(100, "2024-03-31", "2024-01-01", filed="2024-04-30", form="10-Q"),
            fact(104, "2024-03-31", "2024-01-01", filed="2024-09-15", form="10-Q/A"),
        ])
        q = xbrl.select_facts(data, "revenue", annual=False)
        assert q[date(2024, 3, 31)].value == 104
        assert q[date(2024, 3, 31)].filed == date(2024, 4, 30)

    def test_point_in_time_is_unchanged(self):
        """Under as_of the later reprint is invisible, so nothing moves."""
        q = xbrl.select_facts(
            self.reprinted(), "revenue", annual=False, as_of=date(2024, 12, 31)
        )
        assert q[date(2024, 3, 31)].filed == date(2024, 4, 30)


class TestFourthQuarterShareCounts:
    """Share counts are time-weighted averages, not flows.

    Deriving Q4 as the year minus nine months is right for earnings and wrong
    for shares: it gave Caterpillar about minus four million shares, which
    either dropped C1 observations or produced a P/E near zero, and would
    have corrupted every per-share value between a 10-K and the next 10-Q.
    """

    @staticmethod
    def year(fy_avg, nine_month_avg):
        def dur(val, end, start, filed, form):
            return {"val": val, "end": end, "start": start, "filed": filed, "form": form}
        return {"cik": 1, "entityName": "X", "facts": {"us-gaap": {
            "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [
                dur(nine_month_avg, "2025-09-30", "2025-01-01", "2025-10-30", "10-Q"),
                dur(fy_avg, "2025-12-31", "2025-01-01", "2026-02-13", "10-K"),
            ]}},
        }}}

    def test_q4_is_recovered_by_duration_weighting(self):
        q = xbrl.select_facts(self.year(474e6, 476e6), "shares_diluted", annual=False)
        q4 = q[date(2025, 12, 31)].value
        # (474 x 364 - 476 x 272) / 92, the exact time-weighted remainder.
        assert q4 == pytest.approx((474e6 * 364 - 476e6 * 272) / 92)
        assert 440e6 < q4 < 476e6

    def test_a_share_count_is_never_derived_as_non_positive(self):
        q = xbrl.select_facts(self.year(100.0, 1e9), "shares_diluted", annual=False)
        assert date(2025, 12, 31) not in q

    def test_flows_are_still_subtracted(self):
        """Earnings are additive and keep the subtraction."""
        data = payload(NetIncomeLoss=[
            fact(300, "2025-09-30", "2025-01-01", filed="2025-10-30", form="10-Q"),
            fact(420, "2025-12-31", "2025-01-01", filed="2026-02-13", form="10-K"),
        ])
        q = xbrl.select_facts(data, "net_income", annual=False)
        assert q[date(2025, 12, 31)].value == 120


class TestPlaceholderZeros:
    def test_a_zero_under_a_preferred_tag_does_not_hide_real_revenue(self):
        """Flowserve tags Revenues as 0 beside $4.9bn of SalesRevenueNet, and
        read as a company with no sales — unclassifiable for years."""
        data = payload(
            Revenues=[fact(0, "2014-12-31", "2014-01-01")],
            SalesRevenueNet=[fact(4_877_885_000, "2014-12-31", "2014-01-01")],
        )
        annual = xbrl.select_facts(data, "revenue", annual=True)
        assert annual[date(2014, 12, 31)].value == 4_877_885_000

    def test_a_genuine_zero_still_reads_as_zero(self):
        data = payload(Revenues=[fact(0, "2014-12-31", "2014-01-01")])
        assert xbrl.select_facts(data, "revenue", annual=True)[date(2014, 12, 31)].value == 0

    def test_capex_split_by_kind_is_found(self):
        """ADP tags only "other" PP&E purchases; without it free cash flow,
        and so classification, was never computable."""
        data = payload(PaymentsToAcquireOtherPropertyPlantAndEquipment=[
            fact(158_800_000, "2015-06-30", "2014-07-01"),
        ])
        assert xbrl.select_facts(data, "capital_expenditure", annual=True)
