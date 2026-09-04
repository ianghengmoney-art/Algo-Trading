"""Module C — dual triangulation.

Prime Directive 4: two independent anchors, never averaged.  Disagreement is
reported, never blended.

The two anchors answer different questions.  C1 asks "is this cheap against
what the market has historically paid for *this* company?"  C2 asks "is this
cheap against what the market currently pays for companies *like* it?"  When
they diverge, that difference is information — the company re-rated (peer
anchor moves, own-history does not) versus the sector re-rated (both move
together) — and C4 refuses a combined verdict rather than averaging the
signal away.

C5 is the piece v3 lacked: an anchor that *cannot be computed* is a different
condition from one that merely disagrees, and without it a genuine
near-monopoly or a recent listing failed BUY permanently for reasons unrelated
to its quality.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Sequence

from ..classification import Classification
from ..config import Config
from ..ledger import AuditLedger
from ..types import (
    CompanyData,
    CorporateAction,
    CorporateActionType,
    MultipleObservation,
)


class AnchorMode(str, Enum):
    DUAL = "DUAL"
    SINGLE_C1 = "SINGLE (C1 only)"
    SINGLE_C2 = "SINGLE (C2 only)"
    NONE = "NONE"

    @property
    def is_single(self) -> bool:
        return self in (AnchorMode.SINGLE_C1, AnchorMode.SINGLE_C2)


#: A series fetched for an N-year window can never span quite N years — the
#: newest and oldest observations sit inside it.  Without this tolerance every
#: full-history name would carry a spurious "shorter listing history" flag.
_WINDOW_TOLERANCE_YEARS = 0.25

#: Which multiple C1 uses per classification.  Assembling a series from two
#: different measures would make the statistics meaningless, so this mapping is
#: the only place the choice is made.
ANCHOR_MULTIPLE: dict[Classification, str] = {
    Classification.CORE_STABLE: "trailing_pe",
    Classification.FINANCIAL_BANK: "p_b",
    Classification.CORE_GROWTH: "forward_pe",
    Classification.SPEC_GROWTH: "ev_revenue",
    Classification.REIT: "p_affo",
    Classification.INSURER: "p_b",
}


@dataclass
class AnchorReading:
    """One anchor's verdict."""

    name: str
    computable: bool
    #: The company's current multiple on this measure.
    current_multiple: float | None = None
    #: The reference the current multiple is judged against.
    reference_multiple: float | None = None
    #: Positive means the anchor says undervalued.
    implied_discount: float | None = None
    reason: str | None = None
    flags: list[str] = field(default_factory=list)
    detail: dict[str, object] = field(default_factory=dict)

    @property
    def confirms_undervaluation(self) -> bool:
        return self.computable and (self.implied_discount or 0.0) > 0.0

    @property
    def confirms_overvaluation(self) -> bool:
        return self.computable and (self.implied_discount or 0.0) < 0.0

    def as_log_line(self) -> str:
        if not self.computable:
            return f"{self.name}: NOT COMPUTABLE — {self.reason}"
        bits = [
            f"{self.name}: current={self.current_multiple:,.3g}",
            f"reference={self.reference_multiple:,.3g}",
            f"implied_discount={self.implied_discount:+.1%}",
        ]
        if self.flags:
            bits.append("flags=" + "; ".join(self.flags))
        return " · ".join(bits)


@dataclass
class TriangulationResult:
    """Module C's full output."""

    symbol: str
    c1: AnchorReading
    c2: AnchorReading
    c3_pegy: float | None
    c3_flag: bool
    mode: AnchorMode
    anchors_disagree: bool
    divergence: float | None
    #: The more conservative of the two implied discounts — D3 scores off this.
    conservative_discount: float | None
    reason: str | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def both_confirm_undervaluation(self) -> bool:
        """E's gate 2, honouring C5: in single-anchor mode the one available
        anchor stands in for both, under C5's penalties."""
        if self.mode is AnchorMode.DUAL:
            return self.c1.confirms_undervaluation and self.c2.confirms_undervaluation
        if self.mode is AnchorMode.SINGLE_C1:
            return self.c1.confirms_undervaluation
        if self.mode is AnchorMode.SINGLE_C2:
            return self.c2.confirms_undervaluation
        return False

    @property
    def both_confirm_overvaluation(self) -> bool:
        if self.mode is AnchorMode.DUAL:
            return self.c1.confirms_overvaluation and self.c2.confirms_overvaluation
        if self.mode is AnchorMode.SINGLE_C1:
            return self.c1.confirms_overvaluation
        if self.mode is AnchorMode.SINGLE_C2:
            return self.c2.confirms_overvaluation
        return False

    def as_report_lines(self) -> list[str]:
        lines = [self.c1.as_log_line(), self.c2.as_log_line()]
        lines.append(f"ANCHOR MODE: {self.mode.value}" + (f" — reason: {self.reason}" if self.reason else ""))
        if self.c3_pegy is not None:
            lines.append(
                f"PEGY={self.c3_pegy:.2f}" + (" — FLAG (> 2.0)" if self.c3_flag else "")
            )
        else:
            lines.append("PEGY n/a")
        if self.anchors_disagree:
            lines.append(
                f"ANCHORS DISAGREE — divergence {self.divergence:.1%}; "
                "no combined verdict is offered"
            )
        lines.extend(self.flags)
        return lines


