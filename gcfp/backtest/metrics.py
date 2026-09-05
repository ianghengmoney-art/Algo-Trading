"""§13.6 — the full return distribution, not the mean.

The spec is specific about this, and the reason is that a value strategy's
mean return is the least informative number it produces.  What matters is the
*shape*: the median position outcome, how often positions lose, how deep the
worst single position went, how deep the portfolio went, and how long recovery
took.  A strategy with a good mean and a 60% loss rate is a different
proposition from one with the same mean and a 30% loss rate, and only the
distribution distinguishes them.

Module I's pre-registered expectations are the yardstick these are read
against: roughly a third of growth holdings expected to be total losses, sleeve
drawdowns of 40-60% in adverse markets, and 2-5%/year of excess return arriving
unevenly.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from ..classification import Classification, Regime, regime_of
from .portfolio import ClosedPosition


@dataclass(frozen=True)
class DrawdownEpisode:
    """One peak-to-trough-to-recovery cycle."""

    peak_date: date
    peak_value: float
    trough_date: date
    trough_value: float
    recovered_on: date | None

    @property
    def depth(self) -> float:
        if self.peak_value <= 0:
            return 0.0
        return self.trough_value / self.peak_value - 1.0

    @property
    def days_to_trough(self) -> int:
        return (self.trough_date - self.peak_date).days

    @property
    def days_to_recovery(self) -> int | None:
        if self.recovered_on is None:
            return None
        return (self.recovered_on - self.peak_date).days

    def as_report_line(self) -> str:
        recovery = (
            f"recovered in {self.days_to_recovery} days"
            if self.recovered_on
            else "NOT YET RECOVERED"
        )
        return (
            f"{self.depth:.1%} from {self.peak_date.isoformat()} "
            f"to {self.trough_date.isoformat()} ({self.days_to_trough} days), {recovery}"
        )


@dataclass
class DistributionStats:
    """The distribution of closed-position outcomes."""

    count: int
    median_return: float | None
    mean_return: float | None
    best: float | None
    worst: float | None
    loss_rate: float | None
    total_loss_rate: float | None
    p25: float | None
    p75: float | None
    median_holding_days: float | None
    worst_position_drawdown: float | None

    def as_report_lines(self) -> list[str]:
        if not self.count:
            return ["  no closed positions"]
        return [
            f"  positions closed: {self.count}",
            f"  median outcome: {self.median_return:+.1%}"
            f"   (mean {self.mean_return:+.1%} — reported second, deliberately)",
            f"  quartiles: p25 {self.p25:+.1%} · p75 {self.p75:+.1%}",
            f"  best {self.best:+.1%} · worst {self.worst:+.1%}",
            f"  loss rate: {self.loss_rate:.1%}"
            + (
                f" · total losses (<= -90%): {self.total_loss_rate:.1%}"
                if self.total_loss_rate is not None
                else ""
            ),
            f"  largest single-position drawdown: {self.worst_position_drawdown:.1%}"
            if self.worst_position_drawdown is not None
            else "  largest single-position drawdown: n/a",
            f"  median holding period: {self.median_holding_days:.0f} days",
        ]


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def distribution(closed: Sequence[ClosedPosition]) -> DistributionStats:
    if not closed:
        return DistributionStats(0, None, None, None, None, None, None, None, None, None, None)

    returns = [c.total_return for c in closed]
    drawdowns = [c.max_drawdown for c in closed if c.max_drawdown]
    return DistributionStats(
        count=len(closed),
        median_return=statistics.median(returns),
        mean_return=statistics.fmean(returns),
        best=max(returns),
        worst=min(returns),
        loss_rate=sum(1 for c in closed if c.is_loss) / len(closed),
        total_loss_rate=sum(1 for c in closed if c.is_total_loss) / len(closed),
        p25=_percentile(returns, 0.25),
        p75=_percentile(returns, 0.75),
        median_holding_days=statistics.median([c.holding_days for c in closed]),
        worst_position_drawdown=min(drawdowns) if drawdowns else None,
    )


def distribution_by_classification(
    closed: Sequence[ClosedPosition],
) -> dict[Classification, DistributionStats]:
    """§13.1: reported separately per classification.

    A blended backtest hides whether Growth is carrying or dragging the system,
    which is the specific thing this split exists to reveal.
    """
    grouped: dict[Classification, list[ClosedPosition]] = {}
    for position in closed:
        grouped.setdefault(position.classification, []).append(position)
    return {tag: distribution(rows) for tag, rows in sorted(grouped.items(), key=lambda kv: kv[0].value)}


def distribution_by_regime(
    closed: Sequence[ClosedPosition],
) -> dict[Regime, DistributionStats]:
    grouped: dict[Regime, list[ClosedPosition]] = {}
    for position in closed:
        grouped.setdefault(regime_of(position.classification), []).append(position)
    return {regime: distribution(rows) for regime, rows in grouped.items()}


def max_drawdown(curve: Sequence[tuple[date, float]]) -> DrawdownEpisode | None:
    """The deepest peak-to-trough, and when (or whether) it recovered."""
    if len(curve) < 2:
        return None

    peak_date, peak_value = curve[0]
    worst: DrawdownEpisode | None = None

    for day, value in curve:
        if value >= peak_value:
            # A new high closes the worst episode if it was still open.
            if worst is not None and worst.recovered_on is None and value >= worst.peak_value:
                worst = DrawdownEpisode(
                    worst.peak_date, worst.peak_value,
                    worst.trough_date, worst.trough_value, day,
                )
            peak_date, peak_value = day, value
            continue

        depth = value / peak_value - 1.0 if peak_value > 0 else 0.0
        if worst is None or depth < worst.depth:
            worst = DrawdownEpisode(peak_date, peak_value, day, value, None)

    return worst


def rolling_returns(
    curve: Sequence[tuple[date, float]], years: int
) -> list[tuple[date, float]]:
    """Annualised return over trailing windows, for §10's pre-registered bands."""
    if len(curve) < 2:
        return []
    out: list[tuple[date, float]] = []
    for i, (day, value) in enumerate(curve):
        target = date(day.year - years, day.month, min(day.day, 28))
        earlier = [(d, v) for d, v in curve[:i] if d <= target]
        if not earlier:
            continue
        _, start_value = earlier[-1]
        if start_value <= 0:
            continue
        total = value / start_value
        out.append((day, total ** (1.0 / years) - 1.0))
    return out


