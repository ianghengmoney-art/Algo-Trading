"""Module F -- position sizing and portfolio allocator.

Two regimes, never blended (Prime Directive 7). CORE positions are larger
because those businesses are durable. GROWTH positions are roughly 1.5-2.25% of
*total* portfolio because a meaningful fraction of them will fail outright --
the strategy earns from winners running, not from any single position being
large.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from ..config import CORE_SLEEVE, GROWTH_SLEEVE, Params, SLEEVE_OF

QUALIFIED_NO_HEADROOM = "QUALIFIED -- NO HEADROOM"


@dataclass
class Position:
    symbol: str
    classification: str
    sector: Optional[str]
    cost_value: float
    current_value: float

    @property
    def sleeve(self) -> str:
        return SLEEVE_OF.get(self.classification, CORE_SLEEVE)


@dataclass
class Portfolio:
    total_value: float
    ballast_value: float = 0.0
    positions: list[Position] = field(default_factory=list)

    def sleeve_positions(self, sleeve: str) -> list[Position]:
        return [p for p in self.positions if p.sleeve == sleeve]

    def sleeve_value(self, sleeve: str) -> float:
        return sum(p.current_value for p in self.sleeve_positions(sleeve))

    def ballast_pct(self) -> float:
        if self.total_value <= 0:
            return 0.0
        return 100.0 * self.ballast_value / self.total_value

    def sleeve_pct_of_portfolio(self, sleeve: str) -> float:
        if self.total_value <= 0:
            return 0.0
        return 100.0 * self.sleeve_value(sleeve) / self.total_value


@dataclass
class SleeveRules:
    name: str
    high_conviction_pct: float
    standard_pct: float
    hard_cap_pct: float
    sector_cap_pct: float
    target_names: tuple[int, int]
    max_pct_of_portfolio: Optional[float] = None


def sleeve_rules(sleeve: str, params: Params) -> SleeveRules:
    sp = params.sizing
    if sleeve == GROWTH_SLEEVE:
        return SleeveRules(
            GROWTH_SLEEVE,
            sp.growth_high_conviction_pct,
            sp.growth_standard_pct,
            sp.growth_hard_cap_pct,
            sp.growth_sector_cap_pct,
            sp.growth_target_names,
            sp.growth_sleeve_max_pct_of_portfolio,
        )
    return SleeveRules(
        CORE_SLEEVE,
        sp.core_high_conviction_pct,
        sp.core_standard_pct,
        sp.core_hard_cap_pct,
        sp.core_sector_cap_pct,
        sp.core_target_names,
        None,
    )


@dataclass
class SizingDecision:
    symbol: str
    classification: str
    sleeve: str
    status: str
    intended_sleeve_pct: float = 0.0
    intended_value: float = 0.0
    intended_portfolio_pct: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass
class AllocatorReport:
    ballast_pct: float
    ballast_target: tuple[float, float]
    ballast_gap_pct: Optional[float]
    sleeve_utilisation: dict = field(default_factory=dict)
    decisions: list[SizingDecision] = field(default_factory=list)
    header_notices: list[str] = field(default_factory=list)


def intended_sleeve_pct(sleeve: str, conviction_total: float, params: Params) -> tuple[float, str]:
    """Size by conviction band, within the sleeve's own regime."""
    rules = sleeve_rules(sleeve, params)
    cp = params.conviction
    if conviction_total >= cp.band_high_conviction:
        return rules.high_conviction_pct, "high conviction"
    if conviction_total >= cp.band_standard:
        return rules.standard_pct, "standard conviction"
    return 0.0, "below the conviction floor for a position"


def sleeve_capacity_value(portfolio: Portfolio, sleeve: str, params: Params) -> float:
    """How much capital this sleeve is allowed to hold in total."""
    rules = sleeve_rules(sleeve, params)
    if rules.max_pct_of_portfolio is None:
        # The core sleeve's size is whatever is not ballast and not growth.
        growth_cap = sleeve_rules(GROWTH_SLEEVE, params).max_pct_of_portfolio or 0.0
        core_pct = max(0.0, 100.0 - portfolio.ballast_pct() - growth_cap)
        return portfolio.total_value * core_pct / 100.0
    return portfolio.total_value * rules.max_pct_of_portfolio / 100.0


