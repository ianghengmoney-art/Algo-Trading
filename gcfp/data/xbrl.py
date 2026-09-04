"""XBRL fact extraction from SEC ``companyfacts``.

This is the fiddly part of using EDGAR, and it is separated from the adapter so
it can be tested against recorded payloads without a network call.

Three things make raw XBRL unusable as-is:

* **Filers tag the same concept differently.** "Revenue" is
  ``RevenueFromContractWithCustomerExcludingAssessedTax`` for one company,
  ``Revenues`` for another, ``SalesRevenueNet`` for an older filing.  So every
  field resolves through an ordered fallback chain and records which tag
  actually supplied the number.
* **Duration facts overlap.** A Q3 10-Q reports three-month *and* nine-month
  revenue under the same tag.  Picking the wrong one silently triples a
  quarter.  So duration facts are filtered by the length of their period.
* **Amendments restate.** The same period appears several times across
  10-K, 10-K/A and later filings.  Keeping all of them double-counts; keeping
  an arbitrary one is a coin flip.  The latest-filed wins, and the filing date
  is retained because it is what makes this source point-in-time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Sequence

#: Ordered fallback chains.  Earlier tags are preferred; the first that yields
#: a usable fact for the period wins, and the winner is logged so a reader can
#: tell a ``Revenues`` filer from a ``RevenueFromContractWithCustomer`` one.
TAG_CHAINS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "RevenuesNetOfInterestExpense",
    ),
    "net_income": (
        "NetIncomeLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "ProfitLoss",
    ),
    "operating_cash_flow": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    "capital_expenditure": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsForCapitalImprovements",
    ),
    "operating_income": ("OperatingIncomeLoss",),
    "gross_profit": ("GrossProfit",),
    "interest_expense": (
        "InterestExpense",
        "InterestExpenseDebt",
        "InterestIncomeExpenseNet",
    ),
    "tax_expense": ("IncomeTaxExpenseBenefit",),
    "pretax_income": (
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
    ),
    "depreciation_amortization": (
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
    ),
    "total_assets": ("Assets",),
    "total_current_assets": ("AssetsCurrent",),
    "total_current_liabilities": ("LiabilitiesCurrent",),
    "cash_and_equivalents": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndCashEquivalentsIncludingDiscontinuedOperations",
    ),
    "short_term_investments": ("ShortTermInvestments", "OtherShortTermInvestments"),
    "total_equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    "goodwill": ("Goodwill",),
    "intangible_assets": (
        "IntangibleAssetsNetExcludingGoodwill",
        "FiniteLivedIntangibleAssetsNet",
    ),
    "long_term_debt_noncurrent": (
        "LongTermDebtNoncurrent",
        "LongTermDebt",
    ),
    "long_term_debt_current": (
        "LongTermDebtCurrent",
        "LongTermDebtAndCapitalLeaseObligationsCurrent",
    ),
    "short_term_debt": (
        "ShortTermBorrowings",
        "OtherShortTermBorrowings",
        "CommercialPaper",
    ),
    "total_debt_combined": ("DebtLongtermAndShorttermCombinedAmount",),
    "shares_diluted": (
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfDilutedSharesOutstandingBasic",
    ),
    "shares_basic": (
        "WeightedAverageNumberOfSharesOutstandingBasic",
        "WeightedAverageNumberOfSharesOutstanding",
    ),
    "shares_outstanding": ("CommonStockSharesOutstanding",),
    "dividends_paid": (
        "PaymentsOfDividendsCommonStock",
        "PaymentsOfDividends",
    ),
    # REIT and insurer measures.  These are rarely standard us-gaap tags —
    # most filers use custom extensions the companyfacts API does not expose —
    # so a miss here is expected and is reported, never worked around.
    "funds_from_operations": ("FundsFromOperations",),
    "adjusted_funds_from_operations": ("AdjustedFundsFromOperations",),
}

#: Facts with a ``start`` as well as an ``end`` cover a period rather than an
#: instant, and must be length-filtered.
DURATION_FIELDS: frozenset[str] = frozenset(
    {
        "revenue",
        "net_income",
        "operating_cash_flow",
        "capital_expenditure",
        "operating_income",
        "gross_profit",
        "interest_expense",
        "tax_expense",
        "pretax_income",
        "depreciation_amortization",
        "shares_diluted",
        "shares_basic",
        "dividends_paid",
        "funds_from_operations",
        "adjusted_funds_from_operations",
    }
)

#: Period lengths in days, with tolerance.  A "quarter" filed as 88 or 95 days
#: is still a quarter; a 270-day year-to-date figure is not.
QUARTER_DAYS = (75, 115)
ANNUAL_DAYS = (330, 400)


@dataclass(frozen=True)
class Fact:
    """One XBRL fact, with the provenance that makes it auditable."""

    value: float
    period_end: date
    period_start: date | None
    filed: date
    form: str
    tag: str
    fiscal_year: int | None = None
    fiscal_period: str | None = None

    @property
    def duration_days(self) -> int | None:
        if self.period_start is None:
            return None
        return (self.period_end - self.period_start).days


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _facts_for_tag(payload: dict[str, Any], tag: str) -> list[Fact]:
    """Every USD (or share) fact recorded under one tag."""
    concept = payload.get("facts", {}).get("us-gaap", {}).get(tag)
    if concept is None:
        concept = payload.get("facts", {}).get("dei", {}).get(tag)
    if concept is None:
        return []

    out: list[Fact] = []
    for unit, entries in (concept.get("units") or {}).items():
        # USD for money, "shares" for counts.  Per-share units (USD/shares) are
        # deliberately excluded: mixing a per-share fact into a total would be
        # wrong by orders of magnitude and hard to spot downstream.
        if unit not in ("USD", "shares"):
            continue
        for entry in entries:
            end = _parse_date(entry.get("end"))
            filed = _parse_date(entry.get("filed"))
            value = entry.get("val")
            if end is None or filed is None or value is None:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            out.append(
                Fact(
                    value=numeric,
                    period_end=end,
                    period_start=_parse_date(entry.get("start")),
                    filed=filed,
                    form=str(entry.get("form") or ""),
                    tag=tag,
                    fiscal_year=entry.get("fy"),
                    fiscal_period=entry.get("fp"),
                )
            )
    return out


def _matches_period(fact: Fact, annual: bool, field: str) -> bool:
    """Whether a fact covers the period length being asked for."""
    if field not in DURATION_FIELDS:
        # Instant fact (balance sheet).  Any period end is valid; the caller
        # matches it to a period end separately.
        return True
    days = fact.duration_days
    if days is None:
        return False
    low, high = ANNUAL_DAYS if annual else QUARTER_DAYS
    return low <= days <= high


def select_facts(
    payload: dict[str, Any],
    field: str,
    *,
    annual: bool,
    as_of: date | None = None,
) -> dict[date, Fact]:
    """Resolve one field to its best fact per period end.

    ``as_of`` restricts to facts *filed* on or before that date, which is what
    makes this source usable for a point-in-time backtest: a 2015 evaluation
    sees only what had actually been filed by 2015, restatements included or
    excluded on their real dates.
    """
    chosen: dict[date, Fact] = {}
    for tag in TAG_CHAINS.get(field, ()):
        for fact in _facts_for_tag(payload, tag):
            if as_of is not None and fact.filed > as_of:
                continue
            if not _matches_period(fact, annual, field):
                continue
            existing = chosen.get(fact.period_end)
            if existing is None:
                chosen[fact.period_end] = fact
                continue
            # Prefer the tag earlier in the chain; within a tag, the latest
            # filing wins, so an amendment supersedes what it restated.
            chain = TAG_CHAINS[field]
            if chain.index(fact.tag) < chain.index(existing.tag):
                chosen[fact.period_end] = fact
            elif fact.tag == existing.tag and fact.filed > existing.filed:
                chosen[fact.period_end] = fact
    return chosen


def latest_instant(
    payload: dict[str, Any],
    field: str,
    on_or_before: date,
    *,
    as_of: date | None = None,
) -> Fact | None:
    """The most recent balance-sheet fact at or before a period end."""
    facts = select_facts(payload, field, annual=False, as_of=as_of)
    candidates = [f for f in facts.values() if f.period_end <= on_or_before]
    if not candidates:
        return None
    return max(candidates, key=lambda f: (f.period_end, f.filed))


def company_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Name, CIK, and SIC where the payload carries them."""
    return {
        "cik": payload.get("cik"),
        "name": payload.get("entityName"),
    }


def available_fields(payload: dict[str, Any]) -> dict[str, str | None]:
    """Which tag, if any, supplies each field for this filer.

    The coverage probe reads this: a filer with no revenue tag in any chain is
    a normalisation gap, and the answer to that is to extend the chain, not to
    guess a number.
    """
    found: dict[str, str | None] = {}
    for field, chain in TAG_CHAINS.items():
        found[field] = next(
            (tag for tag in chain if _facts_for_tag(payload, tag)), None
        )
    return found


__all__ = [
    "Fact",
    "TAG_CHAINS",
    "DURATION_FIELDS",
    "QUARTER_DAYS",
    "ANNUAL_DAYS",
    "select_facts",
    "latest_instant",
    "company_metadata",
    "available_fields",
]