def annualised_return(curve: Sequence[tuple[date, float]]) -> float | None:
    if len(curve) < 2:
        return None
    (start_day, start_value), (end_day, end_value) = curve[0], curve[-1]
    if start_value <= 0:
        return None
    years = (end_day - start_day).days / 365.25
    if years <= 0:
        return None
    return (end_value / start_value) ** (1.0 / years) - 1.0


def volatility(curve: Sequence[tuple[date, float]]) -> float | None:
    """Annualised standard deviation of period returns."""
    if len(curve) < 3:
        return None
    returns = []
    for (_, before), (_, after) in zip(curve, curve[1:]):
        if before > 0:
            returns.append(after / before - 1.0)
    if len(returns) < 2:
        return None
    periods_per_year = 12.0  # monthly rebalances
    return statistics.stdev(returns) * math.sqrt(periods_per_year)


@dataclass
class PerformanceSummary:
    """One run's headline numbers, benchmark-relative where possible."""

    label: str
    start_value: float
    end_value: float
    annualised: float | None
    volatility: float | None
    max_drawdown: DrawdownEpisode | None
    benchmark_annualised: float | None
    distribution: DistributionStats
    by_classification: dict[Classification, DistributionStats] = field(default_factory=dict)

    @property
    def excess_annualised(self) -> float | None:
        if self.annualised is None or self.benchmark_annualised is None:
            return None
        return self.annualised - self.benchmark_annualised

    def as_report_lines(self) -> list[str]:
        lines = [f"{self.label}"]
        lines.append(
            f"  {self.start_value:,.0f} -> {self.end_value:,.0f}"
            + (f" · {self.annualised:+.2%}/yr" if self.annualised is not None else "")
        )
        if self.benchmark_annualised is not None:
            lines.append(
                f"  benchmark {self.benchmark_annualised:+.2%}/yr · "
                f"excess {self.excess_annualised:+.2%}/yr"
            )
            if self.excess_annualised is not None:
                expected = "within" if 0.02 <= self.excess_annualised <= 0.05 else "outside"
                lines.append(
                    f"  {expected} Module I's pre-registered 2-5%/yr excess band"
                )
        if self.volatility is not None:
            lines.append(f"  volatility {self.volatility:.1%}/yr")
        if self.max_drawdown is not None:
            lines.append(f"  portfolio max drawdown: {self.max_drawdown.as_report_line()}")
        lines.extend(self.distribution.as_report_lines())
        return lines


def summarise(
    label: str,
    curve: Sequence[tuple[date, float]],
    closed: Sequence[ClosedPosition],
    benchmark_curve: Sequence[tuple[date, float]] = (),
) -> PerformanceSummary:
    return PerformanceSummary(
        label=label,
        start_value=curve[0][1] if curve else 0.0,
        end_value=curve[-1][1] if curve else 0.0,
        annualised=annualised_return(curve),
        volatility=volatility(curve),
        max_drawdown=max_drawdown(curve),
        benchmark_annualised=annualised_return(benchmark_curve),
        distribution=distribution(closed),
        by_classification=distribution_by_classification(closed),
    )


def window_slice(
    curve: Sequence[tuple[date, float]], start: date, end: date
) -> list[tuple[date, float]]:
    """§13.2's mandatory windows, cut out of a full-period curve."""
    return [(d, v) for d, v in curve if start <= d <= end]


__all__ = [
    "DistributionStats",
    "DrawdownEpisode",
    "PerformanceSummary",
    "annualised_return",
    "distribution",
    "distribution_by_classification",
    "distribution_by_regime",
    "max_drawdown",
    "rolling_returns",
    "summarise",
    "volatility",
    "window_slice",
]
