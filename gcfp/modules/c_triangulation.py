"""Module C -- dual triangulation.

Two anchors, computed independently, never averaged. Disagreement between them
is reported and flagged, and is often the most informative output the system
produces: it distinguishes *the company re-rated* (peer anchor moves, own
history does not) from *the whole sector re-rated* (both move together, meaning
"cheap versus peers" may just mean the peer group is expensive).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Sequence

from ..config import (
    CORE_GROWTH,
    CORE_STABLE,
    FINANCIAL_BANK,
    INSURER,
    Params,
    REIT,
    SPEC_GROWTH,
)
from ..types import Direction, MultipleSeries

# The classification-appropriate multiple for the own-history anchor.
MULTIPLE_FOR_CLASSIFICATION = {
    CORE_STABLE: "trailing P/E",
    FINANCIAL_BANK: "P/B",
    CORE_GROWTH: "forward P/E",
    SPEC_GROWTH: "EV/Revenue",
    REIT: "P/AFFO",
    INSURER: "P/B",
}

# Every multiple this module handles is "lower is cheaper". Recorded explicitly
# so that adding a yield-style multiple later cannot silently invert a verdict.
LOWER_IS_CHEAPER = True


@dataclass
class AnchorReading:
    anchor: str
    multiple_name: str
    current_multiple: Optional[float]
    reference: Optional[float]
    implied_upside_pct: Optional[float]
    direction: Direction
    confidence: str = "full"
    detail: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.implied_upside_pct is not None

    @property
    def confirms_undervalued(self) -> bool:
        return self.direction is Direction.UNDERVALUED

    @property
    def confirms_overvalued(self) -> bool:
        return self.direction is Direction.OVERVALUED


@dataclass
class PeerCandidate:
    symbol: str
    sector: Optional[str]
    market_cap: Optional[float]
    revenue_growth_pct: Optional[float]
    multiple: Optional[float]


@dataclass
class PeerSelection:
    included: list[PeerCandidate] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)

    def log_lines(self) -> list[str]:
        lines = [f"included: {', '.join(p.symbol for p in self.included) or 'none'}"]
        lines += [f"excluded {sym}: {reason}" for sym, reason in self.excluded]
        return lines


@dataclass
class TriangulationResult:
    symbol: str
    classification: str
    own_history: AnchorReading
    peer: AnchorReading
    peer_selection: PeerSelection
    pegy: Optional[float]
    pegy_note: str
    divergence_pp: Optional[float]
    anchors_disagree: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def both_confirm_undervalued(self) -> bool:
        if self.anchors_disagree:
            return False
        return self.own_history.confirms_undervalued and self.peer.confirms_undervalued

    @property
    def both_confirm_overvalued(self) -> bool:
        if self.anchors_disagree:
            return False
        return self.own_history.confirms_overvalued and self.peer.confirms_overvalued

    @property
    def confirming_count(self) -> int:
        if self.anchors_disagree:
            return 0
        return sum(
            1 for a in (self.own_history, self.peer) if a.confirms_undervalued
        )

    @property
    def combined_verdict(self) -> str:
        """Refused outright when the anchors disagree."""
        if self.anchors_disagree:
            return "NO COMBINED VERDICT -- ANCHORS DISAGREE"
        if self.both_confirm_undervalued:
            return "UNDERVALUED ON BOTH ANCHORS"
        if self.both_confirm_overvalued:
            return "OVERVALUED ON BOTH ANCHORS"
        return "MIXED -- ONE ANCHOR ONLY"


# ---------------------------------------------------------------------------
# C1 -- own-history anchor
# ---------------------------------------------------------------------------


@dataclass
class StepChange:
    detected: bool
    breakpoint: Optional[date]
    before_mean: Optional[float]
    after_mean: Optional[float]
    separation_sigma: Optional[float]


def detect_step_change(values: Sequence[float], dates: Sequence[date], params: Params) -> StepChange:
    """Scan for a sustained shift in the multiple's level, not noise.

    Every split point with enough observations on both sides is tested, and the
    one with the largest mean separation relative to pooled dispersion wins. A
    break means the own-history anchor may be anchored to a business that no
    longer exists -- the Micron DRAM-to-HBM and Apple hardware-to-Services
    problem. Without this scan the anchor silently misleads.
    """
    tp = params.triangulation
    n = len(values)
    min_seg = tp.c1_min_segment_points
    if n < 2 * min_seg:
        return StepChange(False, None, None, None, None)

    best = StepChange(False, None, None, None, 0.0)
    for split in range(min_seg, n - min_seg + 1):
        before, after = values[:split], values[split:]
        mean_b, mean_a = statistics.fmean(before), statistics.fmean(after)
        var_b = statistics.pvariance(before) if len(before) > 1 else 0.0
        var_a = statistics.pvariance(after) if len(after) > 1 else 0.0
        pooled = math.sqrt((len(before) * var_b + len(after) * var_a) / n)
        if pooled <= 0:
            separation = math.inf if mean_b != mean_a else 0.0
        else:
            separation = abs(mean_a - mean_b) / pooled
        if best.separation_sigma is None or separation > best.separation_sigma:
            best = StepChange(
                separation >= tp.c1_step_change_sigma,
                dates[split],
                mean_b,
                mean_a,
                separation,
            )
    return best


def own_history_anchor(
    series: Optional[MultipleSeries],
    current_multiple: Optional[float],
    params: Params,
    classification: str,
) -> AnchorReading:
    """Z-score and percentile of today's multiple within its own history."""
    tp = params.triangulation
    multiple_name = MULTIPLE_FOR_CLASSIFICATION.get(classification, "multiple")

    if series is None or not series.points:
        return AnchorReading(
            "own-history", multiple_name, current_multiple, None, None, Direction.UNKNOWN,
            confidence="unavailable",
            warnings=["No historical multiple series -- Module C1 cannot run."],
        )

    years = series.years_covered
    warnings: list[str] = []
    confidence = "full"
    if years < tp.c1_reduced_confidence_window_years:
        return AnchorReading(
            "own-history", series.multiple_name, current_multiple, None, None, Direction.UNKNOWN,
            confidence="unavailable",
            detail={"years_covered": years},
            warnings=[
                f"Only {years:.1f}y of history; {tp.c1_reduced_confidence_window_years}y is the "
                "floor for any own-history read. Not falling back to a shorter window."
            ],
        )
    if years < tp.c1_min_window_years:
        confidence = "reduced"
        warnings.append(
            f"REDUCED CONFIDENCE -- {years:.1f}y of history against the "
            f"{tp.c1_min_window_years}y standard (short listing history)."
        )

    points = sorted(series.points, key=lambda p: p.observed_on)
    values = [p.value for p in points]
    dates = [p.observed_on for p in points]

    mean = statistics.fmean(values)
    median = statistics.median(values)
    stdev = statistics.pstdev(values) if len(values) > 1 else 0.0

    z_score = None
    if stdev > 0 and current_multiple is not None:
        z_score = (current_multiple - mean) / stdev

    percentile = None
    if current_multiple is not None and values:
        below = sum(1 for v in values if v < current_multiple)
        percentile = 100.0 * below / len(values)

    implied_upside_pct = None
    direction = Direction.UNKNOWN
    if current_multiple is not None and current_multiple > 0 and median > 0:
        implied_upside_pct = 100.0 * (median / current_multiple - 1.0)
        direction = (
            Direction.UNDERVALUED
            if implied_upside_pct > 0
            else Direction.OVERVALUED
            if implied_upside_pct < 0
            else Direction.FAIR
        )

    step = detect_step_change(values, dates, params)
    if step.detected:
        warnings.append(
            "POSSIBLE RE-RATING -- own-history anchor may be anchored to a business that no "
            f"longer exists. Level shifted from {step.before_mean:.1f} to {step.after_mean:.1f} "
            f"around {step.breakpoint.isoformat()} "
            f"({step.separation_sigma:.1f} pooled sd)."
        )

    return AnchorReading(
        anchor="own-history",
        multiple_name=series.multiple_name,
        current_multiple=current_multiple,
        reference=median,
        implied_upside_pct=implied_upside_pct,
        direction=direction,
        confidence=confidence,
        detail={
            "years_covered": years,
            "observations": len(values),
            "mean": mean,
            "median": median,
            "stdev": stdev,
            "z_score": z_score,
            "percentile": percentile,
            "step_change_detected": step.detected,
            "step_change_breakpoint": step.breakpoint.isoformat() if step.breakpoint else None,
            "step_change_sigma": step.separation_sigma,
        },
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# C2 -- industry-peer anchor
# ---------------------------------------------------------------------------


def select_peers(
    subject: PeerCandidate,
    candidates: Sequence[PeerCandidate],
    params: Params,
) -> PeerSelection:
    """Identify genuine peers, logging every rejection.

    Peer selection is the single most gameable step in any valuation system.
    The exclusion log is what makes it auditable, so every considered name
    leaves a record whether it made the cut or not.
    """
    tp = params.triangulation
    selection = PeerSelection()

    scored: list[tuple[float, PeerCandidate]] = []
    for peer in candidates:
        if peer.symbol == subject.symbol:
            selection.excluded.append((peer.symbol, "is the subject company"))
            continue
        if subject.sector and peer.sector and peer.sector != subject.sector:
            selection.excluded.append(
                (peer.symbol, f"different sector ({peer.sector} vs {subject.sector})")
            )
            continue
        if peer.multiple is None or peer.multiple <= 0:
            selection.excluded.append((peer.symbol, "no usable multiple"))
            continue
        if peer.market_cap is None or subject.market_cap is None:
            selection.excluded.append((peer.symbol, "market cap unavailable for size band test"))
            continue
        ratio = peer.market_cap / subject.market_cap
        if not (tp.c2_market_cap_low_multiple <= ratio <= tp.c2_market_cap_high_multiple):
            selection.excluded.append(
                (peer.symbol, f"market cap {ratio:.2f}x subject, outside "
                 f"{tp.c2_market_cap_low_multiple}-{tp.c2_market_cap_high_multiple}x band")
            )
            continue
        if subject.revenue_growth_pct is None or peer.revenue_growth_pct is None:
            selection.excluded.append((peer.symbol, "revenue growth unavailable for growth band test"))
            continue
        gap = abs(peer.revenue_growth_pct - subject.revenue_growth_pct)
        if gap > tp.c2_revenue_growth_band_pp:
            selection.excluded.append(
                (peer.symbol, f"revenue growth {gap:.1f}pp from subject, outside "
                 f"{tp.c2_revenue_growth_band_pp:.0f}pp band")
            )
            continue
        scored.append((abs(math.log(ratio)) if ratio > 0 else math.inf, peer))

    scored.sort(key=lambda item: item[0])
    selection.included = [peer for _, peer in scored[: tp.c2_max_peers]]
    for _, peer in scored[tp.c2_max_peers :]:
        selection.excluded.append(
            (peer.symbol, f"qualified but outside the closest {tp.c2_max_peers} by size")
        )
    return selection


def peer_anchor(
    subject: PeerCandidate,
    selection: PeerSelection,
    params: Params,
    multiple_name: str,
) -> AnchorReading:
    """Company multiple versus the peer *median* -- never the mean.

    One outlier distorts an average, and the outlier is usually the name that
    most wants to be in the comparison.
    """
    tp = params.triangulation
    peers = selection.included
    detail = {
        "peer_count": len(peers),
        "peers": [p.symbol for p in peers],
        "excluded_count": len(selection.excluded),
    }

    if len(peers) < tp.c2_min_peers:
        return AnchorReading(
            "peer", multiple_name, subject.multiple, None, None, Direction.UNKNOWN,
            confidence="unavailable",
            detail=detail,
            warnings=[
                f"Only {len(peers)} genuine peers found, {tp.c2_min_peers} required. "
                "Not widening the bands to reach a quorum."
            ],
        )

    multiples = [p.multiple for p in peers if p.multiple is not None]
    median = statistics.median(multiples)
    detail.update(
        {
            "peer_median": median,
            "peer_multiples": {p.symbol: p.multiple for p in peers},
            "peer_min": min(multiples),
            "peer_max": max(multiples),
        }
    )

    implied_upside_pct = None
    direction = Direction.UNKNOWN
    if subject.multiple is not None and subject.multiple > 0 and median > 0:
        implied_upside_pct = 100.0 * (median / subject.multiple - 1.0)
        direction = (
            Direction.UNDERVALUED
            if implied_upside_pct > 0
            else Direction.OVERVALUED
            if implied_upside_pct < 0
            else Direction.FAIR
        )

    return AnchorReading(
        anchor="peer",
        multiple_name=multiple_name,
        current_multiple=subject.multiple,
        reference=median,
        implied_upside_pct=implied_upside_pct,
        direction=direction,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# C3 -- PEGY
# ---------------------------------------------------------------------------


def pegy(
    trailing_pe: Optional[float],
    forward_eps_growth_pct: Optional[float],
    dividend_yield_pct: Optional[float],
    params: Params,
) -> tuple[Optional[float], str]:
    """Secondary check. Flags, never rejects; never imputes a substitute."""
    if trailing_pe is None or trailing_pe <= 0:
        return None, "PEGY n/a -- trailing P/E not positive"
    if forward_eps_growth_pct is None:
        return None, "PEGY n/a -- no forward EPS growth estimate"
    denominator = forward_eps_growth_pct + (dividend_yield_pct or 0.0)
    if denominator <= 0:
        return None, "PEGY n/a -- growth plus yield not positive"
    value = trailing_pe / denominator
    if value > params.triangulation.c3_pegy_flag_above:
        return value, f"PEGY {value:.2f} above {params.triangulation.c3_pegy_flag_above:.1f} -- flagged, not rejected"
    return value, f"PEGY {value:.2f}"


# ---------------------------------------------------------------------------
# C4 -- divergence
# ---------------------------------------------------------------------------


def triangulate(
    symbol: str,
    classification: str,
    own: AnchorReading,
    peer: AnchorReading,
    selection: PeerSelection,
    params: Params,
    trailing_pe: Optional[float] = None,
    forward_eps_growth_pct: Optional[float] = None,
    dividend_yield_pct: Optional[float] = None,
) -> TriangulationResult:
    """Combine the two anchors -- or refuse to.

    Divergence is measured in percentage points of implied over/undervaluation,
    which is the unit both anchors already report in. The threshold is one of
    the parameters the Module 12 sweep is required to test.
    """
    tp = params.triangulation
    warnings: list[str] = []
    divergence_pp = None
    disagree = False

    if own.available and peer.available:
        divergence_pp = abs(own.implied_upside_pct - peer.implied_upside_pct)
        if divergence_pp > tp.c4_divergence_pct:
            disagree = True
            warnings.append(
                "ANCHORS DISAGREE -- do not trust either without investigation. "
                f"Own-history implies {own.implied_upside_pct:+.0f}%, peers imply "
                f"{peer.implied_upside_pct:+.0f}% ({divergence_pp:.0f}pp apart, threshold "
                f"{tp.c4_divergence_pct:.0f}pp). No combined verdict is produced."
            )
        elif own.direction is not peer.direction:
            warnings.append(
                f"Anchors point different ways within the {tp.c4_divergence_pct:.0f}pp tolerance "
                f"(own-history {own.direction.value}, peers {peer.direction.value})."
            )
    else:
        missing = [a.anchor for a in (own, peer) if not a.available]
        warnings.append(
            f"SINGLE-ANCHOR RUN -- {', '.join(missing)} unavailable. Buy trigger condition 2 "
            "cannot be satisfied."
        )

    value, note = pegy(trailing_pe, forward_eps_growth_pct, dividend_yield_pct, params)

    return TriangulationResult(
        symbol=symbol,
        classification=classification,
        own_history=own,
        peer=peer,
        peer_selection=selection,
        pegy=value,
        pegy_note=note,
        divergence_pp=divergence_pp,
        anchors_disagree=disagree,
        warnings=warnings + own.warnings + peer.warnings,
    )
