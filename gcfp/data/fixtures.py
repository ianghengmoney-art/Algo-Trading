"""Deterministic fixture adapter.

Exists so the whole pipeline -- gates, valuation, triangulation, scoring,
sizing, reporting -- can be exercised end to end without a paid data
subscription and without network access, and so that tests assert on known
answers rather than on whatever the market did today.

The companies here are synthetic. They are shaped to sit on known sides of the
gates (one archetype per classification, plus deliberate failures), which is
exactly what makes them useful for testing and useless for investing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional, Sequence

from ..types import (
    CompanyProfile,
    Estimates,
    Financials,
    FilingFlags,
    MultiplePoint,
    MultipleSeries,
    PeriodType,
    PricePoint,
)
from .base import Capability, DataAdapter, DataUnavailable

ANCHOR_DATE = date(2026, 6, 30)


@dataclass
class FixtureSpec:
    symbol: str
    name: str
    sector: str
    base_revenue: float
    revenue_growth_pct: float
    net_margin_pct: float
    ocf_to_ni: float = 1.15
    capex_pct_of_revenue: float = 4.0
    current_ratio: float = 1.8
    debt_to_ebitda: float = 1.5
    beta: float = 1.0
    price: float = 100.0
    shares: float = 100_000_000.0
    share_growth_pct: float = -1.5
    gross_margin_pct: float = 45.0
    years: int = 11
    is_bank: bool = False
    is_reit: bool = False
    is_insurer: bool = False
    is_adr: bool = False
    exchange: str = "NYSE"
    country: str = "US"
    multiple_name: str = "trailing P/E"
    multiple_history_years: float = 10.0
    # Where today's multiple sits against its own history: 1.4 means the
    # historical median is 40% above today, i.e. the name looks cheap on C1.
    history_median_ratio: float = 1.4
    multiple_step_change: bool = False
    stale_months: float = 0.0
    filing_flags: FilingFlags = field(
        default_factory=lambda: FilingFlags(False, False, None, False, False, ANCHOR_DATE)
    )
    dividend_yield_pct: Optional[float] = None
    next_earnings_in_days: Optional[int] = 45
    forward_eps_growth_pct: Optional[float] = 8.0


def _period(
    spec: FixtureSpec,
    period_end: date,
    revenue: float,
    kind: PeriodType,
    shares: float,
    annual_revenue: float,
) -> Financials:
    """One reporting period.

    Income and cash-flow lines scale with ``revenue`` (the period's own flow);
    balance-sheet lines scale with ``annual_revenue``, because a stock does not
    shrink to a quarter of itself just because the period is shorter. Getting
    this backwards makes every quarterly cash-runway figure a quarter of its
    true value, which is exactly the kind of silent error the pre-profit branch
    of gate A3 would then act on.
    """
    net_income = revenue * spec.net_margin_pct / 100.0
    ebitda = revenue * max(spec.net_margin_pct + 8.0, 4.0) / 100.0
    annual_ebitda = annual_revenue * max(spec.net_margin_pct + 8.0, 4.0) / 100.0
    ocf = net_income * spec.ocf_to_ni
    capex = -revenue * spec.capex_pct_of_revenue / 100.0

    debt = max(annual_ebitda * spec.debt_to_ebitda, 0.0)
    cash = annual_revenue * 0.20
    current_liabilities = annual_revenue * 0.25
    current_assets = current_liabilities * spec.current_ratio
    equity = max(annual_revenue * 0.8, 1.0)

    extra: dict = {}
    if spec.is_reit:
        extra.update(
            {
                "real_estate_depreciation": revenue * 0.28,
                "gains_on_property_sales": revenue * 0.02,
                "recurring_capex": -revenue * 0.06,
                "straight_line_rent_adjustment": revenue * 0.01,
            }
        )
    if spec.is_bank:
        extra.update(
            {
                "tangible_common_equity": equity * 0.88,
                "net_interest_income": revenue * 0.65,
            }
        )
    if spec.is_insurer:
        extra.update(
            {
                "earned_premium": revenue * 0.85,
                "losses_and_lae_incurred": revenue * 0.55,
                "underwriting_expense": revenue * 0.24,
                "net_investment_income": revenue * 0.12,
                "unrealised_investment_gains": net_income * 0.10,
            }
        )

    return Financials(
        period_end=period_end,
        period_type=kind,
        filed_date=period_end + timedelta(days=45),
        revenue=revenue,
        gross_profit=revenue * spec.gross_margin_pct / 100.0,
        operating_income=revenue * (spec.net_margin_pct + 4.0) / 100.0,
        ebitda=ebitda,
        net_income=net_income,
        eps_diluted=net_income / shares if shares else None,
        pretax_income=net_income / 0.79,
        tax_expense=net_income / 0.79 * 0.21,
        depreciation_amortisation=revenue * 0.05,
        current_assets=current_assets,
        current_liabilities=current_liabilities,
        cash_and_equivalents=cash,
        short_term_investments=cash * 0.25,
        total_debt=debt,
        total_assets=annual_revenue * 1.6,
        total_equity=equity,
        goodwill_and_intangibles=equity * 0.12,
        shares_diluted=shares,
        operating_cash_flow=ocf,
        capital_expenditure=capex,
        free_cash_flow=ocf + capex,
        dividends_paid=-(revenue * 0.02) if spec.dividend_yield_pct else 0.0,
        **extra,
    )


class FixtureAdapter(DataAdapter):
    name = "fixtures"

    def __init__(self, specs: Optional[Sequence[FixtureSpec]] = None, as_of: date = ANCHOR_DATE):
        self.as_of = as_of
        self._specs = {s.symbol: s for s in (specs if specs is not None else default_specs())}

    def capabilities(self) -> set[Capability]:
        return {
            Capability.PROFILE,
            Capability.ANNUAL_STATEMENTS,
            Capability.QUARTERLY_STATEMENTS,
            Capability.SHARE_COUNT_HISTORY,
            Capability.CASH_BURN_HISTORY,
            Capability.FILING_FLAGS,
            Capability.PRICE_HISTORY,
            Capability.MULTIPLE_HISTORY,
            Capability.PEER_LIST,
            Capability.SECTOR_AGGREGATES,
            Capability.ANALYST_ESTIMATES,
            Capability.EARNINGS_CALENDAR,
            Capability.REIT_FFO,
            Capability.INSURER_UNDERWRITING,
            Capability.BANK_TANGIBLE_BOOK,
            Capability.POINT_IN_TIME,
        }

    def symbols(self) -> list[str]:
        return sorted(self._specs)

    def _spec(self, symbol: str) -> FixtureSpec:
        if symbol not in self._specs:
            raise DataUnavailable(symbol, "company", "not in the fixture set")
        return self._specs[symbol]

    def get_profile(self, symbol: str) -> CompanyProfile:
        spec = self._spec(symbol)
        return CompanyProfile(
            symbol=spec.symbol,
            name=spec.name,
            sector=spec.sector,
            industry=spec.sector,
            exchange=spec.exchange,
            country=spec.country,
            currency="USD",
            market_cap=spec.price * spec.shares,
            price=spec.price,
            beta=spec.beta,
            avg_daily_dollar_volume=max(spec.price * spec.shares * 0.004, 1_000_000.0),
            shares_outstanding=spec.shares,
            is_adr=spec.is_adr,
            is_etf=False,
            is_fund=False,
            is_actively_trading=True,
            ipo_date=date(2005, 1, 1),
            is_reit=spec.is_reit,
            is_bank=spec.is_bank,
            is_insurer=spec.is_insurer,
            as_of=self.as_of,
        )

    def get_annual_financials(self, symbol: str, years: int = 10) -> Sequence[Financials]:
        spec = self._spec(symbol)
        out: list[Financials] = []
        latest_year_end = date(self.as_of.year - 1, 12, 31)
        for i in range(min(years, spec.years)):
            revenue = spec.base_revenue / ((1.0 + spec.revenue_growth_pct / 100.0) ** i)
            shares = spec.shares / ((1.0 + spec.share_growth_pct / 100.0) ** i)
            out.append(
                _period(
                    spec,
                    date(latest_year_end.year - i, 12, 31),
                    revenue,
                    PeriodType.ANNUAL,
                    shares,
                    annual_revenue=revenue,
                )
            )
        return out

    def get_quarterly_financials(self, symbol: str, quarters: int = 12) -> Sequence[Financials]:
        spec = self._spec(symbol)
        out: list[Financials] = []
        anchor = self.as_of - timedelta(days=int(spec.stale_months * 30.4375))
        for i in range(quarters):
            period_end = anchor - timedelta(days=91 * i)
            annual_revenue = spec.base_revenue / ((1.0 + spec.revenue_growth_pct / 100.0) ** (i / 4.0))
            revenue = annual_revenue / 4.0
            shares = spec.shares / ((1.0 + spec.share_growth_pct / 100.0) ** (i / 4.0))
            out.append(
                _period(
                    spec, period_end, revenue, PeriodType.QUARTER, shares, annual_revenue=annual_revenue
                )
            )
        return out

    def get_filing_flags(self, symbol: str) -> FilingFlags:
        return self._spec(symbol).filing_flags

    def get_price_history(self, symbol: str, years: int = 10) -> Sequence[PricePoint]:
        spec = self._spec(symbol)
        points: list[PricePoint] = []
        months = int(years * 12)
        for i in range(months):
            observed = self.as_of - timedelta(days=30 * (months - 1 - i))
            drift = (1.0 + spec.revenue_growth_pct / 100.0 / 12.0) ** i
            points.append(PricePoint(observed, spec.price / drift * (1.0 + 0.01 * ((i % 5) - 2))))
        points.append(PricePoint(self.as_of, spec.price))
        return points

    def get_multiple_series(self, symbol: str, multiple_name: str, years: int = 10) -> MultipleSeries:
        """History built around the company's own computed current multiple.

        The series is anchored to what ``engine.current_multiple`` actually
        derives from these statements, scaled by ``history_median_ratio``. A
        fixture whose synthetic history is unrelated to its synthetic financials
        would make every C4 divergence test measure the inconsistency of the
        fixture rather than the behaviour of the code.
        """
        from ..engine import current_multiple as _current_multiple
        from ..modules.a_health import build_trailing_window

        spec = self._spec(symbol)
        n = int(spec.multiple_history_years * 4)
        if n < 4:
            raise DataUnavailable(symbol, f"{multiple_name} history", "series too short")

        candidate = self.load_candidate(symbol)
        classification = _CLASSIFICATION_FOR_MULTIPLE.get(spec.multiple_name)
        window = build_trailing_window(candidate)
        anchor = _current_multiple(candidate, classification, window) if classification else None
        if anchor is None or anchor <= 0:
            raise DataUnavailable(symbol, f"{multiple_name} history", "current multiple undefined")

        median_level = anchor * spec.history_median_ratio
        points: list[MultiplePoint] = []
        for i in range(n):
            observed = self.as_of - timedelta(days=91 * (n - 1 - i))
            level = median_level
            if spec.multiple_step_change and i >= n // 2:
                level = median_level * 1.9
            wobble = 1.0 + 0.05 * ((i % 7) - 3) / 3.0
            points.append(MultiplePoint(observed, level * wobble))
        return MultipleSeries(symbol, spec.multiple_name, tuple(points))

    def get_peers(self, symbol: str) -> Sequence[str]:
        spec = self._spec(symbol)
        return [s.symbol for s in self._specs.values() if s.sector == spec.sector and s.symbol != symbol]

    def get_estimates(self, symbol: str) -> Estimates:
        spec = self._spec(symbol)
        next_earnings = (
            self.as_of + timedelta(days=spec.next_earnings_in_days)
            if spec.next_earnings_in_days is not None
            else None
        )
        return Estimates(
            forward_eps_growth_pct=spec.forward_eps_growth_pct,
            forward_revenue=spec.base_revenue * (1.0 + spec.revenue_growth_pct / 100.0),
            dividend_yield_pct=spec.dividend_yield_pct,
            next_earnings_date=next_earnings,
            as_of=self.as_of,
        )

    def get_sector_net_debt_ebitda(self, sector: str) -> Sequence[float]:
        peers = [s for s in self._specs.values() if s.sector == sector]
        if len(peers) < 2:
            raise DataUnavailable(sector, "sector leverage aggregates", "too few sector members")
        return [s.debt_to_ebitda for s in peers]


_CLASSIFICATION_FOR_MULTIPLE = {
    "trailing P/E": "CORE-STABLE",
    "forward P/E": "CORE-GROWTH",
    "EV/Revenue": "SPEC-GROWTH",
    "P/B": "FINANCIAL-BANK",
    "P/AFFO": "REIT",
}


def default_specs() -> list[FixtureSpec]:
    """One archetype per classification, plus deliberate failures."""
    industrial_peers = [
        FixtureSpec(
            symbol=f"IND{i}",
            name=f"Industrial Peer {i}",
            sector="Industrials",
            base_revenue=8_000_000_000.0 * (0.8 + 0.1 * i),
            revenue_growth_pct=6.0 + i * 0.5,
            net_margin_pct=11.0,
            beta=1.1,
            price=90.0 + i * 5,
            shares=90_000_000.0,
            dividend_yield_pct=2.0,
        )
        for i in range(1, 6)
    ]
    tech_peers = [
        FixtureSpec(
            symbol=f"TCH{i}",
            name=f"Software Peer {i}",
            sector="Technology",
            base_revenue=2_000_000_000.0 * (0.9 + 0.1 * i),
            revenue_growth_pct=24.0 + i * 0.4,
            net_margin_pct=15.0,
            gross_margin_pct=76.0,
            capex_pct_of_revenue=3.0,
            beta=1.4,
            price=68.0 + 2 * i,
            shares=120_000_000.0,
            multiple_name="forward P/E",
        )
        for i in range(1, 6)
    ]
    bank_peers = [
        FixtureSpec(
            symbol=f"BNK{i}",
            name=f"Regional Bank {i}",
            sector="Financial Services",
            base_revenue=5_000_000_000.0 * (0.85 + 0.08 * i),
            revenue_growth_pct=5.0,
            net_margin_pct=22.0,
            is_bank=True,
            beta=1.0,
            price=60.0 + 3 * i,
            shares=200_000_000.0,
            multiple_name="P/B",
            dividend_yield_pct=3.2,
        )
        for i in range(1, 6)
    ]
    reit_peers = [
        FixtureSpec(
            symbol=f"RET{i}",
            name=f"Net Lease REIT {i}",
            sector="Real Estate",
            base_revenue=1_200_000_000.0 * (0.85 + 0.08 * i),
            revenue_growth_pct=5.0,
            net_margin_pct=18.0,
            is_reit=True,
            capex_pct_of_revenue=8.0,
            beta=0.85,
            price=55.0 + 2 * i,
            shares=180_000_000.0,
            multiple_name="P/AFFO",
            dividend_yield_pct=5.4,
        )
        for i in range(1, 6)
    ]
    insurer_peers = [
        FixtureSpec(
            symbol=f"INS{i}",
            name=f"P&C Insurer {i}",
            sector="Insurance",
            base_revenue=9_000_000_000.0 * (0.85 + 0.08 * i),
            revenue_growth_pct=7.0,
            net_margin_pct=10.0,
            is_insurer=True,
            beta=0.9,
            price=180.0 + 6 * i,
            shares=110_000_000.0,
            multiple_name="P/B",
            dividend_yield_pct=2.1,
        )
        for i in range(1, 6)
    ]

    # Small-cap software, sized and growing like BURNER so the SPEC-GROWTH
    # path has a genuine peer set rather than an empty one.
    small_tech_peers = [
        FixtureSpec(
            symbol=f"SML{i}",
            name=f"Small Software Peer {i}",
            sector="Technology",
            base_revenue=550_000_000.0 * (0.9 + 0.1 * i),
            revenue_growth_pct=30.0 + i * 0.5,
            net_margin_pct=-8.0,
            ocf_to_ni=0.6,
            gross_margin_pct=64.0,
            capex_pct_of_revenue=5.0,
            beta=1.7,
            price=32.0 + 2 * i,
            shares=135_000_000.0,
            share_growth_pct=2.5,
            multiple_name="EV/Revenue",
            forward_eps_growth_pct=None,
        )
        for i in range(1, 6)
    ]
    # Mega-cap technology, the only band an ADR of ADRCO's size can be
    # legitimately compared against.
    mega_tech_peers = [
        FixtureSpec(
            symbol=f"MEG{i}",
            name=f"Mega Cap Tech {i}",
            sector="Technology",
            base_revenue=28_000_000_000.0 * (0.9 + 0.08 * i),
            revenue_growth_pct=17.0 + i * 0.4,
            net_margin_pct=28.0,
            gross_margin_pct=56.0,
            capex_pct_of_revenue=20.0,
            beta=1.25,
            price=175.0 + 6 * i,
            shares=520_000_000.0,
            multiple_name="forward P/E",
        )
        for i in range(1, 6)
    ]

    subjects = [
        # CORE-STABLE, trading well below its own history and its peers.
        FixtureSpec(
            symbol="MATURE",
            name="Mature Industrial Co",
            sector="Industrials",
            base_revenue=9_000_000_000.0,
            revenue_growth_pct=7.0,
            net_margin_pct=12.0,
            beta=1.05,
            price=70.0,
            shares=95_000_000.0,
            dividend_yield_pct=2.6,
            history_median_ratio=1.65,
        ),
        # CORE-GROWTH: profitable fast-grower clearing the Rule of 40.
        FixtureSpec(
            symbol="FASTPROF",
            name="Profitable Compounder Inc",
            sector="Technology",
            base_revenue=2_400_000_000.0,
            revenue_growth_pct=26.0,
            net_margin_pct=18.0,
            gross_margin_pct=78.0,
            capex_pct_of_revenue=3.0,
            ocf_to_ni=1.30,
            beta=1.35,
            price=58.0,
            shares=130_000_000.0,
            multiple_name="forward P/E",
            forward_eps_growth_pct=25.0,
            history_median_ratio=1.55,
        ),
        # SPEC-GROWTH: unprofitable but funded.
        FixtureSpec(
            symbol="BURNER",
            name="Scaling Unprofitable Co",
            sector="Technology",
            base_revenue=600_000_000.0,
            revenue_growth_pct=32.0,
            net_margin_pct=-12.0,
            ocf_to_ni=0.55,
            gross_margin_pct=62.0,
            capex_pct_of_revenue=5.0,
            beta=1.8,
            price=30.0,
            shares=140_000_000.0,
            share_growth_pct=3.0,
            multiple_name="EV/Revenue",
            forward_eps_growth_pct=None,
            history_median_ratio=1.5,
        ),
        FixtureSpec(
            symbol="BANKCO",
            name="Large Regional Bank",
            sector="Financial Services",
            base_revenue=5_400_000_000.0,
            revenue_growth_pct=5.0,
            net_margin_pct=24.0,
            is_bank=True,
            beta=1.0,
            price=52.0,
            shares=210_000_000.0,
            multiple_name="P/B",
            dividend_yield_pct=3.6,
            history_median_ratio=1.5,
        ),
        FixtureSpec(
            symbol="REITCO",
            name="Net Lease REIT Co",
            sector="Real Estate",
            base_revenue=1_300_000_000.0,
            revenue_growth_pct=5.0,
            net_margin_pct=19.0,
            is_reit=True,
            capex_pct_of_revenue=8.0,
            beta=0.8,
            price=44.0,
            shares=190_000_000.0,
            multiple_name="P/AFFO",
            dividend_yield_pct=6.1,
            history_median_ratio=1.45,
        ),
        FixtureSpec(
            symbol="INSURCO",
            name="P&C Underwriter Co",
            sector="Insurance",
            base_revenue=9_500_000_000.0,
            revenue_growth_pct=7.0,
            net_margin_pct=11.0,
            is_insurer=True,
            beta=0.88,
            price=150.0,
            shares=115_000_000.0,
            multiple_name="P/B",
            dividend_yield_pct=2.0,
            history_median_ratio=1.5,
        ),
        # Foreign ADR.
        FixtureSpec(
            symbol="ADRCO",
            name="Foreign Listed ADR",
            sector="Technology",
            base_revenue=30_000_000_000.0,
            revenue_growth_pct=18.0,
            net_margin_pct=30.0,
            gross_margin_pct=54.0,
            capex_pct_of_revenue=22.0,
            beta=1.3,
            price=180.0,
            shares=500_000_000.0,
            is_adr=True,
            country="TW",
            multiple_name="forward P/E",
            history_median_ratio=1.1,
        ),
        # Deliberate failures, one per gate that matters most.
        FixtureSpec(
            symbol="BADQUAL",
            name="Cash-Poor Earnings Co",
            sector="Industrials",
            base_revenue=4_000_000_000.0,
            revenue_growth_pct=8.0,
            net_margin_pct=10.0,
            ocf_to_ni=0.45,  # fails A3
            beta=1.2,
            price=55.0,
            shares=80_000_000.0,
        ),
        FixtureSpec(
            symbol="STALECO",
            name="Stale Filings Co",
            sector="Industrials",
            base_revenue=3_000_000_000.0,
            revenue_growth_pct=6.0,
            net_margin_pct=9.0,
            stale_months=20.0,  # fails A5
            price=40.0,
            shares=70_000_000.0,
        ),
        FixtureSpec(
            symbol="FLAGGED",
            name="Going Concern Co",
            sector="Technology",
            base_revenue=800_000_000.0,
            revenue_growth_pct=15.0,
            net_margin_pct=-8.0,
            ocf_to_ni=0.7,
            price=8.0,
            shares=200_000_000.0,
            share_growth_pct=22.0,  # fails A4 on dilution too
            filing_flags=FilingFlags(False, False, None, True, False, ANCHOR_DATE),
            multiple_name="EV/Revenue",
            forward_eps_growth_pct=None,
        ),
        # A re-rated business: own-history anchor is anchored to a company that
        # no longer exists.
        FixtureSpec(
            symbol="RERATED",
            name="Transformed Business Co",
            sector="Technology",
            base_revenue=5_000_000_000.0,
            revenue_growth_pct=22.0,
            net_margin_pct=20.0,
            gross_margin_pct=70.0,
            capex_pct_of_revenue=4.0,
            ocf_to_ni=1.25,
            beta=1.4,
            price=95.0,
            shares=150_000_000.0,
            multiple_name="forward P/E",
            multiple_step_change=True,
            history_median_ratio=1.0,
        ),
    ]
    return (
        subjects
        + industrial_peers
        + tech_peers
        + small_tech_peers
        + mega_tech_peers
        + bank_peers
        + reit_peers
        + insurer_peers
    )