def allocate(
    passers: Sequence[tuple[str, str, Optional[str], float]],
    portfolio: Portfolio,
    params: Params,
) -> AllocatorReport:
    """Size every passer, refusing anything that worsens the target structure.

    ``passers`` is a sequence of ``(symbol, classification, sector, conviction)``
    ordered by the caller's preference; ties are broken by conviction.
    """
    sp = params.sizing
    report = AllocatorReport(
        ballast_pct=portfolio.ballast_pct(),
        ballast_target=(sp.ballast_target_low_pct, sp.ballast_target_high_pct),
        ballast_gap_pct=None,
    )

    if report.ballast_pct < sp.ballast_target_low_pct:
        report.ballast_gap_pct = sp.ballast_target_low_pct - report.ballast_pct
        report.header_notices.append(
            f"INDEX/BALLAST BELOW TARGET -- {report.ballast_pct:.1f}% against a "
            f"{sp.ballast_target_low_pct:.0f}-{sp.ballast_target_high_pct:.0f}% target. "
            f"Close the {report.ballast_gap_pct:.1f}pp gap before acting on any stock "
            "recommendation below."
        )

    for sleeve in (CORE_SLEEVE, GROWTH_SLEEVE):
        rules = sleeve_rules(sleeve, params)
        capacity = sleeve_capacity_value(portfolio, sleeve, params)
        used = portfolio.sleeve_value(sleeve)
        report.sleeve_utilisation[sleeve] = {
            "value": used,
            "capacity": capacity,
            "headroom": max(0.0, capacity - used),
            "utilisation_pct": (100.0 * used / capacity) if capacity > 0 else None,
            "names": len(portfolio.sleeve_positions(sleeve)),
            "target_names": rules.target_names,
            "pct_of_portfolio": portfolio.sleeve_pct_of_portfolio(sleeve),
            "max_pct_of_portfolio": rules.max_pct_of_portfolio,
        }

    ordered = sorted(passers, key=lambda item: item[3], reverse=True)
    by_sleeve: dict[str, list] = {CORE_SLEEVE: [], GROWTH_SLEEVE: []}
    for symbol, classification, sector, conviction in ordered:
        by_sleeve.setdefault(SLEEVE_OF.get(classification, CORE_SLEEVE), []).append(
            (symbol, classification, sector, conviction)
        )

    for sleeve, entries in by_sleeve.items():
        rules = sleeve_rules(sleeve, params)
        capacity = sleeve_capacity_value(portfolio, sleeve, params)
        remaining = max(0.0, capacity - portfolio.sleeve_value(sleeve))
        sector_values: dict[str, float] = {}
        for pos in portfolio.sleeve_positions(sleeve):
            if pos.sector:
                sector_values[pos.sector] = sector_values.get(pos.sector, 0.0) + pos.current_value

        # F3: a thin screen means the market is expensive. Concentrating into it
        # is the exact wrong response, so the gap is held in index/cash instead.
        if entries and len(entries) < sp.min_passers_per_sleeve:
            report.header_notices.append(
                f"{sleeve} SLEEVE THIN -- {len(entries)} "
                f"{'passer' if len(entries) == 1 else 'passers'} against a minimum of "
                f"{sp.min_passers_per_sleeve}. Hold the gap in index/cash; positions are NOT "
                "enlarged to compensate."
            )

        for symbol, classification, sector, conviction in entries:
            pct, band = intended_sleeve_pct(sleeve, conviction, params)
            decision = SizingDecision(
                symbol=symbol,
                classification=classification,
                sleeve=sleeve,
                status="SIZED",
                intended_sleeve_pct=pct,
            )
            if pct <= 0:
                decision.status = "NOT SIZED"
                decision.reasons.append(band)
                report.decisions.append(decision)
                continue

            pct = min(pct, rules.hard_cap_pct)
            value = capacity * pct / 100.0

            if value > remaining:
                decision.status = QUALIFIED_NO_HEADROOM
                decision.reasons.append(
                    f"{sleeve} sleeve headroom {remaining:,.0f} below the intended {value:,.0f}"
                )
                report.decisions.append(decision)
                continue

            if sector:
                sector_cap_value = capacity * rules.sector_cap_pct / 100.0
                if sector_values.get(sector, 0.0) + value > sector_cap_value:
                    decision.status = QUALIFIED_NO_HEADROOM
                    decision.reasons.append(
                        f"{sector} would exceed the {rules.sector_cap_pct:.0f}% sector cap for "
                        f"the {sleeve} sleeve"
                    )
                    report.decisions.append(decision)
                    continue
                sector_values[sector] = sector_values.get(sector, 0.0) + value

            decision.intended_sleeve_pct = pct
            decision.intended_value = value
            decision.intended_portfolio_pct = (
                100.0 * value / portfolio.total_value if portfolio.total_value else 0.0
            )
            decision.reasons.append(f"{band}: {pct:.1f}% of the {sleeve} sleeve")
            remaining -= value
            report.decisions.append(decision)

        report.sleeve_utilisation[sleeve]["headroom_after_recommendations"] = remaining

    return report


def growth_sleeve_breach(portfolio: Portfolio, params: Params) -> Optional[str]:
    """Hard-enforced portfolio-level cap on the growth sleeve."""
    cap = params.sizing.growth_sleeve_max_pct_of_portfolio
    actual = portfolio.sleeve_pct_of_portfolio(GROWTH_SLEEVE)
    if actual > cap:
        return (
            f"GROWTH SLEEVE {actual:.1f}% OF PORTFOLIO, ABOVE THE {cap:.0f}% HARD CAP. "
            "No new growth position may be opened until this is back inside the cap."
        )
    return None
