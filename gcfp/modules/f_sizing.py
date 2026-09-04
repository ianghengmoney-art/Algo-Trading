"""Module F — sizing and portfolio allocator.

Prime Directive 7: sleeve regimes never blend.

F2 fixes v3 flaw 8.  v3 said "target index/ballast allocation (default 45-55%)"
without stating whether GCFP's own CORE-classified stock picks counted toward
it.  They do not.  A CORE-classified stock pick is not ballast: it carries
single-name risk that an index fund does not, a fact no amount of business
quality removes.  Three named buckets, summing to 100%.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..classification import Classification, Regime, regime_of
from ..config import Config
from ..ledger import AuditLedger


class Bucket(str, Enum):
    BALLAST = "BALLAST"
    CORE_PICKS = "CORE-PICKS"
    GROWTH_PICKS = "GROWTH-PICKS"

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.value


#: Ballast is broad index funds, cash, short government bills, and gold.
#: Individual stock picks never land here regardless of classification.
BUCKET_OF_REGIME: dict[Regime, Bucket] = {
    Regime.CORE: Bucket.CORE_PICKS,
    Regime.GROWTH: Bucket.GROWTH_PICKS,
}


def bucket_for(classification: Classification) -> Bucket:
    return BUCKET_OF_REGIME[regime_of(classification)]


@dataclass(frozen=True)
class BucketState:
    bucket: Bucket
    current_share: float
    target_low: float
    target_high: float

    @property
    def headroom(self) -> float:
        """Room to the top of the target band, as a share of the portfolio."""
        return max(self.target_high - self.current_share, 0.0)

    @property
    def at_cap(self) -> bool:
        return self.current_share >= self.target_high

    @property
    def below_target(self) -> bool:
        return self.current_share < self.target_low

    @property
    def gap_to_target(self) -> float:
        return max(self.target_low - self.current_share, 0.0)

    def as_report_line(self) -> str:
        status = ""
        if self.at_cap:
            status = " — AT CAP"
        elif self.below_target:
            status = f" — BELOW TARGET by {self.gap_to_target:.1%}"
        return (
            f"{self.bucket.value}: {self.current_share:.1%} of portfolio "
            f"(target {self.target_low:.0%}-{self.target_high:.0%}, "
            f"headroom {self.headroom:.1%}){status}"
        )


@dataclass
class PortfolioState:
    """What the allocator needs to know about the book as it stands."""

    total_value: float
    #: Current value by bucket, in base currency.
    bucket_values: dict[Bucket, float] = field(default_factory=dict)
    #: Per-name value, in base currency.
    position_values: dict[str, float] = field(default_factory=dict)
    #: Sector of each held name, for the sector caps.
    position_sectors: dict[str, str] = field(default_factory=dict)
    #: Which regime each held name belongs to.
    position_regimes: dict[str, Regime] = field(default_factory=dict)
    #: Names currently in SINGLE-ANCHOR MODE, for C5's 20% portfolio cap.
    single_anchor_positions: set[str] = field(default_factory=set)

    def bucket_share(self, bucket: Bucket) -> float:
        if self.total_value <= 0:
            return 0.0
        return self.bucket_values.get(bucket, 0.0) / self.total_value

    def sleeve_value(self, regime: Regime) -> float:
        return sum(
            v
            for s, v in self.position_values.items()
            if self.position_regimes.get(s) is regime
        )

    def sleeve_names(self, regime: Regime) -> list[str]:
        return [
            s for s, r in self.position_regimes.items() if r is regime
        ]

    def sector_share_of_sleeve(self, regime: Regime, sector: str) -> float:
        sleeve = self.sleeve_value(regime)
        if sleeve <= 0:
            return 0.0
        held = sum(
            v
            for s, v in self.position_values.items()
            if self.position_regimes.get(s) is regime
            and self.position_sectors.get(s) == sector
        )
        return held / sleeve


@dataclass
class SizingDecision:
    symbol: str
    classification: Classification
    regime: Regime
    bucket: Bucket
    #: Intended size as a share of the *sleeve*.
    sleeve_share: float
    #: The same size as a share of the total portfolio.
    portfolio_share: float
    amount_base_currency: float
    qualified: bool
    constraints: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def as_report_line(self) -> str:
        if not self.qualified:
            return (
                f"{self.symbol}: QUALIFIED — NO HEADROOM · "
                + "; ".join(self.constraints)
            )
        return (
            f"{self.symbol}: {self.sleeve_share:.1%} of {self.regime.value} sleeve "
            f"= {self.portfolio_share:.2%} of portfolio "
            f"= {self.amount_base_currency:,.0f} base currency"
            + (" · " + "; ".join(self.constraints) if self.constraints else "")
        )


def bucket_states(state: PortfolioState, config: Config) -> list[BucketState]:
    cfg = config.sizing
    targets = {
        Bucket.BALLAST: cfg.ballast_target,
        Bucket.CORE_PICKS: cfg.core_picks_target,
        Bucket.GROWTH_PICKS: cfg.growth_picks_target,
    }
    return [
        BucketState(b, state.bucket_share(b), lo, hi)
        for b, (lo, hi) in targets.items()
    ]


def size_position(
    symbol: str,
    classification: Classification,
    conviction: float,
    sector: str,
    state: PortfolioState,
    config: Config,
    *,
    single_anchor: bool = False,
    ledger: AuditLedger | None = None,
) -> SizingDecision:
    """F1 sizing, then every cap that could reduce or refuse it.

    CORE positions are larger because those businesses are durable.  GROWTH
    positions come to ~1.5-2.25% of the total portfolio because a meaningful
    fraction will fail outright — the strategy earns from winners running, not
    from any single position being large.
    """
    cfg = config.sizing
    regime = regime_of(classification)
    bucket = bucket_for(classification)
    constraints: list[str] = []
    flags: list[str] = []

    if regime is Regime.CORE:
        base = (
            cfg.core_high_conviction_size
            if conviction >= config.conviction.high_conviction
            else cfg.core_standard_size
        )
        name_cap, sector_cap = cfg.core_name_cap, cfg.core_sector_cap
        sleeve_share_of_portfolio = _core_sleeve_share(state, config)
    else:
        base = (
            cfg.growth_high_conviction_size
            if conviction >= config.conviction.high_conviction
            else cfg.growth_standard_size
        )
        name_cap, sector_cap = cfg.growth_name_cap, cfg.growth_sector_cap
        sleeve_share_of_portfolio = cfg.growth_sleeve_cap_of_portfolio

    size = base

    if size > name_cap:
        constraints.append(f"trimmed to per-name cap {name_cap:.0%} of sleeve")
        size = name_cap

    # Sector cap.
    held_sector = state.sector_share_of_sleeve(regime, sector)
    if held_sector + size > sector_cap:
        allowed = max(sector_cap - held_sector, 0.0)
        constraints.append(
            f"sector {sector} at {held_sector:.0%} of sleeve; cap {sector_cap:.0%} "
            f"leaves {allowed:.1%}"
        )
        size = min(size, allowed)

    portfolio_share = size * sleeve_share_of_portfolio

    # Bucket headroom.
    states = {b.bucket: b for b in bucket_states(state, config)}
    bstate = states[bucket]
    if bstate.at_cap:
        constraints.append(
            f"{bucket.value} at {bstate.current_share:.1%}, cap "
            f"{bstate.target_high:.0%} — no headroom"
        )
        return SizingDecision(
            symbol, classification, regime, bucket, 0.0, 0.0, 0.0, False,
            constraints, flags,
        )
    if portfolio_share > bstate.headroom:
        constraints.append(
            f"trimmed to {bucket.value} headroom {bstate.headroom:.1%}"
        )
        portfolio_share = bstate.headroom
        size = portfolio_share / sleeve_share_of_portfolio if sleeve_share_of_portfolio else 0.0

    # C5's portfolio-level cap on SINGLE-ANCHOR names.
    if single_anchor:
        cap = config.anchors.single_anchor_portfolio_cap
        held = len(state.single_anchor_positions)
        total_names = max(len(state.position_values), 1)
        current_rate = held / total_names
        if current_rate >= cap:
            constraints.append(
                f"SINGLE-ANCHOR positions at {current_rate:.0%} of the book, "
                f"cap {cap:.0%} — no headroom for another"
            )
            return SizingDecision(
                symbol, classification, regime, bucket, 0.0, 0.0, 0.0, False,
                constraints, flags,
            )
        flags.append(f"counts against the {cap:.0%} SINGLE-ANCHOR portfolio cap")

    # Sleeve name-count guidance.
    low, high = (
        cfg.core_target_names if regime is Regime.CORE else cfg.growth_target_names
    )
    held_names = len(state.sleeve_names(regime))
    if held_names >= high:
        flags.append(
            f"{regime.value} sleeve holds {held_names} names, at the top of the "
            f"{low}-{high} target range"
        )

    qualified = size > 0 and portfolio_share > 0
    if not qualified:
        constraints.append("no room after caps")

    decision = SizingDecision(
        symbol=symbol,
        classification=classification,
        regime=regime,
        bucket=bucket,
        sleeve_share=size,
        portfolio_share=portfolio_share,
        amount_base_currency=portfolio_share * state.total_value,
        qualified=qualified,
        constraints=constraints,
        flags=flags,
    )
    if ledger is not None:
        ledger.note(f"F · {decision.as_report_line()}")
    return decision


def _core_sleeve_share(state: PortfolioState, config: Config) -> float:
    """The CORE sleeve's share of the portfolio.

    Sized against the current CORE-PICKS bucket where one exists, and against
    the middle of its target band when the book is empty — an empty portfolio
    must still produce a sensible first position size.
    """
    current = state.bucket_share(Bucket.CORE_PICKS)
    if current > 0:
        return current
    low, high = config.sizing.core_picks_target
    return (low + high) / 2.0


def below_minimum_handling(
    passer_count: int, regime: Regime, config: Config
) -> str | None:
    """F3 — fewer than 6 passers in a sleeve holds the gap in BALLAST.

    Never concentrate to compensate for a thin screen: a thin screen means the
    market is expensive, and concentrating into it is the exact wrong response.
    """
    if passer_count >= config.sizing.min_passers_per_sleeve:
        return None
    return (
        f"{regime.value} sleeve has {passer_count} passers, fewer than "
        f"{config.sizing.min_passers_per_sleeve} — the gap is held in BALLAST. "
        "A thin screen means the market is expensive; concentrating into it is "
        "the exact wrong response."
    )


def opening_report_lines(state: PortfolioState, config: Config) -> list[str]:
    """§15 — every report opens with bucket utilisation and the BALLAST gap.

    If BALLAST is below target, that gap appears before any stock
    recommendation.
    """
    states = bucket_states(state, config)
    lines = [b.as_report_line() for b in states]
    ballast = next(b for b in states if b.bucket is Bucket.BALLAST)
    if ballast.below_target:
        lines.insert(
            0,
            f"** BALLAST GAP: {ballast.gap_to_target:.1%} below the "
            f"{ballast.target_low:.0%} floor — close this before acting on any "
            "stock recommendation below. **",
        )
    return lines


__all__ = [
    "Bucket",
    "BucketState",
    "PortfolioState",
    "SizingDecision",
    "bucket_for",
    "bucket_states",
    "size_position",
    "below_minimum_handling",
    "opening_report_lines",
]
