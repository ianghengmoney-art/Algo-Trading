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

#: Extra history fetched beyond C1's window, because the *oldest* observation
#: in that window still needs four trailing quarters behind it before a TTM
#: multiple can be computed.  One year of warm-up leaves exactly none: a
#: 7-year window fetched 32 quarters, which is 8 years, and the observation at
#: the 7-year mark consumed the last four of them.  A single quarter missing
#: anywhere — a restatement, a gap in the tag chain, a fiscal-year change —
#: then truncated the series and the probe reported the source as short on
#: history.  Two years costs one wider price request and nothing on the
#: fundamentals side, where the whole filing history arrives in one response.
_WARM_UP_YEARS = 2

#: Start of the Treasury-yield history read for the risk-free rate.
_YIELD_HISTORY_START = date(2000, 1, 1)


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
    #: Both keyed to one as-of date. They were once kept for the life of the
    #: adapter, so a backtest computed each company's beta at the first month
    #: it appeared and reused it for up to ten years, against an index
    #: history frozen at that first month.
    _benchmark_cache: tuple[date | None, tuple[PricePoint, ...]] | None = field(
        default=None, repr=False
    )
    _beta_cache: dict[tuple[str, date | None], BetaEstimate | None] = field(
        default_factory=dict, repr=False
    )
    _split_factors: dict[tuple[str, date], float] = field(default_factory=dict, repr=False)
    _ticker_cache: dict[str, tuple[str, date | None] | None] = field(
        default_factory=dict, repr=False
    )
    _yield_history: tuple[PricePoint, ...] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.name = f"{self.fundamentals.name}+{self.prices.name}"

    # -- price symbols ----------------------------------------------------
    def _price_symbol(self, symbol: str) -> tuple[str, date | None] | None:
        """The price-feed ticker for ``symbol`` and the date it stops being
        trustworthy.  Dead companies enter a point-in-time universe by CIK;
        the price feed only knows tickers."""
        if symbol not in self._ticker_cache:
            resolve = getattr(self.fundamentals, "price_ticker", None)
            self._ticker_cache[symbol] = (
                resolve(symbol) if resolve is not None else (symbol, None)
            )
        return self._ticker_cache[symbol]

    def _symbol_prices(self, symbol: str, start: date, end: date) -> Sequence[PricePoint]:
        """Prices as they stood on the as-of date: adjusted for the splits
        that had happened by then, and for none after."""
        history = self._adjusted_symbol_prices(symbol, start, end)
        resolved = self._price_symbol(symbol)
        factor = self._split_factor_after_as_of(*resolved) if resolved else 1.0
        if factor == 1.0:
            return history
        return tuple(
            replace(
                p,
                close=p.close * factor,
                adjusted_close=(
                    p.adjusted_close * factor if p.adjusted_close is not None else None
                ),
                # The feeds scale volume up by the same splits; undoing both
                # keeps dollar volume unchanged.
                volume=p.volume / factor if p.volume is not None else None,
            )
            for p in history
        )

    def get_dividends(self, symbol: str, start: date, end: date):
        """Cash dividends per share, on the fully split-adjusted basis the
        simulated book holds. Nothing after a dead company's cutoff: a later
        company on the same ticker paid those."""
        resolved = self._price_symbol(symbol)
        if resolved is None:
            raise DataUnavailable("dividends", f"{symbol}: no ticker on record")
        ticker, cutoff = resolved
        if cutoff is not None:
            end = min(end, cutoff)
        if start > end:
            return ()
        return tuple(
            d for d in self.prices.get_dividends(ticker, start, end)
            if start <= d.ex_date <= end
        )

    def get_adjusted_prices(self, symbol: str, start: date, end: date) -> Sequence[PricePoint]:
        """Prices adjusted for every split up to today, as the feeds publish
        them. A simulated book needs this basis: a holding that splits while
        held keeps its share count, so its prices must not step down."""
        return self._adjusted_symbol_prices(symbol, start, end)

    def _adjusted_symbol_prices(self, symbol: str, start: date, end: date) -> Sequence[PricePoint]:
        resolved = self._price_symbol(symbol)
        if resolved is None:
            raise DataUnavailable(
                "prices", f"{symbol}: no ticker on record, so no price feed can serve it"
            )
        ticker, cutoff = resolved
        if cutoff is not None:
            # The company stopped filing; anything later under that ticker may
            # be a different company that inherited the symbol.
            if start > cutoff:
                raise DataUnavailable(
                    "prices", f"{symbol} ({ticker}) stopped filing before {start}"
                )
            end = min(end, cutoff)
        history = self.prices.get_prices(ticker, start, end)
        if cutoff is not None:
            history = tuple(p for p in history if p.price_date <= cutoff)
        return history

    # -- identity ---------------------------------------------------------
    def get_profile(self, symbol: str) -> CompanyProfile:
        profile = self.fundamentals.get_profile(symbol)
        end = self.as_of or date.today()
        start = end - timedelta(days=DEFAULT_HISTORY_DAYS)

        try:
            history = self._symbol_prices(symbol, start, end)
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
        if self._benchmark_cache is None or self._benchmark_cache[0] != self.as_of:
            end = self.as_of or date.today()
            start = end - timedelta(days=DEFAULT_HISTORY_DAYS)
            try:
                history = tuple(self.prices.get_prices(self.benchmark_symbol, start, end))
            except DataUnavailable:
                history = ()
            self._benchmark_cache = (self.as_of, history)
        return self._benchmark_cache[1]

    def _beta(
        self, symbol: str, history: Sequence[PricePoint]
    ) -> BetaEstimate | None:
        key = (symbol, self.as_of)
        if key not in self._beta_cache:
            if self._beta_cache and next(iter(self._beta_cache))[1] != self.as_of:
                # A walk moves forward a month at a time and never returns;
                # only the current date is worth the memory.
                self._beta_cache.clear()
            benchmark = self._benchmark()
            self._beta_cache[key] = (
                compute_beta(history, benchmark, benchmark=self.benchmark_symbol)
                if benchmark
                else None
            )
        return self._beta_cache[key]

    def _split_factor_after_as_of(self, ticker: str, cutoff: date | None) -> float:
        """How much later splits shrank this ticker's past prices.

        Both price feeds publish history adjusted for every split up to
        today, while the filings give share counts as they were. At a past
        date the two disagree by every split since: a company that later
        split 10-for-1 looked ten times smaller and cheaper than it was, so
        the fair value per share, the price it was compared with, and the
        market cap behind the size bands were all off by that factor (and
        in the other direction after a reverse split).
        """
        as_of = self.as_of
        if as_of is None:
            return 1.0
        key = (ticker, as_of)
        if key not in self._split_factors:
            factor = 1.0
            try:
                splits = self.prices.get_splits(ticker, as_of + timedelta(days=1), date.today())
            except Exception:
                splits = ()
            for split in splits:
                if not split.ratio or split.ratio <= 0 or split.effective_date <= as_of:
                    continue
                if cutoff is not None and split.effective_date > cutoff:
                    continue  # a later company on a recycled ticker
                factor *= split.ratio
            self._split_factors[key] = factor
        return self._split_factors[key]

    def beta_estimate(self, symbol: str) -> BetaEstimate | None:
        """The beta with its window and fit, for the audit log."""
        end = self.as_of or date.today()
        start = end - timedelta(days=DEFAULT_HISTORY_DAYS)
        try:
            history = self._symbol_prices(symbol, start, end)
        except DataUnavailable:
            return None
        return self._beta(symbol, history)

    # -- pass-through -----------------------------------------------------
    def get_annual_financials(self, symbol: str, years: int) -> Sequence[PeriodFinancials]:
        return self.fundamentals.get_annual_financials(symbol, years)

    def get_quarterly_financials(self, symbol: str, quarters: int) -> Sequence[PeriodFinancials]:
        return self.fundamentals.get_quarterly_financials(symbol, quarters)

    def get_prices(self, symbol: str, start: date, end: date) -> Sequence[PricePoint]:
        return self._symbol_prices(symbol, start, end)

    def get_corporate_actions(self, symbol: str, years: int) -> Sequence[CorporateAction]:
        """Splits from the price source.

        This is a partial answer and is reported as one: splits are covered,
        spinoffs are not.  C1.1 needs both, so the composite adapter declares
        ``corporate_actions`` unsupported and the coverage report carries the
        gap rather than an empty list that reads as "none occurred".
        """
        end = self.as_of or date.today()
        start = end - timedelta(days=int(365.25 * years))

        actions: list[CorporateAction] = []
        # Splits from the price feed.
        try:
            resolved = self._price_symbol(symbol)
            if resolved is not None:
                actions.extend(self.prices.get_splits(resolved[0], start, end))
        except Exception:
            pass
        # Disposals and acquisitions from the fundamentals source's filings.
        # Returning only splits here would drop EDGAR's 8-K Item 2.01 feed,
        # which is the half C1.1 actually needs: a split is adjustable, a
        # separation changes what the company is.
        try:
            actions.extend(self.fundamentals.get_corporate_actions(symbol, years))
        except Exception:
            pass

        if not actions:
            raise DataUnavailable(
                "corporate_actions",
                "neither the price feed nor the filings reported any action; "
                "an empty list must not be read as 'none occurred'",
            )
        return tuple(sorted(actions, key=lambda a: a.effective_date, reverse=True))

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
        """The risk-free rate: the 10-year Treasury yield *on the as-of date*.

        B1 raises if this is missing rather than assuming a rate, so a failure
        here stops valuation loudly instead of quietly shifting every fair
        value.

        This used to read the latest quote whatever the as-of date, so a
        backtest valued 2015 with the current yield — lookahead in the one
        input every DCF shares. The whole yield history is now read once and
        the last close on or before the as-of date is used.
        """
        as_of = self.as_of or date.today()
        if self._yield_history is None:
            try:
                self._yield_history = tuple(
                    self.prices.get_prices(
                        TEN_YEAR_YIELD_SYMBOL, _YIELD_HISTORY_START, date.today()
                    )
                )
            except DataUnavailable:
                self._yield_history = ()
        usable = [
            p for p in self._yield_history
            if as_of - timedelta(days=14) <= p.price_date <= as_of
        ]
        if not usable:
            return MarketData(risk_free_rate=None, risk_free_rate_date=None)
        latest = max(usable, key=lambda p: p.price_date)
        # ^TNX quotes the yield in percent.
        return MarketData(
            risk_free_rate=latest.close / 100.0, risk_free_rate_date=latest.price_date
        )

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
        start = end - timedelta(days=int(365.25 * (years + _WARM_UP_YEARS)))

        quarters = list(
            self.fundamentals.get_quarterly_financials(
                symbol, int((years + _WARM_UP_YEARS) * 4)
            )
        )
        if not quarters:
            raise DataUnavailable(
                "historical_multiples", f"no quarterly financials for {symbol}"
            )
        history = self._symbol_prices(symbol, start, end)
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
