"""A synthetic multi-year market, for testing the engine.

Live data is unreachable from this environment, and a backtest engine that has
never actually run is not a backtest engine.  So this builds a deterministic
world with a known shape: companies whose fundamentals and prices are generated
from fixed seeds, a benchmark, and a crash in the middle.

Two properties matter more than realism:

* **Determinism.**  The same seed produces the same history, so a test can
  assert on outcomes rather than on ranges.
* **Point-in-time honesty.**  Quarterly filings carry filing dates ~45 days
  after period end, so an engine with a lookahead bug will visibly outperform
  one without — which is what makes the lookahead test possible at all.
"""

from __future__ import annotations

import dataclasses
import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Sequence

from ..data.fixtures import FixtureAdapter, FixtureCompany
from ..types import (
    CompanyProfile,
    MarketData,
    PeriodFinancials,
    PricePoint,
    ReportingFrequency,
    TaxonomyLevel,
)

#: Filing lag. A December quarter is not public until mid-February, and the
#: engine must not see it before then.
FILING_LAG_DAYS = 45


@dataclass(frozen=True)
class SyntheticCompany:
    """The parameters that generate one company's history."""

    symbol: str
    industry: str
    sector: str
    #: Annual revenue growth.
    growth: float
    #: Net margin.
    margin: float
    #: Starting revenue.
    revenue: float
    shares: float
    #: Starting price expressed as a P/E on year-one earnings.  Setting
    #: valuation directly rather than picking a price level is what gives the
    #: screen something to discriminate on: a market where everything is
    #: expensive tests only that the gate says no.
    start_pe: float
    #: Annual price drift.
    drift: float
    volatility: float
    seed: int
    is_bank: bool = False
    is_reit: bool = False
    is_insurer: bool = False


def _quarter_ends(start: date, end: date) -> list[date]:
    out = []
    year = start.year
    while year <= end.year:
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            q = date(year, month, day)
            if start <= q <= end:
                out.append(q)
        year += 1
    return out


def _year_ends(start: date, end: date) -> list[date]:
    return [
        date(y, 12, 31)
        for y in range(start.year, end.year + 1)
        if start <= date(y, 12, 31) <= end
    ]


