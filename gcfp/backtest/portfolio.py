"""Position and cash tracking for the simulation.

Deliberately simple and explicit: a backtest whose bookkeeping is hard to audit
is a backtest whose results cannot be trusted, and the interesting failures
here are arithmetic (double-counted cash, a position closed twice) rather than
conceptual.

``SimulatedFill`` is named for what it is.  There is no broker, no order type,
and no partial-fill model — the simulation assumes the operator transacts at
the next available close, which is optimistic about liquidity and is stated as
a limitation rather than modelled away.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Sequence

from ..classification import Classification, Regime, regime_of


@dataclass(frozen=True)
class SimulatedFill:
    """One simulated transaction.  Not an order; a record of an assumption."""

    symbol: str
    trade_date: date
    shares: float
    price: float
    side: str  # "buy" | "sell"
    reason: str

    @property
    def value(self) -> float:
        return self.shares * self.price


@dataclass
class BacktestPosition:
    """An open position, with what was believed about it at purchase."""

    symbol: str
    classification: Classification
    shares: float
    cost_basis: float
    opened_on: date
    conviction_at_purchase: float
    intended_weight: float
    anchor_mode: str
    thesis_invalidation: str
    #: Highest close seen while held, for the per-position drawdown §13.6 wants.
    peak_price: float = 0.0
    #: Set when H3 fires, so the reclassification-frequency report can count it.
    reclassified_to: Classification | None = None

    @property
    def regime(self) -> Regime:
        return regime_of(self.classification)

    def market_value(self, price: float) -> float:
        return self.shares * price

    def unrealised_return(self, price: float) -> float:
        if self.cost_basis <= 0:
            return 0.0
        return (self.shares * price) / self.cost_basis - 1.0

    def observe(self, price: float) -> None:
        self.peak_price = max(self.peak_price, price)

    def drawdown_from_peak(self, price: float) -> float:
        if self.peak_price <= 0:
            return 0.0
        return price / self.peak_price - 1.0


@dataclass(frozen=True)
class ClosedPosition:
    """A completed round trip — the unit §13.6's distribution is built from."""

    symbol: str
    classification: Classification
    opened_on: date
    closed_on: date
    cost_basis: float
    proceeds: float
    conviction_at_purchase: float
    anchor_mode: str
    exit_reason: str
    #: Worst mark-to-market drawdown while the position was held.
    max_drawdown: float

    @property
    def total_return(self) -> float:
        if self.cost_basis <= 0:
            return 0.0
        return self.proceeds / self.cost_basis - 1.0

    @property
    def holding_days(self) -> int:
        return (self.closed_on - self.opened_on).days

    @property
    def is_loss(self) -> bool:
        return self.total_return < 0.0

    @property
    def is_total_loss(self) -> bool:
        """§10 expects roughly a third of growth holdings to be total losses,
        so "did it go to nearly zero" is worth counting separately from "did
        it lose money"."""
        return self.total_return <= -0.90


@dataclass(frozen=True)
class Snapshot:
    """The book on one date."""

    as_of: date
    total_value: float
    cash: float
    invested: float
    position_count: int
    #: Value by bucket, so F2's three-bucket discipline can be audited over time.
    ballast: float
    core_picks: float
    growth_picks: float
    benchmark_level: float | None = None

    @property
    def cash_weight(self) -> float:
        return self.cash / self.total_value if self.total_value > 0 else 0.0


