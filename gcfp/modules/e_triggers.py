"""Module E -- buy / sell triggers.

Price is never a buy or sell reason on its own. Valuation gaps and fundamental
deterioration trigger action; momentum, moving averages and RSI are permitted
only as entry-timing hygiene, never as the reason itself.

Anything computable from the price chart alone is banned as a trigger in either
direction. The working test, applied to every rule proposed for this module: if
it can fire without opening a filing, it does not belong here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from ..config import Params
from ..modules.c_triangulation import TriangulationResult
from ..modules.d_conviction import ConvictionScore
from ..types import Verdict


BUY = "BUY"
SELL = "SELL"
REVIEW = "REVIEW"
HOLD = "HOLD"
NO_ACTION = "NO ACTION"


@dataclass
class TriggerResult:
    symbol: str
    action: str
    reasons: list[str] = field(default_factory=list)
    conditions: dict = field(default_factory=dict)
    deferred: bool = False
    deferral_reason: Optional[str] = None

    @property
    def is_buy(self) -> bool:
        return self.action == BUY and not self.deferred


def discount_to_fair_value_pct(price: Optional[float], fair_value: Optional[float]) -> Optional[float]:
    if price is None or fair_value is None or fair_value <= 0:
        return None
    return 100.0 * (1.0 - price / fair_value)


def premium_to_fair_value_pct(price: Optional[float], fair_value: Optional[float]) -> Optional[float]:
    if price is None or fair_value is None or fair_value <= 0:
        return None
    return 100.0 * (price / fair_value - 1.0)


def in_earnings_blackout(
    as_of: date, next_earnings: Optional[date], params: Params
) -> bool:
    """Ten trading days before a scheduled earnings date.

    Approximated as fourteen calendar days, which is the conservative direction:
    it defers slightly more often than the letter of the rule requires.
    """
    if next_earnings is None:
        return False
    window_days = round(params.triggers.earnings_blackout_trading_days * 1.4)
    return as_of <= next_earnings <= as_of + timedelta(days=window_days)


def evaluate_buy(
    symbol: str,
    classification: str,
    price: Optional[float],
    fair_value: Optional[float],
    tri: Optional[TriangulationResult],
    conviction: Optional[ConvictionScore],
    params: Params,
    as_of: Optional[date] = None,
    next_earnings: Optional[date] = None,
) -> TriggerResult:
    """All three conditions required. No two out of three."""
    as_of = as_of or date.today()
    tp = params.triggers
    required_discount = tp.buy_discount_pct.get(classification)
    discount = discount_to_fair_value_pct(price, fair_value)
    score = conviction.total if conviction else None

    cond_discount = (
        discount is not None and required_discount is not None and discount >= required_discount
    )
    cond_anchors = bool(tri and tri.both_confirm_undervalued)
    cond_conviction = score is not None and score >= tp.min_conviction_to_buy

    conditions = {
        "1_discount": {
            "required_pct": required_discount,
            "actual_pct": discount,
            "met": cond_discount,
        },
        "2_both_anchors_confirm": {
            "own_history": tri.own_history.direction.value if tri else None,
            "peer": tri.peer.direction.value if tri else None,
            "anchors_disagree": tri.anchors_disagree if tri else None,
            "met": cond_anchors,
        },
        "3_conviction": {
            "required": tp.min_conviction_to_buy,
            "actual": score,
            "met": cond_conviction,
        },
    }

    reasons: list[str] = []
    if not cond_discount:
        if discount is None:
            reasons.append("discount to fair value not computable")
        else:
            reasons.append(
                f"discount {discount:.1f}% below the {required_discount:.0f}% required for {classification}"
            )
    if not cond_anchors:
        if tri is None:
            reasons.append("no triangulation available")
        elif tri.anchors_disagree:
            reasons.append("anchors disagree -- no combined verdict")
        else:
            reasons.append("both anchors do not independently confirm undervaluation")
    if not cond_conviction:
        reasons.append(
            f"conviction {score if score is not None else 'n/a'} below {tp.min_conviction_to_buy:.0f}"
        )

    if not (cond_discount and cond_anchors and cond_conviction):
        return TriggerResult(symbol, NO_ACTION, reasons, conditions)

    result = TriggerResult(symbol, BUY, ["all three buy conditions met"], conditions)
    if in_earnings_blackout(as_of, next_earnings, params):
        result.deferred = True
        result.deferral_reason = (
            f"earnings scheduled {next_earnings.isoformat()} -- alert deferred, verdict unchanged. "
            "The name stays on the pass list; only the alert waits."
        )
    return result


def evaluate_sell(
    symbol: str,
    classification: str,
    price: Optional[float],
    fair_value: Optional[float],
    tri: Optional[TriangulationResult],
    health_verdict: Verdict,
    params: Params,
) -> TriggerResult:
    """Either an outright Module A failure, or overvaluation on both anchors."""
    tp = params.triggers
    required_premium = tp.sell_premium_pct.get(classification)
    premium = premium_to_fair_value_pct(price, fair_value)

    conditions = {
        "module_a_fail": health_verdict is Verdict.FAIL,
        "premium": {"required_pct": required_premium, "actual_pct": premium},
        "both_anchors_overvalued": bool(tri and tri.both_confirm_overvalued),
    }

    if health_verdict is Verdict.FAIL:
        return TriggerResult(
            symbol, SELL, ["Module A outright FAIL on re-run -- fundamental deterioration"], conditions
        )

    overvalued = (
        premium is not None
        and required_premium is not None
        and premium >= required_premium
        and tri is not None
        and tri.both_confirm_overvalued
    )
    if overvalued:
        return TriggerResult(
            symbol, SELL,
            [f"price {premium:.1f}% above fair value with both anchors confirming overvaluation"],
            conditions,
        )

    # Mixed signal: exactly one anchor confirms overvaluation.
    if tri is not None and not tri.anchors_disagree:
        singles = [a for a in (tri.own_history, tri.peer) if a.confirms_overvalued]
        if len(singles) == 1 and premium is not None and required_premium is not None and premium >= required_premium:
            return TriggerResult(
                symbol, REVIEW,
                [
                    "REVIEW -- mixed valuation signal. "
                    f"own-history {tri.own_history.implied_upside_pct:+.0f}% vs "
                    f"peers {tri.peer.implied_upside_pct:+.0f}%; no automatic trigger, human judgment."
                ],
                conditions,
            )
    if tri is not None and tri.anchors_disagree:
        return TriggerResult(
            symbol, REVIEW, ["REVIEW -- anchors disagree; no automatic trigger"], conditions
        )

    return TriggerResult(symbol, HOLD, ["no sell condition met"], conditions)
