"""Core data types passed between modules.

Everything the gates need arrives as one of these.  Fields are ``None`` when
the data source could not supply them — never zero, never imputed.  A5 turns
missing inputs into an explicit data-gap failure, and that only works if
"missing" survives the trip from the adapter intact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Sequence


class ReportingFrequency(str, Enum):
    """Module H1 keys monitoring cadence off this, not off the calendar."""

    QUARTERLY = "quarterly"
    SEMIANNUAL = "semiannual"
    UNKNOWN = "unknown"


class TaxonomyLevel(str, Enum):
    """Which rung of the classification ladder a grouping came from.

    A2 and C2 both need a peer grouping.  The spec assumes GICS sub-industry
    codes; when the data source cannot supply them, the level actually used is
    logged rather than silently substituted (§18 stop condition 3).
    """

    SUB_INDUSTRY = "sub_industry"
    INDUSTRY = "industry"
    SECTOR = "sector"
    UNAVAILABLE = "unavailable"


class CorporateActionType(str, Enum):
    """C1.1.  Splits are adjustable; spinoffs change what the company is."""

    SPLIT = "split"
    REVERSE_SPLIT = "reverse_split"
    SPINOFF = "spinoff"
    DIVESTITURE = "divestiture"
    ACQUISITION = "acquisition"


@dataclass(frozen=True)
class CorporateAction:
    action_type: CorporateActionType
    effective_date: date
    #: Split ratio for (reverse) splits; ``None`` otherwise.
    ratio: float | None = None
    #: Deal value as a share of market cap at the time, for
    #: divestitures/acquisitions.  C1.1 treats >25% as transformative.
    market_cap_share: float | None = None
    description: str | None = None

    @property
    def is_split_like(self) -> bool:
        return self.action_type in (
            CorporateActionType.SPLIT,
            CorporateActionType.REVERSE_SPLIT,
        )


@dataclass(frozen=True)
class PeriodFinancials:
    """One fiscal period.  Used for both annual and quarterly series."""

    period_end: date
    #: Date the figures were actually filed — what A5 ages against, and what
    #: A4 uses to detect a delayed filing.
    filing_date: date | None = None
    fiscal_year: int | None = None
    fiscal_period: str | None = None

    revenue: float | None = None
    gross_profit: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    ebitda: float | None = None
    #: ``"operating_income"`` (operating income + D&A) or
    #: ``"pretax_plus_interest"`` (pre-tax income + interest expense + D&A,
    #: for filers whose income statement has no operating-income line).
    ebitda_basis: str | None = None
    #: Operating income where tagged; otherwise pre-tax income plus gross
    #: interest expense, for filers with no operating-income line.
    ebit: float | None = None
    interest_expense: float | None = None
    tax_expense: float | None = None
    pretax_income: float | None = None

    operating_cash_flow: float | None = None
    capital_expenditure: float | None = None
    free_cash_flow: float | None = None

    total_assets: float | None = None
    total_current_assets: float | None = None
    total_current_liabilities: float | None = None
    total_debt: float | None = None
    #: How ``total_debt`` was arrived at: ``"tagged"`` (a single combined tag),
    #: ``"summed"`` (non-current + current + short-term parts),
    #: ``"long_term_only"`` (a total-including-current tag, no parts), or
    #: ``"inferred_zero"`` (no debt tag anywhere in the filer's history and a
    #: readable balance sheet, so the company is treated as debt-free).  A2
    #: logs this so an inferred zero is never mistaken for a measured one.
    debt_basis: str | None = None
    cash_and_equivalents: float | None = None
    total_equity: float | None = None
    tangible_book_value: float | None = None
    invested_capital: float | None = None

    shares_diluted: float | None = None
    shares_outstanding: float | None = None
    dividends_paid: float | None = None

    # Classification-specific inputs.  Present only for the paths that need
    # them; their absence is exactly what the §18 probe is looking for.
    funds_from_operations: float | None = None
    adjusted_funds_from_operations: float | None = None
    combined_ratio: float | None = None
    net_interest_income: float | None = None
    provision_for_loan_losses: float | None = None

    currency: str | None = None

    @property
    def current_ratio(self) -> float | None:
        if self.total_current_assets is None or not self.total_current_liabilities:
            return None
        return self.total_current_assets / self.total_current_liabilities

    @property
    def net_debt(self) -> float | None:
        if self.total_debt is None or self.cash_and_equivalents is None:
            return None
        return self.total_debt - self.cash_and_equivalents


@dataclass(frozen=True)
class MultipleObservation:
    """One point in a C1 historical multiple series."""

    observation_date: date
    value: float
    #: Which multiple this is (``trailing_pe``, ``p_affo``, ...), so a series
    #: can never be silently assembled from two different measures.
    multiple: str


@dataclass(frozen=True)
class PricePoint:
    price_date: date
    close: float
    #: Split-adjusted close, when the source distinguishes them.  C1.1 needs
    #: the adjusted series; D5's 12-1 momentum needs a total-return series.
    adjusted_close: float | None = None
    volume: float | None = None


@dataclass(frozen=True)
class DividendEvent:
    """A cash dividend per share, on the basis the price feed's history uses
    (adjusted for every split to date)."""

    ex_date: date
    amount: float


@dataclass(frozen=True)
class AuditorEvent:
    event_date: date
    changed: bool
    stated_reason: str | None = None
    #: A4 disqualifies on an auditor change without a stated benign reason.
    benign: bool = False


@dataclass(frozen=True)
class CompanyProfile:
    """Identity, structure, and the fields the universe screen reads."""

    symbol: str
    name: str
    #: The currency the security trades in.  K2 values in the *reporting*
    #: currency and translates only for display.
    listing_currency: str = "USD"
    reporting_currency: str = "USD"
    exchange: str | None = None
    country: str | None = None

    market_cap: float | None = None
    adv_3m_usd: float | None = None
    beta: float | None = None
    #: True when ``beta`` came from a sub-industry median rather than the
    #: name itself — B1 flags "BETA: PROXY".
    beta_is_proxy: bool = False

    sector: str | None = None
    industry: str | None = None
    sub_industry: str | None = None
    gics_sub_industry_code: str | None = None
    #: The numeric code behind ``industry`` in whatever taxonomy supplied it
    #: (SIC, from EDGAR).  Kept because a code is queryable — it is what lets
    #: peer candidates be drawn from the same industry directly, rather than
    #: sampled from the market and hoped over.
    industry_code: str | None = None
    #: The finest rung the source could actually supply.
    taxonomy_level: TaxonomyLevel = TaxonomyLevel.UNAVAILABLE
    #: Whether the taxonomy is genuinely GICS or a vendor substitute.
    taxonomy_is_gics: bool = False

    is_adr: bool = False
    #: K5: the true economic exposure behind an ADR (TWD for TSM), which is
    #: not the currency it trades in.
    underlying_currency: str | None = None

    is_reit: bool = False
    is_bank: bool = False
    is_insurer: bool = False

    ipo_date: date | None = None
    reporting_frequency: ReportingFrequency = ReportingFrequency.UNKNOWN
    next_earnings_date: date | None = None
    last_report_date: date | None = None
    #: When the next report was *scheduled*.  A4 fails a filing more than 15
    #: days past this (H1).
    next_report_due_date: date | None = None

    @property
    def listing_years(self) -> float | None:
        if self.ipo_date is None:
            return None
        return (date.today() - self.ipo_date).days / 365.25


@dataclass(frozen=True)
class CompanyData:
    """Everything one company's evaluation draws on."""

    profile: CompanyProfile
    #: Newest first.
    annual: Sequence[PeriodFinancials] = field(default_factory=tuple)
    quarterly: Sequence[PeriodFinancials] = field(default_factory=tuple)
    prices: Sequence[PricePoint] = field(default_factory=tuple)
    multiples: Sequence[MultipleObservation] = field(default_factory=tuple)
    corporate_actions: Sequence[CorporateAction] = field(default_factory=tuple)

    current_price: float | None = None
    price_date: date | None = None

    forward_eps_growth: float | None = None
    dividend_yield: float | None = None
    restatements: Sequence[date] = field(default_factory=tuple)
    auditor_events: Sequence[AuditorEvent] = field(default_factory=tuple)
    going_concern_language: bool = False

    #: Free-text notes from the adapter about provenance or known gaps.
    source_notes: Sequence[str] = field(default_factory=tuple)
    source_name: str = "unknown"

    def trailing_quarters(self, n: int = 4) -> list[PeriodFinancials]:
        return list(self.quarterly[:n])

    def trailing_years(self, n: int) -> list[PeriodFinancials]:
        return list(self.annual[:n])

    @property
    def latest_quarter(self) -> PeriodFinancials | None:
        return self.quarterly[0] if self.quarterly else None

    @property
    def latest_annual(self) -> PeriodFinancials | None:
        return self.annual[0] if self.annual else None


