"""Module D — conviction score (fixes v3 flaw 1).

The v3 design double-counted.  Module E's gate already requires a minimum
discount and both anchors confirming; v3's score then re-awarded points for
those same two facts.  Sizing was re-deriving information the gate had already
consumed, and a name that barely cleared the gate still scored 15/30 on
valuation.

Corrected principle, and the invariant every component here is written to
satisfy: **every component measures only what the gate did not already
require.**  A name sitting exactly on its buy gate scores zero on D2 and near
zero on D3 — correctly, because clearing the gate is the entry ticket, not
evidence of conviction.

    Health margin         0-25   how far above A1-A4 thresholds, not merely passing
    Valuation excess      0-30   discount beyond the gate's requirement (D2)
    Anchor conservatism   0-15   how far below fair value the more conservative
                                 anchor places it (D3)
    Business quality      0-20   three sub-components with explicit scales (D4)
    Momentum tiebreaker   0-10   ranked among current passers only (D5)
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Sequence

from ..classification import Classification
from ..config import Config
from ..ledger import AuditLedger
from ..types import CompanyData, MarketData, PeriodFinancials
from .a_health import HealthAssessment, cash_runway_months, share_count_cagr
from .b_discount import DiscountRate
from .c_anchors import AnchorMode, TriangulationResult


@dataclass
class ComponentScore:
    name: str
    points: float
    max_points: float
    basis: str
    detail: dict[str, object] = field(default_factory=dict)

    def as_log_line(self) -> str:
        return f"{self.name}: {self.points:.1f}/{self.max_points:.0f} — {self.basis}"


@dataclass
class ConvictionScore:
    symbol: str
    total: float
    components: list[ComponentScore]
    band: str
    capped_at: float | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def qualifies(self) -> bool:
        """Below 40 does not qualify regardless of gate passes."""
        return self.total >= 40.0

    def component(self, name: str) -> ComponentScore | None:
        for c in self.components:
            if c.name == name:
                return c
        return None

    def as_report_lines(self) -> list[str]:
        lines = [f"CONVICTION {self.total:.1f}/100 — {self.band}"]
        lines.extend(f"  {c.as_log_line()}" for c in self.components)
        if self.capped_at is not None:
            lines.append(
                f"  capped at {self.capped_at:.0f} by SINGLE-ANCHOR MODE"
            )
        lines.extend(f"  {f}" for f in self.flags)
        return lines


def band_for(total: float, config: Config) -> str:
    cfg = config.conviction
    if total >= cfg.high_conviction:
        return "high conviction"
    if total >= cfg.standard_conviction:
        return "standard"
    if total >= cfg.marginal_conviction:
        return "marginal"
    return "does not qualify"


# -- D1: health margin ----------------------------------------------------


def score_health_margin(
    data: CompanyData, health: HealthAssessment, config: Config
) -> ComponentScore:
    """How far above the A1-A4 thresholds, not merely passing.

    Four equal sub-scores of 6.25 points.  Each measures the *margin* by which
    its gate cleared, so a company sitting exactly on every threshold scores
    zero here even though it passed Module A outright.
    """
    max_points = config.conviction.health_margin_max
    per_gate = max_points / 4.0
    parts: dict[str, float] = {}

    # A1 margin: current ratio above 1.0, full marks at 2.0.
    a1 = health.gate("A1")
    if a1 and a1.branch == "current_ratio" and a1.value is not None:
        parts["A1"] = per_gate * min(max((a1.value - 1.0) / 1.0, 0.0), 1.0)
    elif a1 and a1.value is not None and a1.branch == "operating_cash_flow":
        # Cleared on the cash-flow branch; margin is scored on OCF/revenue.
        latest = data.latest_annual
        rev = latest.revenue if latest else None
        ratio = (a1.value / rev) if rev else 0.0
        parts["A1"] = per_gate * min(max(ratio / 0.20, 0.0), 1.0)
    else:
        parts["A1"] = 0.0

    # A2 margin: how far below the grouping's leverage ceiling.
    a2 = health.gate("A2")
    if a2 and a2.value is not None and a2.threshold:
        headroom = (a2.threshold - a2.value) / abs(a2.threshold)
        parts["A2"] = per_gate * min(max(headroom, 0.0), 1.0)
    else:
        parts["A2"] = 0.0

    # A3 margin: cash conversion above the 80% floor, full marks at 130%.
    a3 = health.gate("A3")
    if a3 and a3.branch == "profitable" and a3.value is not None:
        parts["A3"] = per_gate * min(max((a3.value - 0.80) / 0.50, 0.0), 1.0)
    elif a3 and a3.branch == "pre_profit_runway" and a3.value is not None:
        # Runway beyond the 24-month floor, full marks at 60 months.
        parts["A3"] = per_gate * min(max((a3.value - 24.0) / 36.0, 0.0), 1.0)
    elif a3 and a3.branch == "pre_profit_positive_ocf":
        parts["A3"] = per_gate * 0.5
    else:
        parts["A3"] = 0.0

    # A4 margin: no red flags is the pass condition, so the margin is measured
    # on the dilution trend, the one A4 input that is continuous.
    growth = share_count_cagr(data, years=2)
    if growth is None:
        parts["A4"] = 0.0
    else:
        # Full marks for buying back 3%+/yr, zero at the 15% disqualification.
        parts["A4"] = per_gate * min(max((0.15 - growth) / 0.18, 0.0), 1.0)

    total = sum(parts.values())
    return ComponentScore(
        "health margin",
        total,
        max_points,
        "margin above each A-gate threshold, not the pass itself",
        detail=parts,
    )


# -- D2: valuation excess -------------------------------------------------


def score_valuation_excess(
    actual_discount: float, gate_threshold: float, config: Config
) -> ComponentScore:
    """``excess = actual_discount - gate_threshold_X``; score
    ``min(excess / 0.25, 1.0) x 30``.

    A name exactly at its gate scores 0 — correctly, since the gate already
    consumed that.  Full 30 points at gate + 25 percentage points.
    """
    cfg = config.conviction
    excess = actual_discount - gate_threshold
    fraction = min(max(excess / cfg.valuation_excess_denominator, 0.0), 1.0)
    points = fraction * cfg.valuation_excess_max
    reason = (
        f"discount {actual_discount:.1%} vs gate {gate_threshold:.0%} "
        f"= {excess*100:+.1f}pp excess"
    )
    # The score above is the spec's table and is left exactly as written.  But
    # it saturates: excess is capped at gate + 25pp, so a 90% discount and a
    # 60% discount both score a perfect 30.  That means a valuation error
    # large enough to be obviously wrong produces maximum confidence, and
    # nothing downstream notices.  Consolidated Water came through at a 93.4%
    # discount — a $422 fair value against a $27 share price — and scored
    # 30/30.  Say so on the name.
    implausible = actual_discount >= cfg.implausible_discount
    if implausible:
        reason += (
            f" · IMPLAUSIBLE — a {actual_discount:.0%} discount on a company "
            "that cleared every health gate is more often a broken model than "
            "a mispriced market. Check the valuation before the thesis"
        )
    return ComponentScore(
        "valuation excess",
        points,
        cfg.valuation_excess_max,
        reason,
        detail={
            "actual_discount": actual_discount,
            "gate_threshold": gate_threshold,
            "excess_pp": excess * 100.0,
            "implausible": implausible,
        },
    )


# -- D3: anchor conservatism ----------------------------------------------


def score_anchor_conservatism(
    triangulation: TriangulationResult, config: Config
) -> ComponentScore:
    """``min(conservative_discount / 0.40, 1.0) x 15``.

    v3 awarded 15 points for "both anchors confirm" — but the gate already
    requires that, so every passer scored 15 and the component carried zero
    information.  This replaces it with genuinely new information: how far
    below fair value the *more conservative* of the two anchors places it.

    In SINGLE-ANCHOR MODE the score is halved (maximum 7.5) — one anchor
    cannot corroborate itself.
    """
    cfg = config.conviction
    conservative = triangulation.conservative_discount
    if conservative is None:
        return ComponentScore(
            "anchor conservatism",
            0.0,
            cfg.anchor_conservatism_max,
            "no computable anchor discount",
        )
    fraction = min(max(conservative / cfg.anchor_conservatism_denominator, 0.0), 1.0)
    points = fraction * cfg.anchor_conservatism_max
    basis = f"conservative anchor implies {conservative:.1%} discount"
    if triangulation.mode.is_single:
        points *= config.anchors.single_anchor_conservatism_multiplier
        basis += " · halved for SINGLE-ANCHOR MODE (one anchor cannot corroborate itself)"
    return ComponentScore(
        "anchor conservatism",
        points,
        cfg.anchor_conservatism_max,
        basis,
        detail={
            "conservative_discount": conservative,
            "mode": triangulation.mode.value,
        },
    )


# -- D4: business quality -------------------------------------------------


def _roic(data: CompanyData) -> float | None:
    latest = data.latest_annual
    if latest is None:
        return None
    # EBIT from pre-tax income + interest for filers with no operating-income
    # line; 13% of non-financial company-dates in a 400-company check.
    operating = latest.operating_income if latest.operating_income is not None else latest.ebit
    if operating is None:
        return None
    invested = latest.invested_capital
    if invested is None:
        if latest.total_equity is None or latest.total_debt is None:
            return None
        invested = latest.total_equity + latest.total_debt - (
            latest.cash_and_equivalents or 0.0
        )
    if not invested or invested <= 0:
        return None
    tax_rate = 0.21
    if latest.tax_expense is not None and latest.pretax_income and latest.pretax_income > 0:
        candidate = latest.tax_expense / latest.pretax_income
        if 0.0 <= candidate <= 0.60:
            tax_rate = candidate
    return operating * (1.0 - tax_rate) / invested


def _roe(data: CompanyData) -> float | None:
    latest = data.latest_annual
    if latest is None or latest.net_income is None:
        return None
    if not latest.total_equity or latest.total_equity <= 0:
        return None
    return latest.net_income / latest.total_equity


def _gross_margin_stdev(data: CompanyData) -> float | None:
    margins = []
    for row in data.trailing_quarters(8):
        if row.gross_profit is None or not row.revenue or row.revenue <= 0:
            continue
        margins.append(row.gross_profit / row.revenue)
    if len(margins) < 8:
        return None
    return statistics.stdev(margins)


def _combined_ratio_stdev(data: CompanyData) -> float | None:
    ratios = [
        r.combined_ratio for r in data.trailing_quarters(8) if r.combined_ratio is not None
    ]
    if len(ratios) < 8:
        ratios = [
            r.combined_ratio for r in data.trailing_years(5) if r.combined_ratio is not None
        ]
    if len(ratios) < 3:
        return None
    return statistics.stdev(ratios)


def score_business_quality(
    data: CompanyData,
    market: MarketData,
    config: Config,
    classification: Classification,
    discount_rate: DiscountRate | None,
    ledger: AuditLedger | None = None,
) -> ComponentScore:
    """Three sub-components with explicit point scales (fixes v3 flaw 5).

        ROIC minus WACC spread   0-10   0 pts at 0pp · 10 pts at 10pp+ · linear
        Gross margin stability   0-5    8-quarter stdev vs group median stdev
        Share count trend        0-5    shrinking >=2%/yr scores full marks

    Where a sub-component is not computable (ROIC on a bank, gross margin on an
    insurer), the classification-appropriate substitute is awarded and the
    substitution is logged.
    """
    cfg = config.conviction
    parts: dict[str, float] = {}
    notes: list[str] = []

    # 1. ROIC minus WACC spread, or ROE minus cost of equity for banks.
    spread: float | None = None
    if classification is Classification.FINANCIAL_BANK:
        roe = _roe(data)
        if roe is not None and discount_rate is not None:
            spread = roe - discount_rate.cost_of_equity
            notes.append("D4 substitution: ROE minus cost of equity (bank)")
    else:
        roic = _roic(data)
        if roic is not None and discount_rate is not None:
            spread = roic - discount_rate.rate
    if spread is None:
        parts["spread"] = 0.0
        notes.append("D4: return spread not computable — scored 0")
    else:
        parts["spread"] = 10.0 * min(max(spread / cfg.roic_spread_full_marks, 0.0), 1.0)

    # 2. Gross margin stability, or combined-ratio stability for insurers.
    group = (
        data.profile.gics_sub_industry_code
        or data.profile.sub_industry
        or data.profile.industry
        or ""
    )
    group_median = market.group_gross_margin_stdev_median.get(group)
    if classification is Classification.INSURER:
        own = _combined_ratio_stdev(data)
        notes.append("D4 substitution: combined-ratio stability (insurer)")
    else:
        own = _gross_margin_stdev(data)

    if own is None or group_median is None or group_median <= 0:
        parts["stability"] = 0.0
        notes.append("D4: margin stability not computable — scored 0")
    else:
        ratio = own / group_median
        if ratio <= 0.5:
            parts["stability"] = 5.0
        elif ratio <= 1.0:
            parts["stability"] = 3.0
        elif ratio <= 1.5:
            parts["stability"] = 1.0
        else:
            parts["stability"] = 0.0

    # 3. Share count trend.
    growth = share_count_cagr(data, years=2)
    if growth is None:
        parts["share_count"] = 0.0
        notes.append("D4: share count history unavailable — scored 0")
    elif growth <= -0.02:
        parts["share_count"] = 5.0
    elif growth <= 0.0:
        parts["share_count"] = 3.0
    elif growth <= 0.03:
        parts["share_count"] = 1.0
    else:
        parts["share_count"] = 0.0

    if ledger is not None:
        for note in notes:
            ledger.note(note)

    return ComponentScore(
        "business quality",
        sum(parts.values()),
        cfg.business_quality_max,
        "return spread + margin stability + share count trend",
        detail={**parts, "notes": notes, "return_spread": spread},
    )


# -- D5: momentum tiebreaker ----------------------------------------------


def total_return_12_1(data: CompanyData) -> float | None:
    """12-1 month total return: the twelve-month move excluding the last month.

    Excluding the most recent month is standard practice — short-horizon
    reversal runs opposite to momentum and would otherwise contaminate the rank.
    """
    prices = sorted(data.prices, key=lambda p: p.price_date, reverse=True)
    if len(prices) < 2:
        return None
    latest_date = prices[0].price_date

    def _nearest(target_days: int):
        target = latest_date.toordinal() - target_days
        candidates = [p for p in prices if p.price_date.toordinal() <= target]
        return candidates[0] if candidates else None

    start = _nearest(365)
    end = _nearest(30)
    if start is None or end is None:
        return None
    start_px = start.adjusted_close or start.close
    end_px = end.adjusted_close or end.close
    if not start_px or start_px <= 0:
        return None
    return end_px / start_px - 1.0


def score_momentum(
    symbol: str,
    returns_by_symbol: dict[str, float | None],
    config: Config,
) -> ComponentScore:
    """12-1 month total return, ranked among current passers only.

    If fewer than 3 passers exist in the current cycle, award a neutral 5
    points to all — ranking a set of one or two is meaningless and would
    otherwise hand an arbitrary 10 or 0 (fixes v3 flaw 10).  Never a gate.
    """
    cfg = config.conviction
    ranked = {s: r for s, r in returns_by_symbol.items() if r is not None}

    if len(ranked) < cfg.momentum_min_passers:
        return ComponentScore(
            "momentum tiebreaker",
            cfg.momentum_neutral_score,
            cfg.momentum_max,
            f"neutral — only {len(ranked)} ranked passer(s) this cycle, "
            f"fewer than the {cfg.momentum_min_passers} a ranking needs",
            detail={"passers": len(ranked), "neutral": True},
        )

    own = ranked.get(symbol)
    if own is None:
        return ComponentScore(
            "momentum tiebreaker",
            cfg.momentum_neutral_score,
            cfg.momentum_max,
            "neutral — 12-1 month return not computable for this name",
            detail={"neutral": True},
        )

    ordered = sorted(ranked.values())
    below = sum(1 for r in ordered if r < own)
    percentile = below / (len(ordered) - 1) if len(ordered) > 1 else 0.5
    points = percentile * cfg.momentum_max
    return ComponentScore(
        "momentum tiebreaker",
        points,
        cfg.momentum_max,
        f"12-1 return {own:+.1%}, rank {below + 1} of {len(ordered)} passers",
        detail={"return_12_1": own, "rank": below + 1, "passers": len(ordered)},
    )


# -- assembly -------------------------------------------------------------


def score_conviction(
    data: CompanyData,
    market: MarketData,
    config: Config,
    classification: Classification,
    health: HealthAssessment,
    triangulation: TriangulationResult,
    actual_discount: float,
    gate_threshold: float,
    discount_rate: DiscountRate | None,
    momentum_returns: dict[str, float | None] | None = None,
    ledger: AuditLedger | None = None,
) -> ConvictionScore:
    """Assemble the five components and apply C5's cap."""
    components = [
        score_health_margin(data, health, config),
        score_valuation_excess(actual_discount, gate_threshold, config),
        score_anchor_conservatism(triangulation, config),
        score_business_quality(
            data, market, config, classification, discount_rate, ledger
        ),
        score_momentum(
            data.profile.symbol, momentum_returns or {}, config
        ),
    ]
    total = sum(c.points for c in components)

    flags: list[str] = []
    capped_at: float | None = None
    if triangulation.mode.is_single:
        cap = config.anchors.single_anchor_conviction_cap
        if total > cap:
            flags.append(
                f"conviction {total:.1f} capped to {cap:.0f} — a SINGLE-ANCHOR "
                "name can never be sized as high-conviction"
            )
            total = cap
        capped_at = cap

    score = ConvictionScore(
        symbol=data.profile.symbol,
        total=total,
        components=components,
        band=band_for(total, config),
        capped_at=capped_at,
        flags=flags,
    )
    if ledger is not None:
        for line in score.as_report_lines():
            ledger.note(f"D · {line}")
    return score


__all__ = [
    "ConvictionScore",
    "ComponentScore",
    "score_conviction",
    "score_health_margin",
    "score_valuation_excess",
    "score_anchor_conservatism",
    "score_business_quality",
    "score_momentum",
    "total_return_12_1",
    "band_for",
]
