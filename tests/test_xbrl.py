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
