"""Return distribution metrics.

Section 12 asks for the full distribution, not just the mean: the mean is the
one number that a strategy expecting total losses on a third of its growth
holdings is guaranteed to describe badly.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass
class PositionOutcome:
    symbol: str
    classification: str
    return_pct: float
    holding_days: int


@dataclass
class DistributionSummary:
    count: int
    mean_pct: Optional[float]
    median_pct: Optional[float]
    loss_rate_pct: Optional[float]
    total_loss_rate_pct: Optional[float]
    worst_pct: Optional[float]
    best_pct: Optional[float]
    percentiles: dict

    def render(self) -> str:
        if not self.count:
            return "no closed positions"
        return (
            f"n={self.count} | median {self.median_pct:+.1f}% | mean {self.mean_pct:+.1f}% | "
            f"loss rate {self.loss_rate_pct:.0f}% | total-loss rate {self.total_loss_rate_pct:.0f}% | "
            f"worst {self.worst_pct:+.1f}% | best {self.best_pct:+.1f}%"
        )


def summarise(outcomes: Sequence[PositionOutcome], total_loss_threshold_pct: float = -90.0) -> DistributionSummary:
    if not outcomes:
        return DistributionSummary(0, None, None, None, None, None, None, {})
    returns = sorted(o.return_pct for o in outcomes)
    n = len(returns)

    def percentile(p: float) -> float:
        index = min(n - 1, max(0, int(round(p / 100.0 * (n - 1)))))
        return returns[index]

    return DistributionSummary(
        count=n,
        mean_pct=statistics.fmean(returns),
        median_pct=statistics.median(returns),
        loss_rate_pct=100.0 * sum(1 for r in returns if r < 0) / n,
        total_loss_rate_pct=100.0 * sum(1 for r in returns if r <= total_loss_threshold_pct) / n,
        worst_pct=returns[0],
        best_pct=returns[-1],
        percentiles={p: percentile(p) for p in (5, 25, 50, 75, 95)},
    )


def by_classification(outcomes: Sequence[PositionOutcome]) -> dict[str, DistributionSummary]:
    grouped: dict[str, list[PositionOutcome]] = {}
    for outcome in outcomes:
        grouped.setdefault(outcome.classification, []).append(outcome)
    return {k: summarise(v) for k, v in grouped.items()}


def max_drawdown_pct(equity_curve: Sequence[float]) -> Optional[float]:
    if len(equity_curve) < 2:
        return None
    peak = equity_curve[0]
    worst = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, 100.0 * (value / peak - 1.0))
    return worst


def time_to_recovery(equity_curve: Sequence[float]) -> Optional[int]:
    """Longest span, in periods, from a peak to the point that peak was regained.

    Measured peak-to-recovery rather than as a count of underwater periods,
    because the number a drawdown report is asked for is how long the wait was.
    A curve still below its high water mark at the end contributes the span so
    far, so an unrecovered drawdown is never reported as zero.
    """
    if len(equity_curve) < 2:
        return None
    peak = equity_curve[0]
    peak_index = 0
    underwater = False
    longest = 0
    for index, value in enumerate(equity_curve):
        if value >= peak:
            if underwater:
                longest = max(longest, index - peak_index)
                underwater = False
            peak = value
            peak_index = index
        else:
            underwater = True
    if underwater:
        longest = max(longest, len(equity_curve) - 1 - peak_index)
    return longest


def classification_accuracy_pct(assigned: Sequence[str], best_explaining: Sequence[str]) -> Optional[float]:
    """How often A6 routed a company to the method that best explained its behaviour.

    Reported as its own metric because a system that misroutes is broken at the
    foundation regardless of how good the downstream math is.
    """
    pairs = [(a, b) for a, b in zip(assigned, best_explaining) if a and b]
    if not pairs:
        return None
    return 100.0 * sum(1 for a, b in pairs if a == b) / len(pairs)
