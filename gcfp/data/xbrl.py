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

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Callable, Iterable, Sequence

#: Ordered fallback chains.  Earlier tags are preferred; the first that yields
#: a usable fact for the period wins, and the winner is logged so a reader can
#: tell a ``Revenues`` filer from a ``RevenueFromContractWithCustomer`` one.
TAG_CHAINS: dict[str, tuple[str, ...]] = {
    # Revenue is the single most variably-tagged concept in XBRL: the tag a
    # filer uses depends on its industry, its era, and its accountant. A miss
    # here rejects the company at A5 before any judgement is made about it,
    # which is why this chain is the longest.
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet",
        "RevenuesNetOfInterestExpense",
        "TotalRevenuesAndOtherIncomeNet",
        "RegulatedAndUnregulatedOperatingRevenue",
        "RealEstateRevenueNet",
        "HealthCareOrganizationRevenue",
        "InterestAndDividendIncomeOperating",
        "RevenuesExcludingInterestAndDividends",
        "ContractsRevenue",
        "OperatingLeasesIncomeStatementLeaseRevenue",
        # IFRS (foreign private issuers filing 20-F).
        "Revenue",
        "RevenueFromContractsWithCustomers",
        "RevenueFromSaleOfGoods",
    ),
    "net_income": (
        "NetIncomeLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "ProfitLoss",
        "IncomeLossFromContinuingOperations",
        "NetIncomeLossAllocatedToLimitedPartners",
        "ProfitLossAttributableToOwnersOfParent",
    ),
    "operating_cash_flow": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        "CashFlowsFromUsedInOperatingActivities",
    ),
    # Cost of revenue, so gross profit can be derived when GrossProfit itself
    # is untagged — which is common, and which D4's margin-stability
    # sub-component otherwise scores as zero.
    "cost_of_revenue": (
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
        "CostOfServices",
    ),
    "capital_expenditure": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsForCapitalImprovements",
    ),
    "operating_income": (
        "OperatingIncomeLoss",
        "IncomeLossFromContinuingOperationsBeforeInterestExpenseInterestIncomeIncomeTaxesExtraordinaryItemsNoncontrollingInterestsNet",
        "ProfitLossFromOperatingActivities",
    ),
    "gross_profit": ("GrossProfit",),
    # Financial filers tag InterestExpense directly; industrials increasingly
    # use InterestExpenseNonoperating or fold it into a net figure. Missing
    # this only costs the WACC leg — B1 falls back to cost of equity and flags
    # it — but the fallback should be rare, not universal.
    "interest_expense": (
        "InterestExpense",
        "InterestExpenseNonoperating",
        "InterestExpenseDebt",
        "InterestExpenseBorrowings",
        "InterestAndDebtExpense",
        "InterestIncomeExpenseNet",
        "InterestExpenseOther",
        "FinanceCosts",
    ),
    "tax_expense": ("IncomeTaxExpenseBenefit",),
    "pretax_income": (
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
    ),
    #: Filers that never tag a combined figure.  Depreciation and amortisation
    #: are then two separate lines, and a first-match chain finds neither —
    #: which is why 34 names in a 134-name screen could not produce EBITDA and
    #: were blocked at A2.  ``EdgarAdapter`` sums these when the combined tag
    #: above misses.  They are separate fields rather than extra entries in
    #: that chain because a chain picks one tag; this case needs both.
    "depreciation": (
        "Depreciation",
        "DepreciationNonproduction",
        "PropertyPlantAndEquipmentDepreciationMethodsAmount",
    ),
    "amortization": (
        "AmortizationOfIntangibleAssets",
        "AmortizationOfFiniteLivedIntangibleAssets",
        "AdjustmentForAmortization",
    ),
    "depreciation_amortization": (
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
        "DepreciationAmortisationAndImpairmentLossReversalOfImpairmentLossRecognisedInProfitOrLoss",
        "DepreciationAndAmortisationExpense",
    ),
    "total_assets": ("Assets",),
    "total_current_assets": ("AssetsCurrent",),
    "total_current_liabilities": ("LiabilitiesCurrent",),
    "cash_and_equivalents": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndCashEquivalentsIncludingDiscontinuedOperations",
        "CashCashEquivalentsAndShortTermInvestments",
        "CashAndDueFromBanks",
    ),
    "short_term_investments": ("ShortTermInvestments", "OtherShortTermInvestments"),
    "total_equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "PartnersCapital",
        "MembersEquity",
        "CommonStockholdersEquity",
        "EquityAttributableToOwnersOfParent",
        "Equity",
    ),
    "goodwill": ("Goodwill",),
    "intangible_assets": (
        "IntangibleAssetsNetExcludingGoodwill",
        "FiniteLivedIntangibleAssetsNet",
    ),
    # Debt is assembled from three disjoint buckets so the parts can be summed
    # without double counting: the non-current portion, the current portion of
    # long-term debt, and short-term borrowings.  Each bucket takes the first
    # tag that resolves, so alternative names for the same quantity never add
    # to each other.  ``LongTermDebt`` is deliberately absent from the
    # non-current bucket: us-gaap defines it as including current maturities,
    # so summing it with the current portion would count that portion twice.
    # It lives in ``long_term_debt_including_current`` instead.
    "long_term_debt_noncurrent": (
        "LongTermDebtNoncurrent",
        "LongTermDebtAndCapitalLeaseObligationsNoncurrent",
        "LongTermNotesPayableNoncurrent",
        "LongTermNotesPayable",
        "ConvertibleDebtNoncurrent",
        "SeniorNotesNoncurrent",
        "NoncurrentPortionOfNoncurrentBorrowings",
        "NoncurrentBorrowings",
    ),
    "long_term_debt_current": (
        "LongTermDebtCurrent",
        "LongTermDebtAndCapitalLeaseObligationsCurrent",
        "CurrentPortionOfLongTermBorrowings",
    ),
    "short_term_debt": (
        "ShortTermBorrowings",
        "OtherShortTermBorrowings",
        "CommercialPaper",
        "NotesPayableCurrent",
        "LinesOfCreditCurrent",
        "ShorttermBorrowings",
        "CurrentBorrowings",
    ),
    "total_debt_combined": (
        "DebtLongtermAndShorttermCombinedAmount",
        "Borrowings",
    ),
    #: REITs, and others presenting an unclassified balance sheet, split debt
    #: by security rather than by maturity, so none of the three buckets above
    #: resolves.  These two are disjoint from each other by definition and each
    #: already includes current maturities, so they are summed together and
    #: used only when the maturity buckets came up empty.
    "unclassified_secured_debt": (
        "SecuredDebt",
        "SecuredDebtOtherThanRealEstate",
        "MortgageLoansOnRealEstateCommercialAndConsumerNetAmount",
    ),
    "unclassified_unsecured_debt": (
        "UnsecuredDebt",
        "UnsecuredLongTermDebt",
        "SeniorLongTermNotes",
        "LineOfCreditFacilityAmountOutstanding",
    ),
    #: us-gaap's ``LongTermDebt`` includes current maturities, and IFRS
    #: ``BorrowingsNoncurrent`` filers often tag only this.  Used as a whole
    #: only when the non-current bucket is empty, never added to it.
    "long_term_debt_including_current": (
        "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations",
        "DebtInstrumentFaceAmount",
    ),
    #: Any of these appearing anywhere in a filer's history proves the filer
    #: does carry debt, so a period that reads none is a parsing miss rather
    #: than a debt-free balance sheet.  Interest expense counts: you do not
    #: pay it on debt you do not have.
    "debt_existence_evidence": (
        "DebtInstrumentCarryingAmount",
        "DebtInstrumentFaceAmount",
        "InterestExpenseDebt",
        "RepaymentsOfDebt",
        "RepaymentsOfLongTermDebt",
        "ProceedsFromIssuanceOfLongTermDebt",
        "ProceedsFromNotesPayable",
        "FinanceLeaseLiabilityNoncurrent",
    ),
    "shares_diluted": (
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfDilutedSharesOutstandingBasic",
    ),
    "shares_basic": (
        "WeightedAverageNumberOfSharesOutstandingBasic",
        "WeightedAverageNumberOfSharesOutstanding",
    ),
    "shares_outstanding": (
        "CommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
        "CommonStockSharesIssued",
    ),
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
        "cost_of_revenue",
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