def build_company(
    spec: SyntheticCompany, start: date, end: date
) -> FixtureCompany:
    """Generate one company's filings and prices."""
    rng = random.Random(spec.seed)
    # Price the company off its own earnings, so start_pe means what it says.
    start_price = spec.start_pe * (spec.revenue * spec.margin) / spec.shares

    profile = CompanyProfile(
        symbol=spec.symbol,
        name=f"{spec.symbol} Corp",
        listing_currency="USD",
        reporting_currency="USD",
        exchange="NYSE",
        country="US",
        beta=1.0,
        sector=spec.sector,
        industry=spec.industry,
        taxonomy_level=TaxonomyLevel.INDUSTRY,
        taxonomy_is_gics=False,
        is_bank=spec.is_bank,
        is_reit=spec.is_reit,
        is_insurer=spec.is_insurer,
        reporting_frequency=ReportingFrequency.QUARTERLY,
    )

    # -- quarterly filings, newest first --------------------------------
    quarters: list[PeriodFinancials] = []
    for period_end in reversed(_quarter_ends(start, end)):
        years_elapsed = (period_end - start).days / 365.25
        revenue = spec.revenue * (1 + spec.growth) ** years_elapsed / 4.0
        net_income = revenue * spec.margin
        quarters.append(
            PeriodFinancials(
                period_end=period_end,
                filing_date=period_end + timedelta(days=FILING_LAG_DAYS),
                revenue=revenue,
                gross_profit=revenue * 0.38,
                operating_income=revenue * (spec.margin + 0.04),
                net_income=net_income,
                ebitda=revenue * (spec.margin + 0.09),
                interest_expense=revenue * 0.01,
                tax_expense=max(net_income, 0.0) * 0.21,
                pretax_income=net_income * 1.27 if net_income > 0 else net_income,
                operating_cash_flow=net_income * 1.35,
                capital_expenditure=-revenue * 0.03,
                free_cash_flow=net_income * 1.35 - revenue * 0.03,
                total_assets=revenue * 8.0,
                total_current_assets=revenue * 3.0,
                total_current_liabilities=revenue * 2.0,
                total_debt=revenue * 0.3,
                cash_and_equivalents=revenue * 0.2,
                total_equity=revenue * 3.5,
                tangible_book_value=revenue * 3.0,
                shares_diluted=spec.shares,
                shares_outstanding=spec.shares,
                currency="USD",
            )
        )

    # -- annual filings --------------------------------------------------
    annuals: list[PeriodFinancials] = []
    for period_end in reversed(_year_ends(start, end)):
        years_elapsed = (period_end - start).days / 365.25
        revenue = spec.revenue * (1 + spec.growth) ** years_elapsed
        net_income = revenue * spec.margin
        annuals.append(
            PeriodFinancials(
                period_end=period_end,
                filing_date=period_end + timedelta(days=FILING_LAG_DAYS),
                fiscal_year=period_end.year,
                fiscal_period="FY",
                revenue=revenue,
                gross_profit=revenue * 0.38,
                operating_income=revenue * (spec.margin + 0.04),
                net_income=net_income,
                ebitda=revenue * (spec.margin + 0.09),
                interest_expense=revenue * 0.01,
                tax_expense=max(net_income, 0.0) * 0.21,
                pretax_income=net_income * 1.27 if net_income > 0 else net_income,
                operating_cash_flow=net_income * 1.35,
                capital_expenditure=-revenue * 0.03,
                free_cash_flow=net_income * 1.35 - revenue * 0.03,
                total_assets=revenue * 8.0,
                total_current_assets=revenue * 3.0,
                total_current_liabilities=revenue * 2.0,
                total_debt=revenue * 0.3,
                cash_and_equivalents=revenue * 0.2,
                total_equity=revenue * 3.5,
                tangible_book_value=revenue * 3.0,
                invested_capital=revenue * 4.5,
                shares_diluted=spec.shares,
                shares_outstanding=spec.shares,
                currency="USD",
            )
        )

    # -- daily prices ----------------------------------------------------
    prices: list[PricePoint] = []
    price = start_price
    day = start
    daily_drift = (1 + spec.drift) ** (1 / 252.0) - 1
    while day <= end:
        if day.weekday() < 5:
            shock = rng.gauss(0, spec.volatility / (252 ** 0.5))
            price = max(price * (1 + daily_drift + shock), 0.01)
            prices.append(
                PricePoint(
                    price_date=day,
                    close=price,
                    adjusted_close=price,
                    volume=2_000_000.0,
                )
            )
        day += timedelta(days=1)
    prices.reverse()

    return FixtureCompany(
        profile=profile, annual=annuals, quarterly=quarters, prices=prices
    )


def build_benchmark(
    symbol: str, start: date, end: date, *, drift: float = 0.07, seed: int = 99
) -> FixtureCompany:
    """An index series, used as the buy-and-hold benchmark."""
    return build_company(
        SyntheticCompany(
            symbol=symbol, industry="Index", sector="Index",
            growth=0.05, margin=0.10, revenue=1e9, shares=1e9,
            start_pe=18.0, drift=drift, volatility=0.15, seed=seed,
        ),
        start,
        end,
    )