@dataclass(frozen=True)
class MarketData:
    """Market-wide inputs, pulled once per run rather than per name."""

    #: B1: current 10-year US Treasury yield (^TNX), dated in the log.
    risk_free_rate: float | None = None
    risk_free_rate_date: date | None = None
    #: K: spot rates keyed ``"USDSGD"``.
    fx_rates: dict[str, float] = field(default_factory=dict)
    fx_rate_timestamp: date | None = None
    #: Sub-industry aggregates used by A2's median and D4's margin-stability
    #: comparison, keyed by grouping name.
    group_net_debt_ebitda_median: dict[str, float] = field(default_factory=dict)
    group_beta_median: dict[str, float] = field(default_factory=dict)
    group_gross_margin_stdev_median: dict[str, float] = field(default_factory=dict)
    #: How many members of each grouping had the metric computable.  A2
    #: escalates its fallback ladder on this, not on nominal membership.
    group_member_counts: dict[str, int] = field(default_factory=dict)
    #: The rung each grouping key was taken from.
    group_taxonomy_level: dict[str, TaxonomyLevel] = field(default_factory=dict)
    #: B4: prevailing sector cap-rate environment.
    sector_cap_rates: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class TamSource:
    """B2.1 requires every TAM figure to be cited, dated, and named."""

    name: str
    published: date
    figure: float
    currency: str = "USD"
    url: str | None = None
    method: str = "third_party"

    def as_log_line(self) -> str:
        return (
            f"TAM source={self.name} date={self.published.isoformat()} "
            f"figure={self.figure:,.0f} {self.currency} method={self.method}"
        )


@dataclass(frozen=True)
class DataGap:
    """A named missing input.  A5 turns any of these into a hard fail."""

    field_name: str
    module: str
    detail: str | None = None

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.module}:{self.field_name}" + (
            f" ({self.detail})" if self.detail else ""
        )


def coerce_dict(obj: Any) -> dict[str, Any]:
    """Shallow dataclass -> dict, for storage rows and report payloads."""
    if hasattr(obj, "__dataclass_fields__"):
        return {name: getattr(obj, name) for name in obj.__dataclass_fields__}
    raise TypeError(f"not a dataclass: {type(obj)!r}")


__all__ = [
    "ReportingFrequency",
    "TaxonomyLevel",
    "CorporateActionType",
    "CorporateAction",
    "PeriodFinancials",
    "MultipleObservation",
    "PricePoint",
    "AuditorEvent",
    "CompanyProfile",
    "CompanyData",
    "MarketData",
    "TamSource",
    "DataGap",
    "coerce_dict",
]
