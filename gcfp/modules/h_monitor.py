"""Module H -- holdings monitor.

Full re-run of Modules A to E quarterly, after each earnings season.

Banned as triggers: any function of price history alone. In a book where a 40%
drawdown on a winner is routine, price-based exits systematically remove the
positions the strategy depends on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from ..config import CORE_GROWTH, Params, SPEC_GROWTH
from ..modules.a_health import HealthResult
from ..modules.c_triangulation import TriangulationResult
from ..modules.d_conviction import ConvictionScore
from ..modules.e_triggers import SELL, TriggerResult
from ..types import Verdict

SELL_FLAG = "SELL"
REVIEW_FLAG = "REVIEW"
GREEN = "GREEN"
AMBER = "AMBER"
RED = "RED"


@dataclass
class GrowthDeterioration:
    """GROWTH-only quarterly deterioration inputs.

    Each field is a run-length: how many *consecutive* recent quarters the
    condition has held. A single bad quarter is not a signal.
    """

    quarters_revenue_growth_below_floor: int = 0
    quarters_rule_of_40_below_floor: int = 0
    cash_runway_months: Optional[float] = None
    credible_financing_plan: bool = False


@dataclass
class HoldingReview:
    symbol: str
    classification: str
    status: str
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    conviction_now: Optional[float] = None
    conviction_at_purchase: Optional[float] = None
    conviction_change: Optional[float] = None
    detail: dict = field(default_factory=dict)


def review_holding(
    symbol: str,
    classification: str,
    health: HealthResult,
    tri: Optional[TriangulationResult],
    sell_trigger: Optional[TriggerResult],
    conviction_now: Optional[ConvictionScore],
    conviction_at_purchase: Optional[float],
    params: Params,
    growth: Optional[GrowthDeterioration] = None,
    classification_at_purchase: Optional[str] = None,
    anchors_agreed_at_purchase: Optional[bool] = None,
    past_hard_cap: bool = False,
    thesis_broken: bool = False,
) -> HoldingReview:
    """Produce SELL / REVIEW / hold status with every reason recorded."""
    mp = params.monitor
    cp = params.conviction
    now_total = conviction_now.total if conviction_now else None
    change = (
        now_total - conviction_at_purchase
        if now_total is not None and conviction_at_purchase is not None
        else None
    )

    review = HoldingReview(
        symbol=symbol,
        classification=classification,
        status=GREEN,
        conviction_now=now_total,
        conviction_at_purchase=conviction_at_purchase,
        conviction_change=change,
    )

    sell_reasons: list[str] = []
    review_reasons: list[str] = []

    if health.verdict is Verdict.FAIL:
        sell_reasons.append("Module A outright FAIL: " + "; ".join(health.reasons))
    if sell_trigger is not None and sell_trigger.action == SELL:
        sell_reasons.extend(sell_trigger.reasons)
    if thesis_broken:
        sell_reasons.append("thesis-broken condition met")

    if classification in (CORE_GROWTH, SPEC_GROWTH) and growth is not None:
        if growth.quarters_revenue_growth_below_floor >= mp.growth_consecutive_quarters:
            sell_reasons.append(
                f"revenue growth below {mp.growth_min_revenue_growth_pct:.0f}% for "
                f"{growth.quarters_revenue_growth_below_floor} consecutive quarters"
            )
        if growth.quarters_rule_of_40_below_floor >= mp.growth_consecutive_quarters:
            sell_reasons.append(
                f"Rule of 40 below {mp.growth_min_rule_of_40:.0f} for "
                f"{growth.quarters_rule_of_40_below_floor} consecutive quarters"
            )
        if (
            growth.cash_runway_months is not None
            and growth.cash_runway_months < mp.growth_min_cash_runway_months
            and not growth.credible_financing_plan
        ):
            sell_reasons.append(
                f"cash runway {growth.cash_runway_months:.0f} months below "
                f"{mp.growth_min_cash_runway_months:.0f} with no credible financing plan"
            )

    if change is not None and change <= -cp.review_score_drop_points:
        review_reasons.append(
            f"conviction fallen {abs(change):.0f} points since purchase "
            f"({conviction_at_purchase:.0f} -> {now_total:.0f})"
        )
    if health.verdict is Verdict.DATA_GAP:
        review_reasons.append("Module A gates weakening or incomplete: " + "; ".join(health.reasons))
    if tri is not None and tri.anchors_disagree and anchors_agreed_at_purchase:
        review_reasons.append("anchors agreed at purchase and now diverge")
    if tri is not None and tri.own_history.detail.get("step_change_detected"):
        review_reasons.append("re-rating step-change newly detected in C1")
    if past_hard_cap:
        review_reasons.append("position past its hard cap on appreciation")
    if classification_at_purchase and classification_at_purchase != classification:
        review_reasons.append(
            f"classification changed {classification_at_purchase} -> {classification}: the peer "
            "set and valuation method must be rebuilt, not carried forward"
        )

    if sell_reasons:
        review.status = RED
        review.flags.append(SELL_FLAG)
        review.reasons.extend(sell_reasons)
    if review_reasons:
        if review.status != RED:
            review.status = AMBER
        review.flags.append(REVIEW_FLAG)
        review.reasons.extend(review_reasons)

    return review


def quarterly_monitor(reviews: Sequence[HoldingReview]) -> dict:
    return {
        "total": len(reviews),
        "sell": [r.symbol for r in reviews if SELL_FLAG in r.flags],
        "review": [r.symbol for r in reviews if REVIEW_FLAG in r.flags and SELL_FLAG not in r.flags],
        "green": [r.symbol for r in reviews if r.status == GREEN],
    }