@dataclass
class BacktestBook:
    """Cash, open positions, and the audit trail of everything that happened."""

    cash: float
    positions: dict[str, BacktestPosition] = field(default_factory=dict)
    closed: list[ClosedPosition] = field(default_factory=list)
    fills: list[SimulatedFill] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    #: Peak-to-trough tracking for each open position, keyed by symbol.
    _drawdowns: dict[str, float] = field(default_factory=dict, repr=False)

    def total_value(self, prices: dict[str, float]) -> float:
        return self.cash + self.invested_value(prices)

    def invested_value(self, prices: dict[str, float]) -> float:
        total = 0.0
        for symbol, position in self.positions.items():
            price = prices.get(symbol)
            if price is not None:
                total += position.market_value(price)
            else:
                # A name whose price has gone missing is held at cost rather
                # than dropped: silently removing it would flatter the result.
                total += position.cost_basis
        return total

    def buy(
        self,
        symbol: str,
        classification: Classification,
        amount: float,
        price: float,
        trade_date: date,
        *,
        conviction: float,
        intended_weight: float,
        anchor_mode: str,
        thesis_invalidation: str,
        reason: str = "BUY signal",
    ) -> SimulatedFill | None:
        """Open or add to a position.  Refuses to spend cash it does not have."""
        if price <= 0 or amount <= 0:
            return None
        spend = min(amount, self.cash)
        if spend <= 0:
            return None
        shares = spend / price

        existing = self.positions.get(symbol)
        if existing is None:
            self.positions[symbol] = BacktestPosition(
                symbol=symbol,
                classification=classification,
                shares=shares,
                cost_basis=spend,
                opened_on=trade_date,
                conviction_at_purchase=conviction,
                intended_weight=intended_weight,
                anchor_mode=anchor_mode,
                thesis_invalidation=thesis_invalidation,
                peak_price=price,
            )
            self._drawdowns[symbol] = 0.0
        else:
            existing.shares += shares
            existing.cost_basis += spend
            existing.observe(price)

        self.cash -= spend
        fill = SimulatedFill(symbol, trade_date, shares, price, "buy", reason)
        self.fills.append(fill)
        return fill

    def sell(
        self,
        symbol: str,
        price: float,
        trade_date: date,
        reason: str,
        *,
        fraction: float = 1.0,
    ) -> SimulatedFill | None:
        """Close or trim a position.  ``fraction`` under 1.0 is a TRIM-TO-CAP."""
        position = self.positions.get(symbol)
        if position is None or price <= 0:
            return None

        fraction = max(0.0, min(fraction, 1.0))
        shares = position.shares * fraction
        proceeds = shares * price
        cost_share = position.cost_basis * fraction

        self.cash += proceeds
        fill = SimulatedFill(symbol, trade_date, shares, price, "sell", reason)
        self.fills.append(fill)

        if fraction >= 1.0:
            self.closed.append(
                ClosedPosition(
                    symbol=symbol,
                    classification=position.classification,
                    opened_on=position.opened_on,
                    closed_on=trade_date,
                    cost_basis=position.cost_basis,
                    proceeds=proceeds,
                    conviction_at_purchase=position.conviction_at_purchase,
                    anchor_mode=position.anchor_mode,
                    exit_reason=reason,
                    max_drawdown=self._drawdowns.get(symbol, 0.0),
                )
            )
            del self.positions[symbol]
            self._drawdowns.pop(symbol, None)
        else:
            position.shares -= shares
            position.cost_basis -= cost_share
        return fill

    def mark(self, prices: dict[str, float]) -> None:
        """Update peaks and per-position drawdowns."""
        for symbol, position in self.positions.items():
            price = prices.get(symbol)
            if price is None or price <= 0:
                continue
            position.observe(price)
            drawdown = position.drawdown_from_peak(price)
            self._drawdowns[symbol] = min(
                self._drawdowns.get(symbol, 0.0), drawdown
            )

    def snapshot(
        self,
        as_of: date,
        prices: dict[str, float],
        benchmark_level: float | None = None,
    ) -> Snapshot:
        invested = self.invested_value(prices)
        core = sum(
            p.market_value(prices.get(s, 0.0))
            for s, p in self.positions.items()
            if p.regime is Regime.CORE
        )
        growth = sum(
            p.market_value(prices.get(s, 0.0))
            for s, p in self.positions.items()
            if p.regime is Regime.GROWTH
        )
        snap = Snapshot(
            as_of=as_of,
            total_value=self.cash + invested,
            cash=self.cash,
            invested=invested,
            position_count=len(self.positions),
            # Cash is the backtest's ballast: the simulation holds no index
            # fund, so an unspent allocation sits in cash by construction (F3).
            ballast=self.cash,
            core_picks=core,
            growth_picks=growth,
            benchmark_level=benchmark_level,
        )
        self.snapshots.append(snap)
        return snap

    def sleeve_value(self, regime: Regime, prices: dict[str, float]) -> float:
        return sum(
            p.market_value(prices.get(s, p.cost_basis / max(p.shares, 1e-9)))
            for s, p in self.positions.items()
            if p.regime is regime
        )

    @property
    def equity_curve(self) -> list[tuple[date, float]]:
        return [(s.as_of, s.total_value) for s in self.snapshots]


__all__ = [
    "BacktestBook",
    "BacktestPosition",
    "ClosedPosition",
    "SimulatedFill",
    "Snapshot",
]