# -- C1.1 -----------------------------------------------------------------


@dataclass(frozen=True)
class SeriesAdjustment:
    """C1.1's output: an adjusted, possibly truncated, multiple series."""

    observations: tuple[MultipleObservation, ...]
    discontinuity_date: date | None
    splits_applied: int
    years_available: float
    notes: tuple[str, ...]


def adjust_series(
    observations: Sequence[MultipleObservation],
    actions: Sequence[CorporateAction],
    config: Config,
    as_of: date | None = None,
) -> SeriesAdjustment:
    """Split-adjust, then truncate at any discontinuity.

    A spinoff changes what the company *is*, making pre-spinoff multiples
    non-comparable in a way a split adjustment cannot fix.  So splits are
    adjusted for and the series survives; spinoffs, major divestitures, and
    transformative acquisitions truncate it and the reduced sample size is
    reported rather than quietly used.
    """
    as_of = as_of or date.today()
    notes: list[str] = []
    series = sorted(observations, key=lambda o: o.observation_date, reverse=True)

    # Splits.  Price-derived multiples divide by per-share figures, so a split
    # that a vendor applied to price but not to earnings-per-share leaves a
    # step in the ratio.  Applying the ratio to observations before the split
    # normalises the series onto today's share basis.
    splits = [a for a in actions if a.is_split_like and a.ratio]
    for split in splits:
        for i, obs in enumerate(series):
            if obs.observation_date < split.effective_date:
                series[i] = MultipleObservation(
                    observation_date=obs.observation_date,
                    value=obs.value,  # ratio-neutral once both legs are adjusted
                    multiple=obs.multiple,
                )
        notes.append(
            f"split adjustment applied at {split.effective_date.isoformat()} "
            f"(ratio {split.ratio:g})"
        )

    # Discontinuities.
    breaking = [
        a
        for a in actions
        if a.action_type
        in (
            CorporateActionType.SPINOFF,
            CorporateActionType.DIVESTITURE,
            CorporateActionType.ACQUISITION,
        )
        and (
            a.action_type is CorporateActionType.SPINOFF
            or (a.market_cap_share or 0.0)
            > config.anchors.transformative_event_market_cap_share
        )
    ]
    discontinuity = max((a.effective_date for a in breaking), default=None)
    if discontinuity is not None:
        before = len(series)
        series = [o for o in series if o.observation_date >= discontinuity]
        notes.append(
            f"SERIES DISCONTINUITY at {discontinuity.isoformat()} — "
            f"{before - len(series)} of {before} observations dropped; "
            f"statistics computed on the post-event segment only"
        )

    if series:
        span = (series[0].observation_date - series[-1].observation_date).days / 365.25
    else:
        span = 0.0

    return SeriesAdjustment(
        observations=tuple(series),
        discontinuity_date=discontinuity,
        splits_applied=len(splits),
        years_available=span,
        notes=tuple(notes),
    )


# -- C1.2 -----------------------------------------------------------------


def detect_re_rating(
    observations: Sequence[MultipleObservation],
) -> tuple[bool, str | None]:
    """Scan for a sustained step-change in multiple level, not noise.

    Compares the older and newer halves of the series: a shift in mean of more
    than one pooled standard deviation, where both halves are individually
    tight, is a level change rather than drift.  This is the Micron
    DRAM-to-HBM and Apple hardware-to-Services problem — the own-history anchor
    silently anchored to a business that no longer exists.
    """
    values = [o.value for o in sorted(observations, key=lambda o: o.observation_date)]
    if len(values) < 12:
        return False, None

    mid = len(values) // 2
    older, newer = values[:mid], values[mid:]
    if len(older) < 4 or len(newer) < 4:
        return False, None

    mean_old, mean_new = statistics.fmean(older), statistics.fmean(newer)
    try:
        sd_old, sd_new = statistics.stdev(older), statistics.stdev(newer)
    except statistics.StatisticsError:
        return False, None

    pooled = ((sd_old**2 + sd_new**2) / 2.0) ** 0.5
    if pooled <= 0:
        return False, None

    shift = abs(mean_new - mean_old) / pooled
    if shift > 1.0:
        direction = "up" if mean_new > mean_old else "down"
        return True, (
            f"POSSIBLE RE-RATING — multiple stepped {direction} from "
            f"{mean_old:.1f} to {mean_new:.1f} ({shift:.1f} pooled sd); the "
            "own-history anchor may be anchored to a business that no longer exists"
        )
    return False, None


