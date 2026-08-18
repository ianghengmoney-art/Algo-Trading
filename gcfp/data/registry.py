"""Adapter registry.

Keeps the choice of source in one place so that Section 15's swappability is a
configuration decision rather than an import scattered through the modules.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from .base import CapabilityGate, DataAdapter

# Tried in order when measuring how much history a source really has; the first
# one that returns a series for a symbol answers for that symbol.
PROBE_MULTIPLES = ("trailing P/E", "forward P/E", "P/B", "EV/Revenue", "P/AFFO")

_BUILDERS: dict[str, Callable[[], DataAdapter]] = {}


def register(name: str, builder: Callable[[], DataAdapter]) -> None:
    _BUILDERS[name] = builder


def available() -> list[str]:
    return sorted(_BUILDERS)


def build(name: str) -> DataAdapter:
    if name not in _BUILDERS:
        raise KeyError(f"unknown adapter {name!r}; available: {', '.join(available())}")
    return _BUILDERS[name]()


def _fixtures() -> DataAdapter:
    from .fixtures import FixtureAdapter

    return FixtureAdapter()


def _fmp() -> DataAdapter:
    from .fmp import FMPAdapter

    return FMPAdapter()


def _yfinance() -> DataAdapter:
    from .yfinance_adapter import YFinanceAdapter

    return YFinanceAdapter()


register("fixtures", _fixtures)
register("fmp", _fmp)
register("yfinance", _yfinance)


def measure_multiple_history_years(
    adapter: DataAdapter, symbols: Sequence[str], sample: int = 5
) -> Optional[float]:
    """How many years of multiple history the source actually serves.

    Measured against real symbols rather than declared, and reported as the
    *minimum* across the sample: Module C1 has to work for the thinnest name in
    the universe, not the best-covered one.
    """
    observed: list[float] = []
    for symbol in list(symbols)[:sample]:
        for name in PROBE_MULTIPLES:
            try:
                series = adapter.get_multiple_series(symbol, name, years=10)
            except Exception:
                # Includes DataUnavailable and any transport error the adapter
                # surfaces: a measurement probe must never break a run.
                continue
            if series.points:
                observed.append(series.years_covered)
                break
    return min(observed) if observed else None


def capability_gate(
    adapter: DataAdapter,
    multiple_history_years: Optional[float] = None,
    symbols: Optional[Sequence[str]] = None,
) -> CapabilityGate:
    """Build the gate, measuring multiple-history depth when it is not supplied."""
    if multiple_history_years is None and symbols:
        multiple_history_years = measure_multiple_history_years(adapter, symbols)
    return CapabilityGate(
        adapter_name=adapter.name,
        capabilities=adapter.capabilities(),
        multiple_history_years=multiple_history_years,
    )
