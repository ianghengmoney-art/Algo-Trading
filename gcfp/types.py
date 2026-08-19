"""Adapter-agnostic data model.

Every numeric field is ``Optional``. Missing means missing: gate A5 fails a
candidate on absent inputs and the code never substitutes a zero, a sector
average, or a carried-forward prior period for a value the source did not
supply. A silent zero here becomes a passed gate three modules downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional, Sequence


class PeriodType(str, Enum):
    QUARTER = "Q"
    ANNUAL = "A"


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    DATA_GAP = "DATA_GAP"


class Direction(str, Enum):
    UNDERVALUED = "UNDERVALUED"
    OVERVALUED = "OVERVALUED"
    FAIR = "FAIR"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CompanyProfile:
    symbol: str
    name: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    exchange: Optional[str] = None
    country: Optional[str] = None
    currency: Optional[str] = None
    market_cap: Optional[float] = None
    price: Optional[float] = None
    beta: Optional[float] = None
    avg_daily_dollar_volume: Optional[float] = None
    shares_outstanding: Optional[float] = None
    is_adr: Optional[bool] = None
    is_etf: Optional[bool] = None
    is_fund: Optional[bool] = None
    is_actively_trading: Optional[bool] = None
    ipo_date: Optional[date] = None
    # SIC / GICS style hints used by the A6 router for the structural tags.
    sic_code: Optional[str] = None
    # SEC Central Index Key -- the join key for the EDGAR filing-flag overlay.
    cik: Optional[str] = None
    is_reit: Optional[bool] = None
    is_bank: Optional[bool] = None
    is_insurer: Optional[bool] = None
    as_of: Optional[date] = None


@dataclass(frozen=True)
class Financials:
    """One reporting period, normalised across sources.

    Sector-specific fields sit alongside the general ones rather than in
    subclasses: a REIT still has revenue and an insurer still has cash, and the
    valuation modules ask only for what their own path needs.
    """

    period_end: date
    period_type: PeriodType
    filed_date: Optional[date] = None

    # Income statement.
    revenue: Optional[float] = None
    gross_profit: Optional[float] = None
    operating_income: Optional[float] = None
    ebitda: Optional[float] = None
    net_income: Optional[float] = None
    eps_diluted: Optional[float] = None
    interest_expense: Optional[float] = None
    tax_expense: Optional[float] = None
    pretax_income: Optional[float] = None
    depreciation_amortisation: Optional[float] = None

    # Balance sheet.
    current_assets: Optional[float] = None
    current_liabilities: Optional[float] = None
    cash_and_equivalents: Optional[float] = None
    short_term_investments: Optional[float] = None
    total_debt: Optional[float] = None
    total_assets: Optional[float] = None
    total_liabilities: Optional[float] = None
    total_equity: Optional[float] = None
    goodwill_and_intangibles: Optional[float] = None
    shares_diluted: Optional[float] = None

    # Cash flow.
    operating_cash_flow: Optional[float] = None
    capital_expenditure: Optional[float] = None
    free_cash_flow: Optional[float] = None
    dividends_paid: Optional[float] = None
    share_repurchase: Optional[float] = None

    # REIT (Module B4).
    ffo: Optional[float] = None
    affo: Optional[float] = None
    real_estate_depreciation: Optional[float] = None
    gains_on_property_sales: Optional[float] = None
    recurring_capex: Optional[float] = None
    straight_line_rent_adjustment: Optional[float] = None

    # Bank (Module B3).
    tangible_common_equity: Optional[float] = None
    net_interest_income: Optional[float] = None

    # Insurer (Module B5).
    earned_premium: Optional[float] = None
    losses_and_lae_incurred: Optional[float] = None
    underwriting_expense: Optional[float] = None
    net_investment_income: Optional[float] = None
    unrealised_investment_gains: Optional[float] = None
    realised_investment_gains: Optional[float] = None

    @property
    def net_debt(self) -> Optional[float]:
        if self.total_debt is None or self.cash_and_equivalents is None:
            return None
        liquid = self.cash_and_equivalents + (self.short_term_investments or 0.0)
        return self.total_debt - liquid

    @property
    def tangible_book_value(self) -> Optional[float]:
        if self.total_equity is None:
            return None
        if self.tangible_common_equity is not None:
            return self.tangible_common_equity
        if self.goodwill_and_intangibles is None:
            return None
        return self.total_equity - self.goodwill_and_intangibles

    @property
    def computed_free_cash_flow(self) -> Optional[float]:
        if self.free_cash_flow is not None:
            return self.free_cash_flow
        if self.operating_cash_flow is None or self.capital_expenditure is None:
            return None
        # capex is reported negative by most sources; normalise to a subtraction.
        return self.operating_cash_flow - abs(self.capital_expenditure)


@dataclass(frozen=True)
class FilingFlags:
    """Module A4 inputs. ``None`` means the source could not tell us, which is
    a data gap, not a clean bill of health."""

    restatement_within_lookback: Optional[bool] = None
    auditor_change_within_lookback: Optional[bool] = None
    auditor_change_reason: Optional[str] = None
    going_concern_language: Optional[bool] = None
    delayed_filing: Optional[bool] = None
    as_of: Optional[date] = None
    # Provenance, so a human reading a PASS can see which source answered and
    # on what evidence. A4 is the gate most likely to be wrong for boring
    # reasons, and "which filing said so" is the first question worth asking.
    audit_opinion: Optional[str] = None
    evidence: tuple = ()

    def merge(self, other: "FilingFlags") -> "FilingFlags":
        """Overlay ``other`` onto self, with known beating unknown.

        Neither source is trusted to say "clean" about a field it does not
        carry: a ``None`` never overwrites a real answer, and a real answer
        always wins over ``None``. Where both sources have an opinion, the
        overlay wins, because it is the more specific source by construction.
        """

        def pick(a, b):
            return b if b is not None else a

        return FilingFlags(
            restatement_within_lookback=pick(
                self.restatement_within_lookback, other.restatement_within_lookback
            ),
            auditor_change_within_lookback=pick(
                self.auditor_change_within_lookback, other.auditor_change_within_lookback
            ),
            auditor_change_reason=pick(self.auditor_change_reason, other.auditor_change_reason),
            going_concern_language=pick(self.going_concern_language, other.going_concern_language),
            delayed_filing=pick(self.delayed_filing, other.delayed_filing),
            as_of=pick(self.as_of, other.as_of),
            audit_opinion=pick(self.audit_opinion, other.audit_opinion),
            evidence=tuple(self.evidence) + tuple(other.evidence),
        )


@dataclass(frozen=True)
class MultiplePoint:
    observed_on: date
    value: float


@dataclass(frozen=True)
class MultipleSeries:
    """Module C1 input: the classification-appropriate multiple through time."""

    symbol: str
    multiple_name: str
    points: Sequence[MultiplePoint] = field(default_factory=tuple)

    @property
    def years_covered(self) -> float:
        if len(self.points) < 2:
            return 0.0
        span = self.points[-1].observed_on - self.points[0].observed_on
        return span.days / 365.25


@dataclass(frozen=True)
class PricePoint:
    observed_on: date
    close: float


@dataclass(frozen=True)
class Estimates:
    """Forward figures. Used for PEGY (C3) and forward multiples only -- never
    to relax a gate."""

    forward_eps: Optional[float] = None
    forward_eps_growth_pct: Optional[float] = None
    forward_revenue: Optional[float] = None
    dividend_yield_pct: Optional[float] = None
    next_earnings_date: Optional[date] = None
    as_of: Optional[date] = None


@dataclass(frozen=True)
class CandidateData:
    """Everything the pipeline needs about one company, assembled once."""

    symbol: str
    profile: CompanyProfile
    quarterly: Sequence[Financials] = field(default_factory=tuple)
    annual: Sequence[Financials] = field(default_factory=tuple)
    filing_flags: FilingFlags = field(default_factory=FilingFlags)
    estimates: Estimates = field(default_factory=Estimates)
    multiple_series: Optional[MultipleSeries] = None
    price_history: Sequence[PricePoint] = field(default_factory=tuple)
    peer_symbols: Sequence[str] = field(default_factory=tuple)
    # Populated for the Module A2 sector-relative leverage test and the
    # Module C2 peer anchor.
    sector_net_debt_ebitda: Sequence[float] = field(default_factory=tuple)
    as_of: Optional[date] = None


@dataclass(frozen=True)
class GateOutcome:
    """One gate's result, with the values that produced it.

    ``detail`` is what makes a run auditable: a human must be able to see
    exactly which numbers earned the pass, not merely that one was earned.
    """

    gate: str
    verdict: Verdict
    reason: str
    detail: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.verdict is Verdict.PASS