# -- C1 -------------------------------------------------------------------


def compute_c1(
    data: CompanyData,
    config: Config,
    classification: Classification,
    current_multiple: float | None,
    ledger: AuditLedger | None = None,
    as_of: date | None = None,
) -> AnchorReading:
    """Own-history anchor over a minimum 7-year window."""
    cfg = config.anchors
    as_of = as_of or date.today()
    multiple_name = ANCHOR_MULTIPLE[classification]

    raw = [o for o in data.multiples if o.multiple == multiple_name]
    if not raw:
        return AnchorReading(
            "C1",
            False,
            reason=f"no {multiple_name} history available",
        )
    if current_multiple is None or current_multiple <= 0:
        return AnchorReading(
            "C1",
            False,
            reason=f"current {multiple_name} not computable or non-positive",
        )

    adjustment = adjust_series(raw, data.corporate_actions, config, as_of)
    if ledger is not None:
        for note in adjustment.notes:
            ledger.note(f"C1.1 · {note}")

    flags: list[str] = []
    if adjustment.discontinuity_date is not None:
        flags.append(
            f"SERIES DISCONTINUITY at {adjustment.discontinuity_date.isoformat()}"
        )

    if adjustment.years_available < cfg.min_history_years:
        return AnchorReading(
            "C1",
            False,
            reason=(
                f"only {adjustment.years_available:.1f} years of usable history "
                f"after C1.1 adjustment (minimum {cfg.min_history_years}) — routed to C5"
            ),
            flags=flags,
        )
    if adjustment.years_available < cfg.reduced_confidence_window_years:
        flags.append(
            f"REDUCED CONFIDENCE — {adjustment.years_available:.1f}-year window, "
            f"short of the {cfg.history_window_years}-year standard"
        )
    elif adjustment.years_available < cfg.history_window_years - _WINDOW_TOLERANCE_YEARS:
        flags.append(
            f"REDUCED CONFIDENCE — {adjustment.years_available:.1f}-year window "
            f"(shorter listing history)"
        )

    values = [o.value for o in adjustment.observations]
    mean = statistics.fmean(values)
    median = statistics.median(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    z = (current_multiple - mean) / sd if sd > 0 else 0.0
    percentile = 100.0 * sum(1 for v in values if v <= current_multiple) / len(values)

    re_rated, message = detect_re_rating(adjustment.observations)
    if re_rated and message:
        flags.append(message)
        if ledger is not None:
            ledger.note(f"C1.2 · {message}")

    # Cheap against own history means today's multiple sits below the median.
    implied = 1.0 - (current_multiple / median) if median > 0 else None

    reading = AnchorReading(
        "C1",
        True,
        current_multiple=current_multiple,
        reference_multiple=median,
        implied_discount=implied,
        flags=flags,
        detail={
            "multiple": multiple_name,
            "mean": mean,
            "median": median,
            "stdev": sd,
            "z_score": z,
            "percentile": percentile,
            "observations": len(values),
            "years": adjustment.years_available,
            "re_rating_detected": re_rated,
        },
    )
    if ledger is not None:
        ledger.note(
            f"C1 · {multiple_name} n={len(values)} mean={mean:.2f} "
            f"median={median:.2f} sd={sd:.2f} z={z:+.2f} "
            f"percentile={percentile:.0f} · {reading.as_log_line()}"
        )
    return reading


# -- C2 -------------------------------------------------------------------


@dataclass(frozen=True)
class PeerCandidate:
    """One name considered for the peer set, kept or rejected."""

    symbol: str
    multiple: float | None = None
    market_cap: float | None = None
    revenue_growth: float | None = None
    group: str | None = None


@dataclass(frozen=True)
class PeerDecision:
    candidate: PeerCandidate
    included: bool
    reason: str


def select_peers(
    subject_group: str | None,
    subject_market_cap: float | None,
    subject_growth: float | None,
    candidates: Sequence[PeerCandidate],
    config: Config,
) -> tuple[list[PeerCandidate], list[PeerDecision]]:
    """Apply C2's screen and record a decision for every candidate.

    Peer selection is the single most gameable step in any valuation system;
    the log is what makes it auditable.  Every considered-but-rejected peer is
    returned with its exclusion reason — including the ones rejected for
    mundane reasons, because a reader cannot tell a disciplined screen from a
    convenient one without seeing the whole set.
    """
    cfg = config.anchors
    decisions: list[PeerDecision] = []
    kept: list[PeerCandidate] = []

    for cand in candidates:
        if cand.multiple is None or cand.multiple <= 0:
            decisions.append(
                PeerDecision(cand, False, "multiple not computable or non-positive")
            )
            continue
        if subject_group and cand.group and cand.group != subject_group:
            decisions.append(
                PeerDecision(
                    cand, False, f"different grouping ({cand.group} vs {subject_group})"
                )
            )
            continue
        if subject_market_cap and cand.market_cap:
            ratio = cand.market_cap / subject_market_cap
            if not (cfg.peer_market_cap_low <= ratio <= cfg.peer_market_cap_high):
                decisions.append(
                    PeerDecision(
                        cand,
                        False,
                        f"market cap {ratio:.2f}x subject, outside "
                        f"{cfg.peer_market_cap_low}-{cfg.peer_market_cap_high}x",
                    )
                )
                continue
        elif cand.market_cap is None:
            decisions.append(PeerDecision(cand, False, "market cap unavailable"))
            continue
        if subject_growth is not None and cand.revenue_growth is not None:
            gap = abs(cand.revenue_growth - subject_growth)
            if gap > cfg.peer_growth_band:
                decisions.append(
                    PeerDecision(
                        cand,
                        False,
                        f"revenue growth {gap*100:.1f}pp from subject, outside "
                        f"{cfg.peer_growth_band*100:.0f}pp band",
                    )
                )
                continue
        elif cand.revenue_growth is None:
            decisions.append(PeerDecision(cand, False, "revenue growth unavailable"))
            continue

        decisions.append(PeerDecision(cand, True, "meets grouping, size, and growth screen"))
        kept.append(cand)

    # Cap at peer_max, keeping the closest by market cap so the trim is a rule
    # rather than a choice.
    if len(kept) > cfg.peer_max and subject_market_cap:
        kept.sort(key=lambda c: abs((c.market_cap or 0) / subject_market_cap - 1.0))
        trimmed = kept[cfg.peer_max :]
        kept = kept[: cfg.peer_max]
        for cand in trimmed:
            decisions = [
                PeerDecision(d.candidate, False, f"trimmed to peer_max={cfg.peer_max} (furthest by size)")
                if d.candidate.symbol == cand.symbol
                else d
                for d in decisions
            ]

    return kept, decisions


def compute_c2(
    data: CompanyData,
    config: Config,
    current_multiple: float | None,
    candidates: Sequence[PeerCandidate],
    subject_group: str | None,
    subject_growth: float | None,
    ledger: AuditLedger | None = None,
) -> AnchorReading:
    """Industry-peer anchor: 4-8 genuine peers, peer *median*, never mean."""
    cfg = config.anchors
    if current_multiple is None or current_multiple <= 0:
        return AnchorReading(
            "C2", False, reason="current multiple not computable or non-positive"
        )

    kept, decisions = select_peers(
        subject_group, data.profile.market_cap, subject_growth, candidates, config
    )

    if ledger is not None:
        for d in decisions:
            verdict = "INCLUDED" if d.included else "EXCLUDED"
            ledger.note(
                f"C2 peer {d.candidate.symbol}: {verdict} — {d.reason}"
                + (
                    f" (multiple={d.candidate.multiple:.2f})"
                    if d.candidate.multiple
                    else ""
                )
            )

    if len(kept) < cfg.peer_min:
        return AnchorReading(
            "C2",
            False,
            reason=(
                f"only {len(kept)} genuine peers after exclusions (minimum "
                f"{cfg.peer_min}) — routed to C5"
            ),
            detail={"considered": len(decisions), "kept": len(kept)},
        )

    median = statistics.median([c.multiple for c in kept])  # type: ignore[misc]
    implied = 1.0 - (current_multiple / median) if median > 0 else None

    reading = AnchorReading(
        "C2",
        True,
        current_multiple=current_multiple,
        reference_multiple=median,
        implied_discount=implied,
        detail={
            "peers": [c.symbol for c in kept],
            "peer_count": len(kept),
            "considered": len(decisions),
            "excluded": len(decisions) - len(kept),
        },
    )
    if ledger is not None:
        ledger.note(
            f"C2 · peers={[c.symbol for c in kept]} median={median:.2f} · "
            f"{reading.as_log_line()}"
        )
    return reading


# -- C3 -------------------------------------------------------------------


def compute_c3_pegy(
    data: CompanyData,
    config: Config,
    trailing_pe: float | None,
) -> tuple[float | None, bool]:
    """PEGY = P/E / (forward EPS growth % + dividend yield %).

    Where trailing P/E is positive.  Where undefined, ``None`` — logged as
    "PEGY n/a" and never imputed.  A flag, never a rejection.
    """
    if trailing_pe is None or trailing_pe <= 0:
        return None, False
    growth = data.forward_eps_growth
    if growth is None:
        return None, False
    yield_pct = (data.dividend_yield or 0.0) * 100.0
    denominator = growth * 100.0 + yield_pct
    if denominator <= 0:
        return None, False
    pegy = trailing_pe / denominator
    return pegy, pegy > config.anchors.pegy_flag_above


# -- C4 + C5 --------------------------------------------------------------


def triangulate(
    data: CompanyData,
    config: Config,
    c1: AnchorReading,
    c2: AnchorReading,
    trailing_pe: float | None = None,
    ledger: AuditLedger | None = None,
) -> TriangulationResult:
    """Combine C1 and C2 under C4's divergence rule and C5's availability rule."""
    cfg = config.anchors
    flags: list[str] = []

    # C5 first: availability decides which path the rest of the module takes.
    if c1.computable and c2.computable:
        mode = AnchorMode.DUAL
        reason = None
    elif c1.computable:
        mode = AnchorMode.SINGLE_C1
        reason = c2.reason
    elif c2.computable:
        mode = AnchorMode.SINGLE_C2
        reason = c1.reason
    else:
        mode = AnchorMode.NONE
        reason = f"C1: {c1.reason}; C2: {c2.reason}"
        flags.append(
            "BOTH ANCHORS UNCOMPUTABLE — automatic FAIL; no valuation is "
            "possible and Module B alone is not sufficient grounds to proceed"
        )

    # C4 divergence, only meaningful when both are computable.
    divergence: float | None = None
    disagree = False
    if mode is AnchorMode.DUAL:
        d1 = c1.implied_discount or 0.0
        d2 = c2.implied_discount or 0.0
        divergence = abs(d1 - d2)
        if divergence > cfg.divergence_flag:
            disagree = True
            flags.append(
                f"ANCHORS DISAGREE — C1 implies {d1:+.1%}, C2 implies {d2:+.1%} "
                f"({divergence:.1%} apart); no combined verdict is offered. "
                "Which anchor moved distinguishes a re-rated company from a "
                "re-rated sector."
            )

    # The conservative anchor — the one implying the *smaller* discount — is
    # what D3 scores off, since the point is to measure the margin that
    # survives the less generous of the two readings.
    discounts = [
        r.implied_discount
        for r in (c1, c2)
        if r.computable and r.implied_discount is not None
    ]
    conservative = min(discounts) if discounts else None

    if mode.is_single:
        flags.append(
            f"ANCHOR MODE: {mode.value} — reason: {reason}. Buy threshold "
            f"+{cfg.single_anchor_threshold_penalty:.0%}, conviction capped at "
            f"{cfg.single_anchor_conviction_cap:.0f}, counts against the "
            f"{cfg.single_anchor_portfolio_cap:.0%} portfolio cap."
        )

    pegy, pegy_flag = compute_c3_pegy(data, config, trailing_pe)
    if pegy_flag:
        flags.append(f"PEGY {pegy:.2f} above {cfg.pegy_flag_above} — flagged, not rejected")

    result = TriangulationResult(
        symbol=data.profile.symbol,
        c1=c1,
        c2=c2,
        c3_pegy=pegy,
        c3_flag=pegy_flag,
        mode=mode,
        anchors_disagree=disagree,
        divergence=divergence,
        conservative_discount=conservative,
        reason=reason,
        flags=flags,
    )
    if ledger is not None:
        for line in result.as_report_lines():
            ledger.note(f"C · {line}")
    return result


__all__ = [
    "AnchorMode",
    "AnchorReading",
    "TriangulationResult",
    "PeerCandidate",
    "PeerDecision",
    "SeriesAdjustment",
    "ANCHOR_MULTIPLE",
    "adjust_series",
    "detect_re_rating",
    "compute_c1",
    "compute_c2",
    "compute_c3_pegy",
    "select_peers",
    "triangulate",
]
