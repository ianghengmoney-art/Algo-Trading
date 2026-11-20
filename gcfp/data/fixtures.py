"""A deterministic, offline adapter.

Two jobs:

* it is what the test suite runs against, so gate arithmetic is verified
  against numbers chosen to sit just either side of each threshold rather than
  against whatever the market happened to do today; and
* it lets the §18 probe be exercised end to end — including its stop
  conditions — in an environment with no market-data egress, which is how you
  find out the probe itself works before trusting its verdict on live data.

Fixtures declare their own capability gaps.  A fixture built without FFO is
telling the probe "this source cannot value a REIT", and the probe should say
so rather than quietly producing a number.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Sequence

from ..types import (
    CompanyData,
    CompanyProfile,
    CorporateAction,
    MarketData,
    MultipleObservation,
    PeriodFinancials,
    PricePoint,
    TaxonomyLevel,
)
from .adapter import Capability, DataAdapter, DataUnavailable, REQUIRED_CAPABILITIES


@dataclass
class FixtureCompany:
    """One company's canned data."""

    profile: CompanyProfile
    annual: list[PeriodFinancials] = field(default_factory=list)
    quarterly: list[PeriodFinancials] = field(default_factory=list)
    prices: list[PricePoint] = field(default_factory=list)
    multiples: dict[str, list[MultipleObservation]] = field(default_factory=dict)
    corporate_actions: list[CorporateAction] = field(default_factory=list)
    peers: list[str] = field(default_factory=list)
    forward_eps_growth: float | None = None
    dividend_yield: float | None = None
    # A4's red-flag inputs live on CompanyData rather than on any statement,
    # so the fixture has to carry them explicitly or A4 can never be exercised.
    restatements: list = field(default_factory=list)
    auditor_events: list = field(default_factory=list)
    going_concern_language: bool = False


@dataclass
class FixtureAdapter(DataAdapter):
    """Serves :class:`FixtureCompany` records and nothing else."""

    companies: dict[str, FixtureCompany] = field(default_factory=dict)
    market: MarketData = field(default_factory=MarketData)
    groups: dict[str, list[str]] = field(default_factory=dict)
    #: Capabilities this fixture set claims *not* to have, so the probe can be
    #: driven through its stop conditions deterministically.
    unsupported: frozenset[str] = frozenset()
    name: str = "fixture"

    def add(self, company: FixtureCompany) -> "FixtureAdapter":
        self.companies[company.profile.symbol] = company
        return self

    def all_tickers(self) -> dict[str, int]:
        """Every symbol this fixture holds.

        Mirrors :meth:`EdgarAdapter.all_tickers` so the universe builder can
        enumerate a fixture set the same way it enumerates real filers.
        """
        return {symbol: i for i, symbol in enumerate(sorted(self.companies))}

    def _get(self, symbol: str) -> FixtureCompany:
        try:
            return self.companies[symbol]
        except KeyError:
            raise DataUnavailable("profile", f"{symbol} not in fixture set") from None

    # -- DataAdapter ------------------------------------------------------
    def get_profile(self, symbol: str) -> CompanyProfile:
        return self._get(symbol).profile

    def get_annual_financials(
        self, symbol: str, years: int
    ) -> Sequence[PeriodFinancials]:
        if "annual_financials" in self.unsupported:
            raise DataUnavailable("annual_financials", "not supplied by this source")
        return tuple(self._get(symbol).annual[:years])

    def get_quarterly_financials(
        self, symbol: str, quarters: int
    ) -> Sequence[PeriodFinancials]:
        if "quarterly_financials" in self.unsupported:
            raise DataUnavailable("quarterly_financials", "not supplied by this source")
        return tuple(self._get(symbol).quarterly[:quarters])

    def get_prices(
        self, symbol: str, start: date, end: date
    ) -> Sequence[PricePoint]:
        if "prices" in self.unsupported:
            raise DataUnavailable("prices", "not supplied by this source")
        return tuple(
            p for p in self._get(symbol).prices if start <= p.price_date <= end
        )

    def get_historical_multiples(
        self, symbol: str, multiple: str, years: int
    ) -> Sequence[MultipleObservation]:
        if "historical_multiples" in self.unsupported:
            raise DataUnavailable("historical_multiples", "not supplied by this source")
        series = self._get(symbol).multiples.get(multiple)
        if series is None:
            raise DataUnavailable(
                "historical_multiples", f"{multiple} not held for {symbol}"
            )
        cutoff = date.today() - timedelta(days=int(365.25 * years))
        return tuple(o for o in series if o.observation_date >= cutoff)

    def get_corporate_actions(
        self, symbol: str, years: int
    ) -> Sequence[CorporateAction]:
        if "corporate_actions" in self.unsupported:
            raise DataUnavailable("corporate_actions", "not supplied by this source")
        cutoff = date.today() - timedelta(days=int(365.25 * years))
        return tuple(
            a for a in self._get(symbol).corporate_actions if a.effective_date >= cutoff
        )

    def get_peer_symbols(self, symbol: str) -> Sequence[str]:
        if "peer_group" in self.unsupported:
            raise DataUnavailable("peer_group", "not supplied by this source")
        return tuple(self._get(symbol).peers)

    def get_group_members(
        self, group: str, level: TaxonomyLevel
    ) -> Sequence[str]:
        return tuple(self.groups.get(group, ()))

    def get_market_data(self) -> MarketData:
        return self.market

    def capabilities(self) -> Sequence[Capability]:
        return tuple(
            Capability(
                name=name,
                supported=name not in self.unsupported,
                detail="withheld by fixture" if name in self.unsupported else "fixture",
            )
            for name in REQUIRED_CAPABILITIES
        )

    def load_company(self, symbol: str, **kw) -> CompanyData:
        data = super().load_company(symbol, **kw)
        fixture = self._get(symbol)
        return replace(
            data,
            forward_eps_growth=fixture.forward_eps_growth,
            dividend_yield=fixture.dividend_yield,
            restatements=tuple(fixture.restatements),
            auditor_events=tuple(fixture.auditor_events),
            going_concern_language=fixture.going_concern_language,
        )