#: Fields a 10-Q reports **cumulatively within the fiscal year** rather than
#: per quarter.  The cash flow statement is the whole of it: Q1 covers three
#: months, Q2 covers six, Q3 covers nine, and no three-month fact is filed for
#: Q2 or Q3 at all.
#:
#: This is the single most consequential quirk in EDGAR for this system. The
#: income statement carries *both* a three-month and a year-to-date fact, so
#: TTM net income works; the cash flow statement carries only the year-to-date
#: one, so a naive quarterly filter finds exactly one quarter a year and TTM
#: operating cash flow is never computable. That silently disables A1's
#: cash-flow branch, the whole of A3, the cash-runway test, and TTM EBITDA —
#: which reads as "this data source cannot support the strategy" when it
#: actually means "the parser did not subtract".
YTD_FIELDS: frozenset[str] = frozenset(
    {
        "operating_cash_flow",
        "capital_expenditure",
        "depreciation_amortization",
        "dividends_paid",
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


#: Namespaces searched, in order.  Foreign private issuers file 20-F under
#: **IFRS**, which lives in ``ifrs-full`` — a US-GAAP-only reader sees nothing
#: at all for them, which is why an ADR came back with 21% coverage and no
#: classification rather than with a specific missing field.
NAMESPACES: tuple[str, ...] = ("us-gaap", "ifrs-full", "dei")


def _facts_for_tag(payload: dict[str, Any], tag: str) -> list[Fact]:
    """Every USD (or share) fact recorded under one tag, in any namespace."""
    facts = payload.get("facts", {})
    concept = None
    for namespace in NAMESPACES:
        concept = facts.get(namespace, {}).get(tag)
        if concept is not None:
            break
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


#: Cumulative periods a 10-Q can report: one, two or three quarters into the
#: fiscal year.  A fourth would be the annual figure.
YTD_DAYS = (75, 290)

#: Duration fields that are *time-weighted averages*, not flows.  A year's
#: weighted-average share count is not the sum of its quarters, so Q4 cannot
#: be the year minus nine months: that produced about minus four million
#: shares for Caterpillar.  It is recovered by duration weighting instead —
#: exact, by the definition of a time-weighted average.
AVERAGE_FIELDS: frozenset[str] = frozenset({"shares_diluted", "shares_basic"})

#: A three-quarter cumulation is ~273 days.  Widened for 52/53-week filers and
#: for the odd fiscal calendar; the band must not reach ANNUAL_DAYS or a full
#: year would be mistaken for nine months and Q4 would come out near zero.
NINE_MONTH_DAYS = (240, 300)


def _in_range(fact: Fact, band: tuple[int, int]) -> bool:
    days = fact.duration_days
    return days is not None and band[0] <= days <= band[1]


def _matches_period(fact: Fact, annual: bool, field: str) -> bool:
    """Whether a fact covers the period length being asked for."""
    if field not in DURATION_FIELDS:
        # Instant fact (balance sheet).  Any period end is valid; the caller
        # matches it to a period end separately.
        return True
    days = fact.duration_days
    if days is None:
        return False
    if annual:
        low, high = ANNUAL_DAYS
        return low <= days <= high
    if field in YTD_FIELDS:
        # Keep the cumulative facts; ``_to_quarterly`` differences them back.
        low, high = YTD_DAYS
        return low <= days <= high
    low, high = QUARTER_DAYS
    return low <= days <= high


def _to_quarterly(facts: dict[date, Fact], field: str) -> dict[date, Fact]:
    """Recover per-quarter values from cumulative year-to-date facts.

    A filer reporting 100 at Q1 and 250 at Q2 had a 150 second quarter. The
    subtraction only holds *within* a fiscal year, so a fact whose period start
    differs from its predecessor's is already a fresh cumulation and is kept
    as-is — which is what makes this safe across a fiscal year boundary.
    """
    if field not in YTD_FIELDS or not facts:
        return facts

    ordered = sorted(facts.values(), key=lambda f: f.period_end)
    out: dict[date, Fact] = {}
    previous: Fact | None = None

    for fact in ordered:
        cumulative_from_same_start = (
            previous is not None
            and fact.period_start is not None
            and previous.period_start is not None
            and fact.period_start == previous.period_start
            and fact.period_end > previous.period_end
        )
        if cumulative_from_same_start:
            out[fact.period_end] = Fact(
                value=fact.value - previous.value,
                period_end=fact.period_end,
                period_start=previous.period_end,
                filed=fact.filed,
                form=fact.form,
                tag=fact.tag,
                fiscal_year=fact.fiscal_year,
                fiscal_period=fact.fiscal_period,
            )
        else:
            # First quarter of a cumulation, or a genuine three-month fact.
            out[fact.period_end] = fact
        previous = fact

    return out


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
    chosen = _resolve(
        payload, field, lambda f: _matches_period(f, annual, field), as_of=as_of
    )
    if annual:
        return chosen
    chosen = _to_quarterly(chosen, field)
    return _with_derived_fourth_quarter(payload, field, chosen, as_of=as_of)


def _resolve(
    payload: dict[str, Any],
    field: str,
    keep: Callable[[Fact], bool],
    *,
    as_of: date | None,
) -> dict[date, Fact]:
    """Best fact per period end among those ``keep`` admits."""
    chosen: dict[date, Fact] = {}
    #: When each period first became public, under any tag in the chain.
    first_filed: dict[date, date] = {}
    chain = TAG_CHAINS.get(field, ())
    for tag in chain:
        for fact in _facts_for_tag(payload, tag):
            if as_of is not None and fact.filed > as_of:
                continue
            if not keep(fact):
                continue
            known = first_filed.get(fact.period_end)
            if known is None or fact.filed < known:
                first_filed[fact.period_end] = fact.filed
            existing = chosen.get(fact.period_end)
            if existing is None:
                chosen[fact.period_end] = fact
                continue
            # Prefer the tag earlier in the chain; within a tag, the latest
            # filing wins, so an amendment supersedes what it restated.
            if chain.index(fact.tag) < chain.index(existing.tag):
                chosen[fact.period_end] = fact
            elif fact.tag == existing.tag and fact.filed > existing.filed:
                chosen[fact.period_end] = fact

    # The *value* comes from the latest filing, but the *date* must be the
    # first.  Every 10-Q repeats last year's quarter as a comparative and
    # every 10-K repeats two prior years, so "latest filing" dated each
    # period by the last time it was reprinted — about two years after it
    # became public.  Caterpillar's Q1 2016 read as filed in February 2018.
    # Live, that scrambled which quarters counted as known at each date in
    # the C1 reconstruction and cut CAT's usable history to 5.5 years.
    # Under ``as_of`` nothing filed later is ever seen, so a backtest was not
    # affected; the first-filed date is what it already used.
    return {
        end: replace(fact, filed=first_filed[end]) if fact.filed != first_filed[end] else fact
        for end, fact in chosen.items()
    }


def _with_derived_fourth_quarter(
    payload: dict[str, Any],
    field: str,
    quarters: dict[date, Fact],
    *,
    as_of: date | None,
) -> dict[date, Fact]:
    """Add the fourth quarter, which no filer reports on its own.

    A 10-K states the full year.  It does not state Q4 — that quarter exists
    only as the year minus the nine months already reported.  Nothing in EDGAR
    fills the gap, so without this every fourth quarter is simply absent, and
    the damage is not merely a sparse series: ``trailing_quarters(4)`` reaches
    back past the hole and sums Q1+Q2+Q3 of one year with Q3 of the year
    before, double-counting one quarter and dropping another.  Every TTM
    figure in the system — earnings, cash flow, EBITDA, and every multiple
    built on them — is wrong by that much until Q4 is recovered.

    Derived facts are dated by the 10-K's filing date, not the year end, so
    the point-in-time rule holds: Q4 becomes knowable when the annual report
    lands, not when the quarter closes.
    """
    if field not in DURATION_FIELDS:
        return quarters

    annuals = _resolve(
        payload, field, lambda f: _in_range(f, ANNUAL_DAYS), as_of=as_of
    )
    if not annuals:
        return quarters
    nine_months = _resolve(
        payload, field, lambda f: _in_range(f, NINE_MONTH_DAYS), as_of=as_of
    )
    if not nine_months:
        return quarters

    out = dict(quarters)
    by_start: dict[date, list[Fact]] = {}
    for fact in nine_months.values():
        if fact.period_start is not None:
            by_start.setdefault(fact.period_start, []).append(fact)

    for year_end, full_year in annuals.items():
        if year_end in out:
            # The filer reported this quarter itself, or it was already
            # differenced out of a cumulation.  Never overwrite a real fact.
            continue
        if full_year.period_start is None:
            continue
        for ytd in by_start.get(full_year.period_start, ()):
            gap = (year_end - ytd.period_end).days
            low, high = QUARTER_DAYS
            if not (low <= gap <= high):
                continue
            if field in AVERAGE_FIELDS:
                fy_days, ytd_days = full_year.duration_days, ytd.duration_days
                if not fy_days or not ytd_days or gap <= 0:
                    continue
                value = (full_year.value * fy_days - ytd.value * ytd_days) / gap
                if value <= 0:
                    # A share count cannot be non-positive.  Report nothing
                    # rather than a number that is plainly impossible.
                    continue
            else:
                value = full_year.value - ytd.value
            out[year_end] = Fact(
                value=value,
                period_end=year_end,
                period_start=ytd.period_end,
                # The subtraction is only known once both halves are filed.
                filed=max(full_year.filed, ytd.filed),
                form=full_year.form,
                tag=full_year.tag,
                fiscal_year=full_year.fiscal_year,
                fiscal_period="Q4",
            )
            break

    return out


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


def has_any_fact(
    payload: dict[str, Any], field: str, *, as_of: date | None = None
) -> bool:
    """Whether any fact was ever filed under a field's tag chain.

    Deliberately ignores period shape and period end: this answers "did this
    filer ever report this at all", which is what separates a balance sheet
    that carries no debt from one whose debt tags this parser failed to read.
    """
    for tag in TAG_CHAINS.get(field, ()):
        for fact in _facts_for_tag(payload, tag):
            if as_of is not None and fact.filed > as_of:
                continue
            return True
    return False


__all__ = [
    "Fact",
    "TAG_CHAINS",
    "NAMESPACES",
    "DURATION_FIELDS",
    "QUARTER_DAYS",
    "ANNUAL_DAYS",
    "YTD_DAYS",
    "NINE_MONTH_DAYS",
    "AVERAGE_FIELDS",
    "YTD_FIELDS",
    "select_facts",
    "has_any_fact",
    "latest_instant",
    "company_metadata",
    "available_fields",
]
