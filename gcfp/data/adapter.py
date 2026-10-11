"""The data-layer boundary.

§16: the data layer must be a clean adapter interface — FMP, yfinance, SimFin,
Alpha Vantage swappable without touching gate logic.  This matters more in v4
than in any prior version, because five classification paths need genuinely
different inputs (cash-flow depth for B1/B2, tangible book for B3, FFO/AFFO for
B4, combined ratio for B5) and free sources cover these unevenly.

No gate module imports a provider directly.  They take a :class:`DataAdapter`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence

from ..types import (
    CompanyData,
    CompanyProfile,
    CorporateAction,
    MarketData,
    MultipleObservation,
    PeriodFinancials,
    PricePoint,
    TaxonomyLevel,
    years_before,
)


class DataUnavailable(Exception):
    """The source cannot supply a required input.

    Raised rather than returning a default: A5 must see a genuine gap.  Callers
    convert this into a logged data-gap failure, never into an imputed value.
    """

    def __init__(self, field_name: str, detail: str = "") -> None:
        self.field_name = field_name
        self.detail = detail
        super().__init__(f"{field_name}: {detail}" if detail else field_name)


@dataclass(frozen=True)
class Capability:
    """One thing an adapter either can or cannot supply.

    The §18 probe reads these to build its coverage report, so an adapter
    declaring a capability it does not have will show up as a failed probe row
    rather than a silently wrong number downstream.
    """

    name: str
    supported: bool
    detail: str = ""


#: The capability names the probe checks.  Each maps to at least one gate that
#: cannot be enforced without it.
REQUIRED_CAPABILITIES: tuple[str, ...] = (
    "profile",
    "annual_financials",
    "quarterly_financials",
    "prices",
    "historical_multiples",
    "corporate_actions",
    "gics_sub_industry",
    "peer_group",
    "share_count_history",
    "cash_burn",
    "reit_ffo_affo",
    "insurer_combined_ratio",
    "bank_tangible_book",
    "forward_estimates",
    "risk_free_rate",
    "fx_rates",
    "point_in_time",
)


class DataAdapter(ABC):
    """Every provider implements this and nothing more."""

    name: str = "abstract"

    # -- identity ---------------------------------------------------------
    @abstractmethod
    def get_profile(self, symbol: str) -> CompanyProfile:
        """Identity, structure flags, taxonomy, liquidity."""

    # -- fundamentals -----------------------------------------------------
    @abstractmethod
    def get_annual_financials(
        self, symbol: str, years: int
    ) -> Sequence[PeriodFinancials]:
        """Newest first.  Fewer rows than requested is a data gap, not an error."""

    @abstractmethod
    def get_quarterly_financials(
        self, symbol: str, quarters: int
    ) -> Sequence[PeriodFinancials]:
        """Newest first.  A1/A3 need four trailing quarters."""

    # -- market -----------------------------------------------------------
    @abstractmethod
    def get_prices(
        self, symbol: str, start: date, end: date
    ) -> Sequence[PricePoint]:
        """Daily closes.  Used for ADV, D5 momentum, and C1 price-derived
        multiples — never as a buy or sell reason (Prime Directive 5)."""

    @abstractmethod
    def get_historical_multiples(
        self, symbol: str, multiple: str, years: int
    ) -> Sequence[MultipleObservation]:
        """C1's series, for one named multiple.  Split adjustment is C1.1's
        job, not the adapter's — the adapter reports what the source holds."""

    @abstractmethod
    def get_corporate_actions(
        self, symbol: str, years: int
    ) -> Sequence[CorporateAction]:
        """C1.1.  An adapter that cannot detect spinoffs must say so via
        :meth:`capabilities` rather than returning an empty tuple, which would
        read as "no spinoffs occurred"."""

    # -- grouping ---------------------------------------------------------
    @abstractmethod
    def get_peer_symbols(self, symbol: str) -> Sequence[str]:
        """Candidate peers only.  C2 applies the real screen and logs every
        rejection; a vendor's peer list is a starting set, not a peer group."""

    @abstractmethod
    def get_group_members(
        self, group: str, level: TaxonomyLevel
    ) -> Sequence[str]:
        """Everything in a taxonomy grouping, for A2's universe-wide median."""

    # -- market-wide ------------------------------------------------------
    @abstractmethod
    def get_market_data(self) -> MarketData:
        """Risk-free rate, FX spot, and the group aggregates A2/D4 compare to."""

    # -- introspection ----------------------------------------------------
    @abstractmethod
    def capabilities(self) -> Sequence[Capability]:
        """What this adapter can actually supply, for the §18 coverage report."""

    # -- convenience ------------------------------------------------------
    def capability_map(self) -> dict[str, Capability]:
        return {c.name: c for c in self.capabilities()}

    def supports(self, capability: str) -> bool:
        cap = self.capability_map().get(capability)
        return bool(cap and cap.supported)

    def load_company(
        self,
        symbol: str,
        *,
        annual_years: int = 10,
        quarters: int = 12,
        multiple: str | None = None,
        history_years: int = 7,
        price_start: date | None = None,
        price_end: date | None = None,
    ) -> CompanyData:
        """Assemble a :class:`CompanyData` from the individual calls.

        Each piece is fetched defensively: a source that cannot supply one
        series should still let the gates run far enough to report *which*
        input was missing, which is the whole output of the §18 probe.
        """
        profile = self.get_profile(symbol)
        notes: list[str] = []

        def _try(label: str, fn):
            try:
                return fn()
            except DataUnavailable as exc:
                notes.append(f"{label} unavailable: {exc}")
                return ()
            except Exception as exc:  # pragma: no cover - provider-specific
                notes.append(f"{label} error: {type(exc).__name__}: {exc}")
                return ()

        annual = _try("annual", lambda: self.get_annual_financials(symbol, annual_years))
        quarterly = _try(
            "quarterly", lambda: self.get_quarterly_financials(symbol, quarters)
        )
        end = price_end or date.today()
        start = price_start or years_before(end, 2)
        prices = _try("prices", lambda: self.get_prices(symbol, start, end))
        actions = _try(
            "corporate_actions", lambda: self.get_corporate_actions(symbol, history_years)
        )
        multiples: Sequence[MultipleObservation] = ()
        if multiple:
            multiples = _try(
                "multiples",
                lambda: self.get_historical_multiples(symbol, multiple, history_years),
            )

        current_price = None
        price_date = None
        if prices:
            latest = max(prices, key=lambda p: p.price_date)
            current_price = latest.close
            price_date = latest.price_date

        return CompanyData(
            profile=profile,
            annual=tuple(annual),
            quarterly=tuple(quarterly),
            prices=tuple(prices),
            multiples=tuple(multiples),
            corporate_actions=tuple(actions),
            current_price=current_price,
            price_date=price_date,
            source_notes=tuple(notes),
            source_name=self.name,
        )


def summarise_capabilities(
    adapters: Iterable[DataAdapter],
) -> dict[str, dict[str, bool]]:
    """Capability matrix across adapters, for the coverage report."""
    return {
        adapter.name: {c.name: c.supported for c in adapter.capabilities()}
        for adapter in adapters
    }


__all__ = [
    "DataAdapter",
    "DataUnavailable",
    "Capability",
    "REQUIRED_CAPABILITIES",
    "summarise_capabilities",
]
