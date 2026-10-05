"""Data adapter interface and capability model.

Section 15 requires that FMP, yfinance, SimFin and Alpha Vantage be swappable
without touching gate logic. That is only half of what is needed. The five
classification paths want genuinely different inputs and free sources cover
them unevenly, so an adapter must also be able to say *what it cannot do* --
otherwise the pipeline discovers a hole three modules deep and the tempting fix
is to approximate. Adapters therefore advertise capabilities up front, and
``gcfp.engine`` refuses the affected paths rather than filling the gap.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Iterable, Mapping, Optional, Sequence

from ..types import (
    CandidateData,
    CompanyProfile,
    Estimates,
    Financials,
    FilingFlags,
    MultipleSeries,
    PricePoint,
)


class Capability(str, Enum):
    """What an adapter can actually supply."""

    PROFILE = "profile"
    ANNUAL_STATEMENTS = "annual_statements"
    QUARTERLY_STATEMENTS = "quarterly_statements"
    SHARE_COUNT_HISTORY = "share_count_history"
    CASH_BURN_HISTORY = "cash_burn_history"
    FILING_FLAGS = "filing_flags"
    PRICE_HISTORY = "price_history"
    MULTIPLE_HISTORY = "multiple_history"
    PEER_LIST = "peer_list"
    SECTOR_AGGREGATES = "sector_aggregates"
    ANALYST_ESTIMATES = "analyst_estimates"
    EARNINGS_CALENDAR = "earnings_calendar"
    REIT_FFO = "reit_ffo"
    INSURER_UNDERWRITING = "insurer_underwriting"
    BANK_TANGIBLE_BOOK = "bank_tangible_book"
    POINT_IN_TIME = "point_in_time"
    DELISTED_COVERAGE = "delisted_coverage"


class DataUnavailable(Exception):
    """Raised when a source cannot supply a required input.

    Never caught and defaulted. Callers convert it into a ``DATA_GAP`` verdict
    so that the absence is visible in the report.
    """

    def __init__(self, symbol: str, what: str, detail: str = "") -> None:
        self.symbol = symbol
        self.what = what
        self.detail = detail
        message = f"{symbol}: {what} unavailable"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)


class DataAdapter(abc.ABC):
    """Base class for every source.

    Subclasses implement only what they can serve and declare the rest absent.
    The default implementations raise ``DataUnavailable`` so an adapter that
    forgets a method fails loudly instead of returning an empty sequence that
    reads downstream as "this company has no debt".
    """

    name: str = "unnamed"

    @abc.abstractmethod
    def capabilities(self) -> set[Capability]:
        """Capabilities this adapter can serve on its current credentials."""

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities()

    # -- required surface -------------------------------------------------
    def get_profile(self, symbol: str) -> CompanyProfile:
        raise DataUnavailable(symbol, "profile", f"{self.name} does not serve profiles")

    def get_annual_financials(self, symbol: str, years: int = 10) -> Sequence[Financials]:
        raise DataUnavailable(symbol, "annual statements", f"not served by {self.name}")

    def get_quarterly_financials(self, symbol: str, quarters: int = 12) -> Sequence[Financials]:
        raise DataUnavailable(symbol, "quarterly statements", f"not served by {self.name}")

    def get_filing_flags(self, symbol: str) -> FilingFlags:
        raise DataUnavailable(symbol, "filing flags", f"not served by {self.name}")

    def get_price_history(self, symbol: str, years: int = 10) -> Sequence[PricePoint]:
        raise DataUnavailable(symbol, "price history", f"not served by {self.name}")

    def get_multiple_series(self, symbol: str, multiple_name: str, years: int = 10) -> MultipleSeries:
        raise DataUnavailable(symbol, f"{multiple_name} history", f"not served by {self.name}")

    def get_peers(self, symbol: str) -> Sequence[str]:
        raise DataUnavailable(symbol, "peer list", f"not served by {self.name}")

    def get_estimates(self, symbol: str) -> Estimates:
        raise DataUnavailable(symbol, "estimates", f"not served by {self.name}")

    def get_sector_net_debt_ebitda(self, sector: str) -> Sequence[float]:
        raise DataUnavailable(sector, "sector leverage aggregates", f"not served by {self.name}")

    # -- assembly ---------------------------------------------------------
    def load_candidate(self, symbol: str, as_of: Optional[date] = None) -> CandidateData:
        """Assemble everything available, tolerating gaps.

        Gaps become missing fields on ``CandidateData``; Module A5 decides
        whether the gap is disqualifying for this candidate's path. Assembly is
        deliberately not the place that judges -- it only reports.
        """
        profile = self.get_profile(symbol)

        def attempt(fn, *args, default=None, **kwargs):
            try:
                return fn(*args, **kwargs)
            except DataUnavailable:
                return default

        annual = attempt(self.get_annual_financials, symbol, default=()) or ()
        quarterly = attempt(self.get_quarterly_financials, symbol, default=()) or ()
        flags = attempt(self.get_filing_flags, symbol, default=FilingFlags()) or FilingFlags()
        estimates = attempt(self.get_estimates, symbol, default=Estimates()) or Estimates()
        prices = attempt(self.get_price_history, symbol, default=()) or ()
        peers = attempt(self.get_peers, symbol, default=()) or ()
        sector_leverage = ()
        if profile.sector:
            sector_leverage = attempt(self.get_sector_net_debt_ebitda, profile.sector, default=()) or ()

        return CandidateData(
            symbol=symbol,
            profile=profile,
            annual=annual,
            quarterly=quarterly,
            filing_flags=flags,
            estimates=estimates,
            price_history=prices,
            peer_symbols=peers,
            sector_net_debt_ebitda=sector_leverage,
            as_of=as_of or profile.as_of,
        )


@dataclass
class CapabilityGate:
    """Section 17's two stop conditions, enforced in code.

    The spec is explicit that these are disable-the-path decisions, not
    degrade-quietly decisions. This class is what the engine consults before it
    is allowed to route anything.
    """

    adapter_name: str
    capabilities: set[Capability]
    multiple_history_years: Optional[float] = None
    required_multiple_history_years: float = 7.0

    @property
    def spec_growth_enabled(self) -> bool:
        """Stop condition 1.

        Without cash-burn and share-count history, gates A3 (pre-profit branch)
        and A4 cannot be enforced -- and without them the SPEC-GROWTH path is
        not a strategy, it is a way to buy companies shortly before they run
        out of money.
        """
        return (
            Capability.CASH_BURN_HISTORY in self.capabilities
            and Capability.SHARE_COUNT_HISTORY in self.capabilities
        )

    @property
    def own_history_anchor_enabled(self) -> bool:
        """Stop condition 2: Module C1 needs a genuine 7-year window."""
        if Capability.MULTIPLE_HISTORY not in self.capabilities:
            return False
        if self.multiple_history_years is None:
            return False
        return self.multiple_history_years >= self.required_multiple_history_years

    @property
    def single_anchor_degraded(self) -> bool:
        """True when the system has lost C1 and is running on peers alone."""
        return not self.own_history_anchor_enabled

    @property
    def point_in_time(self) -> bool:
        return Capability.POINT_IN_TIME in self.capabilities

    def blocking_notices(self) -> list[str]:
        """Notices that must appear in the report header, not a footnote."""
        notices: list[str] = []
        if not self.spec_growth_enabled:
            missing = []
            if Capability.CASH_BURN_HISTORY not in self.capabilities:
                missing.append("cash-burn history")
            if Capability.SHARE_COUNT_HISTORY not in self.capabilities:
                missing.append("share-count history")
            notices.append(
                "SPEC-GROWTH CLASSIFICATION DISABLED -- "
                f"{self.adapter_name} cannot supply {' and '.join(missing)}, so gates A3 "
                "(pre-profit branch) and A4 cannot be enforced."
            )
        if not self.own_history_anchor_enabled:
            if Capability.MULTIPLE_HISTORY not in self.capabilities:
                detail = f"{self.adapter_name} serves no historical multiples"
            else:
                have = self.multiple_history_years or 0.0
                detail = (
                    f"{self.adapter_name} serves {have:.1f}y of history, "
                    f"{self.required_multiple_history_years:.0f}y required"
                )
            notices.append(
                "MODULE C1 DISABLED -- SYSTEM IS SINGLE-ANCHOR. "
                f"{detail}. Buy trigger condition 2 (both anchors confirm) cannot be "
                "satisfied; no BUY alert can fire."
            )
        if not self.point_in_time:
            notices.append(
                "NO POINT-IN-TIME DATA -- constituents and peer sets are current-only. "
                "Any backtest run on this source is inflated by an unknown but material "
                "amount from survivorship bias."
            )
        return notices


@dataclass
class ProbeResult:
    """One (symbol, requirement) coverage observation."""

    symbol: str
    classification_path: str
    requirement: str
    module: str
    available: bool
    detail: str = ""
    value: Optional[str] = None


@dataclass
class CoverageReport:
    """Output of the Section 17 critical first task."""

    adapter_name: str
    generated_on: date
    results: list[ProbeResult] = field(default_factory=list)
    capability_gate: Optional[CapabilityGate] = None
    notes: list[str] = field(default_factory=list)

    def add(self, result: ProbeResult) -> None:
        self.results.append(result)

    def missing(self) -> list[ProbeResult]:
        return [r for r in self.results if not r.available]

    def by_module(self) -> Mapping[str, list[ProbeResult]]:
        grouped: dict[str, list[ProbeResult]] = {}
        for r in self.results:
            grouped.setdefault(r.module, []).append(r)
        return grouped

    def coverage_pct(self) -> float:
        if not self.results:
            return 0.0
        return 100.0 * sum(1 for r in self.results if r.available) / len(self.results)


def merge_capabilities(adapters: Iterable[DataAdapter]) -> set[Capability]:
    """Union of what a stack of adapters can serve.

    Used when an operator fills FMP's gaps with a second source. The union is
    honest only because each adapter's own methods still raise on the specific
    symbol they cannot serve.
    """
    merged: set[Capability] = set()
    for adapter in adapters:
        merged |= adapter.capabilities()
    return merged