#: A small market: eight industrials with differing quality and valuation, so
#: A2's grouping has enough computable members and C2 can assemble peer sets.
class SyntheticAdapter(FixtureAdapter):
    """A fixture adapter that behaves like the real composite adapter.

    Two behaviours matter for the backtest to mean anything:

    * **Market cap and ADV are derived** from price and share count, as
      :class:`~gcfp.data.composite.CompositeAdapter` does, rather than
      hard-coded — so the universe screen exercises the production path,
      including the part where a name with no price has no market cap.
    * **``as_of`` is honoured.** Without it the fixture would hand the engine
      filings from the future and the backtest would carry the exact lookahead
      bias the design exists to prevent — which shows up as a *better* result,
      not as an error.
    """

    name: str = "synthetic"
    #: Restricts every fact to what had been filed by this date.
    as_of: date | None = None

    def get_quarterly_financials(self, symbol: str, quarters: int):
        rows = super().get_quarterly_financials(symbol, 10_000)
        return tuple(self._filed_by(rows))[:quarters]

    def get_annual_financials(self, symbol: str, years: int):
        rows = super().get_annual_financials(symbol, 10_000)
        return tuple(self._filed_by(rows))[:years]

    def get_historical_multiples(self, symbol: str, multiple: str, years: int):
        """Reconstruct C1's series exactly as the composite adapter does.

        Sharing the implementation is the point: a fixture deriving the series
        differently would test a path production never takes.
        """
        from ..data.adapter import DataUnavailable
        from ..data.reconstruct import reconstruct_series

        end = self.as_of or date.today()
        quarters = list(self.get_quarterly_financials(symbol, (years + 1) * 4))
        prices = list(
            super().get_prices(
                symbol, end - timedelta(days=int(365.25 * (years + 1))), end
            )
        )
        observations = reconstruct_series(
            quarters, prices, multiple, end=end, years=years
        )
        if not observations:
            raise DataUnavailable(
                "historical_multiples",
                f"could not reconstruct a {multiple} series for {symbol}",
            )
        return tuple(observations)

    def _filed_by(self, rows):
        if self.as_of is None:
            return rows
        return [r for r in rows if (r.filing_date or r.period_end) <= self.as_of]

    def load_company(self, symbol: str, **kw):
        data = super().load_company(symbol, **kw)
        if data.current_price is None or not data.quarterly:
            return data

        latest = data.quarterly[0]
        shares = latest.shares_diluted or latest.shares_outstanding
        if not shares:
            return data

        from ..data.prices import average_dollar_volume

        return dataclasses.replace(
            data,
            profile=dataclasses.replace(
                data.profile,
                market_cap=data.current_price * shares,
                adv_3m_usd=average_dollar_volume(data.prices),
                beta=1.0,
            ),
        )


DEFAULT_MARKET: tuple[SyntheticCompany, ...] = tuple(
    SyntheticCompany(
        symbol=f"IND{i}",
        industry="Machinery",
        sector="Industrials",
        growth=0.04 + 0.01 * (i % 4),
        margin=0.08 + 0.01 * (i % 3),
        # Revenue and share count are kept close across the market so that
        # market caps stay inside C2's 0.3-3x size band. A market whose caps
        # span 40x would leave every name without four genuine peers, and the
        # backtest would exercise only the single-anchor path.
        revenue=5e9 + 0.3e9 * i,
        shares=250e6,
        # A spread from genuinely cheap to plainly expensive, so the screen has
        # to choose rather than reject everything or accept everything.
        start_pe=(8.0, 9.0, 11.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0)[i],
        drift=0.05 + 0.01 * (i % 3),
        volatility=0.22,
        seed=100 + i,
    )
    for i in range(9)
)


def build_synthetic_market(
    start: date,
    end: date,
    companies: Sequence[SyntheticCompany] = DEFAULT_MARKET,
    *,
    benchmark_symbol: str = "^GSPC",
) -> FixtureAdapter:
    """A complete adapter over a generated multi-year market."""
    adapter = SyntheticAdapter(name="synthetic")
    for spec in companies:
        adapter.add(build_company(spec, start, end))
    adapter.add(build_benchmark(benchmark_symbol, start, end))

    adapter.market = MarketData(
        risk_free_rate=0.035,
        risk_free_rate_date=end,
        group_net_debt_ebitda_median={"Machinery": 1.5, "Industrials": 1.6},
        group_member_counts={"Machinery": len(companies), "Industrials": len(companies)},
        group_gross_margin_stdev_median={"Machinery": 0.02},
    )
    # A synthetic source has no forward estimates and no corporate actions,
    # matching the free stack's real shape.
    adapter.unsupported = frozenset(
        {"gics_sub_industry", "forward_estimates", "corporate_actions"}
    )
    return adapter


__all__ = [
    "SyntheticAdapter",
    "SyntheticCompany",
    "DEFAULT_MARKET",
    "FILING_LAG_DAYS",
    "build_company",
    "build_benchmark",
    "build_synthetic_market",
]
