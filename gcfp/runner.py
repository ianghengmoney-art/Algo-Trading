"""Assembling a run: adapters, universe, inputs.

The CLIs are thin over this so the same wiring is used by scripts, tests, and
any future backtest driver — and so "how a run is put together" lives in one
readable place rather than being reconstructed per entry point.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

from .config import Config
from .data.adapter import DataAdapter, DataUnavailable
from .modules import a_health, c_anchors
from .modules.f_sizing import PortfolioState
from .modules.k_currency import FxTable
from .pipeline import CandidateInputs
from .types import MarketData
from .universe import Universe, build_universe, default_symbol_list, find_peers


def build_free_adapter(
    user_agent: str,
    *,
    cache_dir: Path | None = None,
    as_of: date | None = None,
) -> DataAdapter:
    """The zero-cost stack: SEC EDGAR fundamentals over a free price feed.

    ``user_agent`` must identify you with contact details — SEC policy, and
    they block anonymous scrapers.
    """
    from .data.composite import CompositeAdapter
    from .data.edgar import EdgarAdapter
    from .data.prices import FallbackPriceSource

    return CompositeAdapter(
        fundamentals=EdgarAdapter(
            user_agent=user_agent, cache_dir=cache_dir, as_of=as_of
        ),
        prices=FallbackPriceSource(),
        as_of=as_of,
    )


def free_stack_config(base: Config | None = None) -> Config:
    """Config adjusted for a source with no analyst estimates.

    CORE-GROWTH's C1 anchor moves to a trailing multiple.  That is a real
    downgrade and it is made explicitly here rather than by letting a forward
    series quietly fill with trailing numbers — C1 logs the substitution on
    every name it touches.
    """
    import dataclasses

    base = base or Config()
    return dataclasses.replace(
        base,
        anchors=dataclasses.replace(
            base.anchors,
            core_growth_multiple="trailing_pe",
            forward_estimates_available=False,
        ),
    )


def load_or_build_universe(
    adapter: DataAdapter,
    config: Config,
    cache_path: Path,
    *,
    limit: int | None = None,
    max_age_days: int = 7,
    force: bool = False,
    progress: bool = False,
) -> Universe:
    """Reuse a recent universe snapshot, or build one.

    Building touches every filer once and is the slow step of a run, so a
    weekly screen should pay for it weekly rather than per name.
    """
    cache_path = Path(cache_path)
    if cache_path.exists() and not force:
        universe = Universe.load(cache_path)
        if not universe.is_stale(max_age_days):
            return universe

    symbols = default_symbol_list(adapter, limit=limit)
    universe = build_universe(adapter, symbols, config, progress=progress)
    universe.save(cache_path)
    return universe


def build_inputs(
    adapter: DataAdapter,
    universe: Universe,
    config: Config,
    symbols: Sequence[str],
    *,
    sector_price_to_book: dict[str, float] | None = None,
    peer_price_to_affo: dict[str, float] | None = None,
) -> dict[str, CandidateInputs]:
    """Assemble each candidate's per-name inputs.

    Peers are found from the universe rather than taken from a vendor list, so
    the screen that produced them is visible and C2's exclusion log means
    something.

    The reference multiples for banks, REITs and insurers stay operator-supplied:
    a sector P/B is a judgement about what the sector should trade at, and
    inventing one here would hide that judgement inside the machinery.
    """
    sector_price_to_book = sector_price_to_book or {}
    peer_price_to_affo = peer_price_to_affo or {}
    out: dict[str, CandidateInputs] = {}

    for symbol in symbols:
        member = universe.by_symbol(symbol)
        if member is None:
            continue
        try:
            data = adapter.load_company(symbol)
        except DataUnavailable:
            continue

        tag, _considered, _reasons = a_health.classify(data, config)
        if tag is None:
            continue
        multiple = c_anchors.anchor_multiple_for(tag, config)

        out[symbol] = CandidateInputs(
            current_multiple=c_anchors.compute_current_multiple(data, multiple),
            trailing_pe=c_anchors.compute_current_multiple(data, "trailing_pe"),
            peer_candidates=find_peers(universe, member, config, adapter, multiple),
            subject_group=member.industry,
            subject_growth=member.revenue_growth,
            sector_price_to_book=sector_price_to_book.get(member.sector or ""),
            peer_price_to_affo=peer_price_to_affo.get(member.sector or ""),
        )
    return out


@dataclass
class RunContext:
    """Everything one screen or monitor pass needs, assembled once."""

    adapter: DataAdapter
    config: Config
    universe: Universe
    market: MarketData
    portfolio: PortfolioState
    fx: FxTable

    @property
    def candidate_symbols(self) -> list[str]:
        return [m.symbol for m in self.universe.included]


def assemble(
    adapter: DataAdapter,
    config: Config,
    universe: Universe,
    portfolio: PortfolioState,
    *,
    fx_rates: dict[str, float] | None = None,
    as_of: date | None = None,
) -> RunContext:
    """Fold the universe's grouping statistics into market data and pair it
    with the operator's portfolio and FX table."""
    as_of = as_of or date.today()
    try:
        base = adapter.get_market_data()
    except DataUnavailable:
        base = MarketData()

    market = universe.market_data(base)
    fx = FxTable(dict(fx_rates or base.fx_rates), as_of)
    return RunContext(adapter, config, universe, market, portfolio, fx)


__all__ = [
    "RunContext",
    "assemble",
    "build_free_adapter",
    "build_inputs",
    "free_stack_config",
    "load_or_build_universe",
]
