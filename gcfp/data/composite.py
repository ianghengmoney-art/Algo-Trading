"""Pair a fundamentals source with a price source behind one adapter.

EDGAR has filings and no quotes; Stooq and Yahoo have quotes and no filings.
Neither is a complete :class:`DataAdapter` alone, and the gates should not have
to know that.  This composes them, and fills the three gaps that only exist
once both halves are present:

* **market cap and ADV**, which need a price and a share count together;
* **beta**, regressed from price history rather than bought;
* **C1's multiple history**, which no free source publishes and which is
  reconstructed here from prices and per-share fundamentals.

That reconstruction is where lookahead bias would creep in.  A company's
December quarter is not *knowable* until it is filed in February, so the
trailing multiple on 15 January must use the September quarter.  Matching on
period end instead of filing date would hand the model three years of
information it could not have had, and would make every backtest look better
than reality in a way that is invisible in the output.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Sequence

from ..analytics import BetaEstimate, compute_beta
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
from .reconstruct import reconstruct_series
from .prices import (
    BENCHMARK_SYMBOL,
    TEN_YEAR_YIELD_SYMBOL,
    PriceSource,
    average_dollar_volume,
)

#: How much price history to pull for beta and the C1 reconstruction.
DEFAULT_HISTORY_DAYS = int(365.25 * 8)


@dataclass
class CompositeAdapter(DataAdapter):
    """One adapter over a fundamentals source and a price source."""

    fundamentals: DataAdapter
    prices: PriceSource
    #: Restricts facts to what had been filed by this date.  Set it to run a
    #: point-in-time backtest; leave it ``None`` for live screening.
    as_of: date | None = None
    benchmark_symbol: str = BENCHMARK_SYMBOL
    name: str = "composite"
    _benchmark_cache: tuple[PricePoint, ...] | None = field(default=None, repr=False)
    _beta_cache: dict[str, BetaEstimate | None] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.name = f"{self.fundamentals.name}+{self.prices.name}"

    # -- identity ---------------------------------------------------------
    def get_profile(self, symbol: str) -> CompanyProfile:
        profile = self.fundamentals.get_profile(symbol)
        end = self.as_of or date.today()
        start = end - timedelta(days=DEFAULT_HISTORY_DAYS)

        try:
            history = self.prices.get_prices(symbol, start, end)
        except DataUnavailable:
            # No prices means no market cap, no ADV, and no beta.  Every one of
            # those is an A5 data gap; none of them gets a substitute.
            return profile

        latest = history[0] if history else None
        adv = average_dollar_volume(history)

        shares = self._latest_share_count(symbol)
        market_cap = (
            latest.close * shares if latest and shares else None
        )

        beta_estimate = self._beta(symbol, history)

        return replace(
            profile,
            market_cap=market_cap,
            adv_3m_usd=adv,
            beta=beta_estimate.beta if beta_estimate else None,
            beta_is_proxy=False,
        )

    def _latest_share_count(self, symbol: str) -> float | None:
        try:
            quarters = self.fundamentals.get_quarterly_financials(symbol, 4)
        except DataUnavailable:
            return None
        for row in quarters:
            count = row.shares_diluted or row.shares_outstanding
            if count:
                return count
        return None

    def _benchmark(self) -> Sequence[PricePoint]:
        if self._benchmark_cache is None:
            end = self.as_of or date.today()
            start = end - timedelta(days=DEFAULT_HISTORY_DAYS)
            try:
                self._benchmark_cache = tuple(
                    self.prices.get_prices(self.benchmark_symbol, start, end)
                )
            except DataUnavailable:
                self._benchmark_cache = ()
        return self._benchmark_cache

    def _beta(
        self, symbol: str, history: Sequence[PricePoint]
    ) -> BetaEstimate | None:
        if symbol not in self._beta_cache:
            benchmark = self._benchmark()
            self._beta_cache[symbol] = (
                compute_beta(history, benchmark, benchmark=self.benchmark_symbol)
                if benchmark
                else None
            )
        return self._beta_cache[symbol]

    def beta_estimate(self, symbol: str) -> BetaEstimate | None:
        """The beta with its window and fit, for the audit log."""
        end = self.as_of or date.today()
        start = end - timedelta(days=DEFAULT_HISTORY_DAYS)
        try:
            history = self.prices.get_prices(symbol, start, end)
        except DataUnavailable:
            return None
        return self._beta(symbol, history)

    # -- pass-through -----------------------------------------------------
    def get_annual_financials(self, symbol: str, years: int) -> Sequence[PeriodFinancials]:
        return self.fundamentals.get_annual_financials(symbol, years)

    def get_quarterly_financials(self, symbol: str, quarters: int) -> Sequence[PeriodFinancials]:
        return self.fundamentals.get_quarterly_financials(symbol, quarters)

    def get_prices(self, symbol: str, start: date, end: date) -> Sequence[PricePoint]:
        return self.prices.get_prices(symbol, start, end)

    def get_corporate_actions(self, symbol: str, years: int) -> Sequence[CorporateAction]:
        """Splits from the price source.

        This is a partial answer and is reported as one: splits are covered,
        spinoffs are not.  C1.1 needs both, so the composite adapter declares
        ``corporate_actions`` unsupported and the coverage report carries the
        gap rather than an empty list that reads as "none occurred".
        """
        end = self.as_of or date.today()
        start = end - timedelta(days=int(365.25 * years))
        return self.prices.get_splits(symbol, start, end)

    def get_peer_symbols(self, symbol: str) -> Sequence[str]:
        raise DataUnavailable(
            "peer_group",
            "no vendor peer list; peers are built from the universe by "
            "grouping, size and growth (gcfp.universe.find_peers)",
        )

    def get_group_members(self, group: str, level: TaxonomyLevel) -> Sequence[str]:
        return self.fundamentals.get_group_members(group, level)

    # -- market -----------------------------------------------------------
    def get_market_data(self) -> MarketData:
        """The risk-free rate, from the 10-year Treasury yield.

        B1 raises if this is missing rather than assuming a rate, so a failure
        here stops valuation loudly instead of quietly shifting every fair
        value.
        """
        risk_free = None
        stamp = None
        try:
            level = self.prices.get_index_level(TEN_YEAR_YIELD_SYMBOL)
            if level is not None:
                # ^TNX quotes the yield in percent.
                risk_free = level / 100.0
                stamp = self.as_of or date.today()
        except DataUnavailable:
            pass
        return MarketData(risk_free_rate=risk_free, risk_free_rate_date=stamp)

    # -- C1 reconstruction ------------------------------------------------
    def get_historical_multiples(
        self, symbol: str, multiple: str, years: int
    ) -> Sequence[MultipleObservation]:
        """Rebuild C1's series from prices and per-share fundamentals.

        The reconstruction itself lives in :mod:`gcfp.data.reconstruct` so that
        every adapter pairing fundamentals with prices derives the series the
        same way — including its lookahead discipline.
        """
        end = self.as_of or date.today()
        start = end - timedelta(days=int(365.25 * (years + 1)))

        quarters = list(
            self.fundamentals.get_quarterly_financials(symbol, (years + 1) * 4)
        )
        if not quarters:
            raise DataUnavailable(
                "historical_multiples", f"no quarterly financials for {symbol}"
            )
        history = self.prices.get_prices(symbol, start, end)
        if not history:
            raise DataUnavailable("historical_multiples", f"no prices for {symbol}")

        observations = reconstruct_series(
            quarters, history, multiple, end=end, years=years
        )
        if not observations:
            raise DataUnavailable(
                "historical_multiples",
                f"could not reconstruct a {multiple} series for {symbol}",
            )
        return tuple(observations)

    # -- introspection ----------------------------------------------------
    def capabilities(self) -> Sequence[Capability]:
        """The union of both halves, with the composite's own additions."""
        underlying = {c.name: c for c in self.fundamentals.capabilities()}
        out: list[Capability] = []
        for name in REQUIRED_CAPABILITIES:
            if name == "prices":
                out.append(Capability(name, True, f"from {self.prices.name}"))
            elif name == "historical_multiples":
                out.append(
                    Capability(
                        name, True,
                        "reconstructed from prices x per-share fundamentals, "
                        "observed on filing dates so the series carries no lookahead",
                    )
                )
            elif name == "corporate_actions":
                out.append(
                    Capability(
                        name, False,
                        "splits only, from the price source; spinoffs are not "
                        "detected, so C1.1 cannot see a business changing in kind",
                    )
                )
            elif name == "risk_free_rate":
                out.append(
                    Capability(name, True, f"10-year Treasury yield via {self.prices.name}")
                )
            elif name == "peer_group":
                out.append(
                    Capability(name, False, "built from the universe, not fetched")
                )
            else:
                out.append(underlying.get(name, Capability(name, False, "not served")))
        return tuple(out)

    def load_company(self, symbol: str, **kw) -> CompanyData:
        data = super().load_company(symbol, **kw)
        # Forward estimates are unavailable on the free stack; leaving the
        # field None makes C3 report "PEGY n/a" rather than impute a growth
        # rate, which is what the spec requires.
        return replace(data, forward_eps_growth=None)


__all__ = ["CompositeAdapter", "DEFAULT_HISTORY_DAYS"]
