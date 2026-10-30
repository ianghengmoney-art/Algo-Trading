"""How close is this company to being classified differently?

A6's tags are hard cut-offs on soft numbers.  A company growing 19.9% is
CORE-STABLE; at 20.1% it is CORE-GROWTH — and that flips the valuation method
(two-stage DCF becomes a three-phase scenario DCF), the buy gate (25% becomes
35%), the sizing regime, and the anchor multiple.  All of it turns on a figure
that gets restated, that depends on which revenue tag the filer used, and that
a single quarter can move.

The cliff is in the spec and this module does not remove it.  What it removes
is the possibility of walking off one without noticing: any company sitting
within a small margin of a threshold is flagged, together with what would
change if it crossed.

H3 already handles a tag that *has* changed.  This is the early warning that
one is about to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .classification import Classification
from .config import Config
from .modules.a_health import (
    _positive_fcf_years,
    _profitable_years,
    _revenue_cagr_2y,
    _revenue_growth_yoy,
    _rule_of_40,
    is_profitable_ttm,
)
from .types import CompanyData

#: Within this fraction of a threshold counts as "near".  10% of the threshold
#: value, so a 20% growth cut-off flags between 18% and 22%.
NEAR_MARGIN = 0.10


@dataclass(frozen=True)
class BoundaryProximity:
    """One threshold this company sits close to."""

    criterion: str
    value: float
    threshold: float
    #: True when the company currently satisfies the criterion.
    satisfied: bool
    would_become: Classification | None
    consequence: str

    @property
    def distance(self) -> float:
        return self.value - self.threshold

    @property
    def relative_distance(self) -> float:
        if self.threshold == 0:
            return abs(self.distance)
        return abs(self.distance / self.threshold)

    @property
    def is_near(self) -> bool:
        return self.relative_distance <= NEAR_MARGIN

    def as_report_line(self) -> str:
        direction = "above" if self.distance >= 0 else "below"
        line = (
            f"    {self.criterion}: {self.value:.4g} is {abs(self.distance):.4g} "
            f"{direction} the {self.threshold:.4g} threshold"
        )
        if self.would_become is not None:
            line += f"\n      → would become {self.would_become.value}: {self.consequence}"
        return line


def _consequence(
    current: Classification | None, other: Classification, config: Config
) -> str:
    """What actually changes if the tag flips."""
    from .modules.c_anchors import anchor_multiple_for
    from .classification import regime_of

    bits = [
        f"gate {config.triggers.buy_discount[other]:.0%}",
        f"anchor {anchor_multiple_for(other, config)}",
        f"{regime_of(other).value} sizing",
    ]
    if current is not None:
        bits.insert(
            0,
            f"gate moves {config.triggers.buy_discount[current]:.0%} → "
            f"{config.triggers.buy_discount[other]:.0%}",
        )
        bits.pop(1)
    return ", ".join(bits)


def assess(
    data: CompanyData, config: Config, current: Classification | None
) -> list[BoundaryProximity]:
    """Every A6 threshold this company sits near."""
    out: list[BoundaryProximity] = []

    growth = _revenue_growth_yoy(data)
    cagr = _revenue_cagr_2y(data)
    rule40 = _rule_of_40(data)
    profitable = is_profitable_ttm(data)
    fcf_years = _positive_fcf_years(data, 5)

    # The 20% growth line separates CORE-STABLE from CORE-GROWTH.
    if growth is not None and profitable:
        target = (
            Classification.CORE_GROWTH
            if current is Classification.CORE_STABLE
            else Classification.CORE_STABLE
        )
        out.append(
            BoundaryProximity(
                criterion="revenue growth YoY",
                value=growth,
                threshold=0.20,
                satisfied=growth >= 0.20,
                would_become=target,
                consequence=_consequence(current, target, config),
            )
        )

    # The 15% two-year CAGR, CORE-GROWTH's second growth test.
    if cagr is not None and profitable:
        out.append(
            BoundaryProximity(
                criterion="revenue 2-year CAGR",
                value=cagr,
                threshold=0.15,
                satisfied=cagr >= 0.15,
                would_become=Classification.CORE_GROWTH,
                consequence=_consequence(current, Classification.CORE_GROWTH, config),
            )
        )

    # Rule of 40 — CORE-GROWTH's quality bar.
    if rule40 is not None and profitable:
        out.append(
            BoundaryProximity(
                criterion="Rule of 40",
                value=rule40,
                threshold=40.0,
                satisfied=rule40 >= 40.0,
                would_become=Classification.CORE_GROWTH,
                consequence=_consequence(current, Classification.CORE_GROWTH, config),
            )
        )

    # Positive FCF in 4 of 5 years — CORE-STABLE's durability test.
    if fcf_years is not None:
        out.append(
            BoundaryProximity(
                criterion="years of positive FCF (of 5)",
                value=float(fcf_years),
                threshold=4.0,
                satisfied=fcf_years >= 4,
                would_become=Classification.CORE_STABLE,
                consequence=_consequence(current, Classification.CORE_STABLE, config),
            )
        )

    # The $50M revenue floor for SPEC-GROWTH.
    latest = data.latest_annual
    if latest is not None and latest.revenue is not None and profitable is False:
        out.append(
            BoundaryProximity(
                criterion="revenue (SPEC-GROWTH floor)",
                value=latest.revenue,
                threshold=50_000_000.0,
                satisfied=latest.revenue >= 50_000_000.0,
                would_become=Classification.SPEC_GROWTH,
                consequence=_consequence(current, Classification.SPEC_GROWTH, config),
            )
        )

    return out


def near_boundaries(
    data: CompanyData, config: Config, current: Classification | None
) -> list[BoundaryProximity]:
    return [p for p in assess(data, config, current) if p.is_near]


def report_lines(
    data: CompanyData, config: Config, current: Classification | None
) -> list[str]:
    near = near_boundaries(data, config, current)
    if not near:
        return []
    lines = [
        f"  ** NEAR CLASSIFICATION BOUNDARY — {len(near)} threshold(s) within "
        f"{NEAR_MARGIN:.0%} **"
    ]
    lines.extend(p.as_report_line() for p in near)
    lines.append(
        "    A restatement or one soft quarter could move this name across. "
        "H3's protocol handles a tag that has changed; this is the warning "
        "that one is about to."
    )
    return lines


# -- round-trip costs (0.6) ----------------------------------------------

#: A round trip is two transactions. The default is deliberately modest for a
#: liquid US large cap and should be raised for anything thinner.
DEFAULT_ONE_WAY_COST = 0.001


@dataclass(frozen=True)
class RoundTripCost:
    """What the discount looks like after the cost of getting in and out.

    The spec's gates are stated gross. This does not change them — parameter
    changes go through annual review — but a 25% gate cleared by 25.1% before
    costs is not cleared after them, and that should be visible at the moment
    of decision rather than discovered in the P&L.
    """

    gross_discount: float
    one_way_cost: float = DEFAULT_ONE_WAY_COST

    @property
    def round_trip(self) -> float:
        return self.one_way_cost * 2.0

    @property
    def net_discount(self) -> float:
        return self.gross_discount - self.round_trip

    def clears(self, threshold: float) -> bool:
        return self.net_discount >= threshold

    def as_report_line(self, threshold: float) -> str:
        line = (
            f"  round-trip cost: {self.gross_discount:.1%} gross → "
            f"{self.net_discount:.1%} net of {self.round_trip:.2%} costs"
        )
        if self.gross_discount >= threshold and not self.clears(threshold):
            line += (
                f"  ** clears the {threshold:.0%} gate gross but NOT net — "
                "the margin is inside the cost of trading it **"
            )
        return line


__all__ = [
    "BoundaryProximity",
    "NEAR_MARGIN",
    "RoundTripCost",
    "DEFAULT_ONE_WAY_COST",
    "assess",
    "near_boundaries",
    "report_lines",
]
