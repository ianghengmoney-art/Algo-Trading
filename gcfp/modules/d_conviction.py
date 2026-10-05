"""Module D -- conviction score.

Exists because "which position do I have least conviction in" is unanswerable
by gut feel, and because sizing (F) and trimming (H) both need a rankable
number rather than a binary pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional, Sequence

from ..config import Params
from ..modules.a_health import HealthResult, sorted_annuals
from ..modules.c_triangulation import TriangulationResult
from ..types import CandidateData, Financials, PeriodType, PricePoint


@dataclass
class ConvictionScore:
    symbol: str
    health_margin: float = 0.0
    valuation_margin: float = 0.0
    anchor_agreement: float = 0.0
    business_quality: float = 0.0
    momentum: float = 0.0
    momentum_pending: bool = True
    detail: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return round(
            self.health_margin
            + self.valuation_margin
            + self.anchor_agreement
            + self.business_quality
            + self.momentum,
            1,
        )

    def band(self, params: Params) -> str:
        cp = params.conviction
        total = self.total
        if total >= cp.band_high_conviction:
            return "high conviction"
        if total >= cp.band_standard:
            return "standard"
        if total >= cp.band_marginal:
            return "marginal -- review before acting"
        return "does not qualify for a position"

    def breakdown(self) -> dict:
        return {
            "health_margin": self.health_margin,
            "valuation_margin": self.valuation_margin,
            "anchor_agreement": self.anchor_agreement,
            "business_quality": self.business_quality,
            "momentum": self.momentum,
            "total": self.total,
        }


def _health_margin_points(health: HealthResult, params: Params) -> tuple[float, dict]:
    """How far *above* the A1-A4 thresholds, not merely passing.

    A company at four times the required cash runway scores higher than one
    barely clearing 24 months.
    """
    cp = params.conviction
    margins = health.margins()
    scored = {k: min(1.0, max(0.0, v)) for k, v in margins.items() if k in ("A1", "A2", "A3", "A4")}
    if not scored:
        return 0.0, {"margins": {}, "note": "no gate margins available"}
    points = cp.health_margin_max * sum(scored.values()) / len(scored)
    return points, {"margins": scored, "gates_scored": sorted(scored)}


def _valuation_margin_points(
    price: Optional[float], fair_value: Optional[float], params: Params
) -> tuple[float, dict]:
    cp = params.conviction
    if price is None or fair_value is None or fair_value <= 0 or price <= 0:
        return 0.0, {"discount_pct": None}
    discount_pct = 100.0 * (1.0 - price / fair_value)
    if discount_pct <= 0:
        return 0.0, {"discount_pct": discount_pct}
    scaled = min(1.0, discount_pct / cp.valuation_full_points_discount_pct)
    return cp.valuation_margin_max * scaled, {"discount_pct": discount_pct}


def _anchor_points(tri: Optional[TriangulationResult], params: Params) -> tuple[float, dict]:
    cp = params.conviction
    if tri is None:
        return 0.0, {"state": "no triangulation"}
    if tri.anchors_disagree:
        return cp.anchor_divergent_points, {"state": "divergence flagged"}
    confirming = tri.confirming_count
    if confirming >= 2:
        return cp.anchor_both_confirm_points, {"state": "both anchors confirm undervaluation"}
    if confirming == 1:
        return cp.anchor_one_confirm_points, {"state": "one anchor confirms undervaluation"}
    return 0.0, {"state": "neither anchor confirms undervaluation"}


def _gross_margin_stability(quarters: Sequence[Financials]) -> Optional[float]:
    """Coefficient of variation of gross margin over the last eight quarters.

    Returns ``None`` when eight quarters are not available: the sub-component
    scores zero rather than being estimated from four.
    """
    usable = [
        q for q in quarters
        if q.gross_profit is not None and q.revenue not in (None, 0)
    ][:8]
    if len(usable) < 8:
        return None
    margins = [100.0 * q.gross_profit / q.revenue for q in usable]
    mean = sum(margins) / len(margins)
    if mean == 0:
        return None
    variance = sum((m - mean) ** 2 for m in margins) / len(margins)
    return (variance ** 0.5) / abs(mean)


def _business_quality_points(
    candidate: CandidateData, params: Params, discount_rate_pct: Optional[float]
) -> tuple[float, dict]:
    """ROIC against cost of capital, gross margin stability, share count trend."""
    cp = params.conviction
    annuals = sorted_annuals(candidate)
    detail: dict = {}
    earned = 0.0
    per_component = cp.business_quality_max / 3.0

    # ROIC vs cost of capital.
    roic_pct = None
    if annuals:
        latest = annuals[0]
        if (
            latest.operating_income is not None
            and latest.total_equity is not None
            and latest.total_debt is not None
        ):
            invested = latest.total_equity + latest.total_debt - (latest.cash_and_equivalents or 0.0)
            if invested > 0:
                tax_rate = 0.21
                if latest.pretax_income and latest.tax_expense is not None and latest.pretax_income > 0:
                    tax_rate = min(0.5, max(0.0, latest.tax_expense / latest.pretax_income))
                roic_pct = 100.0 * latest.operating_income * (1 - tax_rate) / invested
    detail["roic_pct"] = roic_pct
    detail["cost_of_capital_pct"] = discount_rate_pct
    if roic_pct is not None and discount_rate_pct:
        spread = roic_pct - discount_rate_pct
        detail["roic_spread_pp"] = spread
        earned += per_component * min(1.0, max(0.0, spread / 10.0))

    # Gross margin stability over eight quarters.
    quarters = sorted(
        [q for q in candidate.quarterly if q.period_type is PeriodType.QUARTER],
        key=lambda q: q.period_end,
        reverse=True,
    )
    cv = _gross_margin_stability(quarters)
    detail["gross_margin_cv"] = cv
    if cv is not None:
        # A coefficient of variation at or below 5% earns the component in full.
        earned += per_component * min(1.0, max(0.0, 1.0 - cv / 0.05))
    else:
        detail["gross_margin_note"] = "fewer than 8 quarters available -- component scored zero"

    # Share count trend: buybacks positive, dilution negative.
    share_change_pct = None
    if len(annuals) >= 3:
        latest_shares = annuals[0].shares_diluted
        base_shares = annuals[2].shares_diluted
        if latest_shares and base_shares and base_shares > 0:
            share_change_pct = 100.0 * (latest_shares / base_shares - 1.0)
    detail["share_count_change_pct_2y"] = share_change_pct
    if share_change_pct is not None:
        # -5% or better (net buyback) earns the component; +5% or worse earns none.
        normalised = (5.0 - share_change_pct) / 10.0
        earned += per_component * min(1.0, max(0.0, normalised))

    return earned, detail


def total_return_12_1(candidate: CandidateData) -> Optional[float]:
    """12-1 month total return: the trailing year, excluding the latest month.

    The most recent month is dropped because short-horizon reversal is noise
    for this purpose, and this component is only ever a tiebreaker between
    names that have already passed every gate.
    """
    points = sorted(candidate.price_history, key=lambda p: p.observed_on)
    if len(points) < 2:
        return None
    end = points[-1].observed_on

    def nearest(target: date) -> Optional[PricePoint]:
        return min(points, key=lambda p: abs((p.observed_on - target).days), default=None)

    recent = nearest(end - timedelta(days=30))
    year_ago = nearest(end - timedelta(days=365))
    if recent is None or year_ago is None or year_ago.close <= 0:
        return None
    # Guard against a series too short to actually span the window.
    if (recent.observed_on - year_ago.observed_on).days < 180:
        return None
    return 100.0 * (recent.close / year_ago.close - 1.0)


def score_conviction(
    candidate: CandidateData,
    health: HealthResult,
    fair_value: Optional[float],
    tri: Optional[TriangulationResult],
    params: Params,
    discount_rate_pct: Optional[float] = None,
) -> ConvictionScore:
    """Score everything except momentum, which is cross-sectional."""
    price = candidate.profile.price
    health_pts, health_detail = _health_margin_points(health, params)
    val_pts, val_detail = _valuation_margin_points(price, fair_value, params)
    anchor_pts, anchor_detail = _anchor_points(tri, params)
    quality_pts, quality_detail = _business_quality_points(candidate, params, discount_rate_pct)

    return ConvictionScore(
        symbol=candidate.symbol,
        health_margin=round(health_pts, 1),
        valuation_margin=round(val_pts, 1),
        anchor_agreement=round(anchor_pts, 1),
        business_quality=round(quality_pts, 1),
        momentum=0.0,
        momentum_pending=True,
        detail={
            "health": health_detail,
            "valuation": val_detail,
            "anchors": anchor_detail,
            "quality": quality_detail,
        },
    )


def apply_momentum_ranking(
    scores: Sequence[ConvictionScore],
    returns: dict[str, Optional[float]],
    params: Params,
) -> None:
    """Rank 12-1 month return among current passers only.

    Never a gate. It can only order names that already passed everything else,
    so it is applied after scoring and only across the surviving set.
    """
    cp = params.conviction
    ranked = [(sym, r) for sym, r in returns.items() if r is not None]
    ranked.sort(key=lambda item: item[1])
    positions = {sym: i for i, (sym, _) in enumerate(ranked)}
    n = len(ranked)

    for score in scores:
        score.momentum_pending = False
        if score.symbol not in positions or n <= 1:
            score.momentum = 0.0
            score.notes.append("momentum: no rankable return among current passers -- 0 points")
            continue
        percentile = positions[score.symbol] / (n - 1)
        score.momentum = round(cp.momentum_max * percentile, 1)
        score.detail.setdefault("momentum", {})
        score.detail["momentum"] = {
            "return_12_1_pct": returns[score.symbol],
            "rank_percentile": 100.0 * percentile,
            "ranked_against": n,
        }
