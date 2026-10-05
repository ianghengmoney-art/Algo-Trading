"""Compose one source's gaps with another's coverage.

The coverage audits kept reaching the same shape of conclusion: no single
vendor serves everything Module A needs. QuantConnect carries point-in-time
fundamentals but no restatement or late-filing status; EDGAR carries those as
structured filing events but no financials at all. Neither is sufficient; the
pair is.

This adapter delegates everything to a primary source and overlays filing flags
from a secondary one, so the composition happens in the data layer where it
belongs rather than inside a gate.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional, Protocol, Sequence

from ..types import (
    CandidateData,
    CompanyProfile,
    Estimates,
    Financials,
    FilingFlags,
    MultipleSeries,
    PricePoint,
)
from .base import Capability, DataAdapter, DataUnavailable


class FilingFlagProvider(Protocol):
    """Anything that can answer Module A4 for a given CIK."""

    def get_flags(self, cik: Optional[str], as_of: Optional[date] = None) -> FilingFlags:
        ...


class CompositeAdapter(DataAdapter):
    """A primary source, with filing flags overlaid from a second one.

    The overlay never *weakens* an answer: ``FilingFlags.merge`` refuses to let
    a ``None`` overwrite a known value, so an EDGAR outage degrades the result
    to whatever the primary knew rather than silently reporting a clean bill of
    health.
    """

    def __init__(self, primary: DataAdapter, filing_flags: FilingFlagProvider) -> None:
        self.primary = primary
        self.filing_flags = filing_flags
        self.name = f"{primary.name}+edgar"

    def capabilities(self) -> set[Capability]:
        return set(self.primary.capabilities()) | {Capability.FILING_FLAGS}

    # -- the one method this class exists to change ------------------------
    def get_filing_flags(self, symbol: str) -> FilingFlags:
        try:
            base = self.primary.get_filing_flags(symbol)
        except DataUnavailable:
            base = FilingFlags()

        cik = None
        try:
            cik = self.primary.get_profile(symbol).cik
        except DataUnavailable:
            pass

        overlay = self.filing_flags.get_flags(cik, as_of=base.as_of)
        return base.merge(overlay)

    # -- everything else is the primary source -----------------------------
    def get_profile(self, symbol: str) -> CompanyProfile:
        return self.primary.get_profile(symbol)

    def get_annual_financials(self, symbol: str, years: int = 10) -> Sequence[Financials]:
        return self.primary.get_annual_financials(symbol, years)

    def get_quarterly_financials(self, symbol: str, quarters: int = 12) -> Sequence[Financials]:
        return self.primary.get_quarterly_financials(symbol, quarters)

    def get_price_history(self, symbol: str, years: int = 10) -> Sequence[PricePoint]:
        return self.primary.get_price_history(symbol, years)

    def get_multiple_series(self, symbol: str, multiple_name: str, years: int = 10) -> MultipleSeries:
        return self.primary.get_multiple_series(symbol, multiple_name, years)

    def get_peers(self, symbol: str) -> Sequence[str]:
        return self.primary.get_peers(symbol)

    def get_estimates(self, symbol: str) -> Estimates:
        return self.primary.get_estimates(symbol)

    def get_sector_net_debt_ebitda(self, sector: str) -> Sequence[float]:
        return self.primary.get_sector_net_debt_ebitda(sector)

    def load_candidate(self, symbol: str, as_of: Optional[date] = None) -> CandidateData:
        """Assemble from the primary, then replace the flags with the merge."""
        candidate = self.primary.load_candidate(symbol, as_of=as_of)
        import dataclasses

        return dataclasses.replace(candidate, filing_flags=self.get_filing_flags(symbol))

    def __getattr__(self, name: str) -> Any:
        """Pass through anything source-specific, such as QC's set_universe."""
        return getattr(self.primary, name)