# -- builders ------------------------------------------------------------
# Small helpers so tests can express "a company that clears A1 by a hair"
# without hand-writing forty fields.


def quarters_back(n: int, anchor: date | None = None) -> list[date]:
    """``n`` quarter-end dates, newest first."""
    anchor = anchor or date.today()
    return [anchor - timedelta(days=91 * i) for i in range(n)]


def make_quarters(
    n: int,
    *,
    revenue: float,
    net_income: float,
    operating_cash_flow: float,
    revenue_growth: float = 0.0,
    shares: float | None = 1_000_000_000.0,
    share_growth: float = 0.0,
    anchor: date | None = None,
    **extra,
) -> list[PeriodFinancials]:
    """A quarterly series, newest first, growing backwards at the given rates."""
    out: list[PeriodFinancials] = []
    for i, period_end in enumerate(quarters_back(n, anchor)):
        decay = (1.0 + revenue_growth) ** (-i / 4.0)
        # ``shares=None`` models a source that does not report a share count,
        # which is what A4's dilution flag needs to be exercised against.
        share_decay = (1.0 + share_growth) ** (-i / 4.0) if shares is not None else None
        out.append(
            PeriodFinancials(
                period_end=period_end,
                filing_date=period_end + timedelta(days=30),
                revenue=revenue * decay,
                net_income=net_income * decay,
                operating_cash_flow=operating_cash_flow * decay,
                shares_diluted=shares * share_decay if shares is not None else None,
                shares_outstanding=shares * share_decay if shares is not None else None,
                **extra,
            )
        )
    return out


def make_annuals(
    n: int,
    *,
    revenue: float,
    net_income: float,
    operating_cash_flow: float,
    free_cash_flow: float | None = None,
    revenue_growth: float = 0.05,
    anchor: date | None = None,
    **extra,
) -> list[PeriodFinancials]:
    """An annual series, newest first."""
    anchor = anchor or date.today()
    out: list[PeriodFinancials] = []
    for i in range(n):
        decay = (1.0 + revenue_growth) ** (-i)
        out.append(
            PeriodFinancials(
                period_end=date(anchor.year - i - 1, 12, 31),
                filing_date=date(anchor.year - i, 2, 15),
                fiscal_year=anchor.year - i - 1,
                fiscal_period="FY",
                revenue=revenue * decay,
                net_income=net_income * decay,
                operating_cash_flow=operating_cash_flow * decay,
                free_cash_flow=(
                    free_cash_flow * decay if free_cash_flow is not None else None
                ),
                **extra,
            )
        )
    return out


def make_multiple_series(
    multiple: str,
    values: Sequence[float],
    *,
    anchor: date | None = None,
    step_days: int = 91,
) -> list[MultipleObservation]:
    """A C1 series, newest first, one observation per ``step_days``."""
    anchor = anchor or date.today()
    return [
        MultipleObservation(
            observation_date=anchor - timedelta(days=step_days * i),
            value=v,
            multiple=multiple,
        )
        for i, v in enumerate(values)
    ]


def make_prices(
    n: int,
    *,
    start_price: float,
    daily_drift: float = 0.0,
    anchor: date | None = None,
    volume: float = 5_000_000.0,
) -> list[PricePoint]:
    """A daily price series, newest first."""
    anchor = anchor or date.today()
    out: list[PricePoint] = []
    for i in range(n):
        price = start_price * (1.0 + daily_drift) ** (-i)
        out.append(
            PricePoint(
                price_date=anchor - timedelta(days=i),
                close=price,
                adjusted_close=price,
                volume=volume,
            )
        )
    return out


__all__ = [
    "FixtureAdapter",
    "FixtureCompany",
    "make_quarters",
    "make_annuals",
    "make_multiple_series",
    "make_prices",
    "quarters_back",
]
