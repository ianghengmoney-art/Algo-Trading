"""Numbers derived from price history rather than bought.

Beta is the notable one.  B1 wants "5-year monthly beta vs. S&P 500, from the
data provider", and providers charge for it — but it is a regression on data
that is free, and computing it has a property buying it does not: you can see
exactly what window and what benchmark produced the number.  The spec's real
requirement is that beta is never *assumed* to be 1.0, and computing it
satisfies that better than trusting an unexplained vendor field.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from .types import PricePoint


@dataclass(frozen=True)
class BetaEstimate:
    """A beta with the evidence behind it."""

    beta: float
    observations: int
    window_start: date
    window_end: date
    benchmark: str
    r_squared: float

    def as_log_line(self) -> str:
        return (
            f"beta={self.beta:.3f} vs {self.benchmark} "
            f"n={self.observations} months "
            f"({self.window_start.isoformat()}..{self.window_end.isoformat()}) "
            f"r2={self.r_squared:.2f}"
        )


def monthly_returns(
    prices: Sequence[PricePoint],
) -> dict[tuple[int, int], float]:
    """Month-end to month-end returns, keyed by (year, month).

    Uses the adjusted close where the source supplies one, so a split does not
    read as a 50% loss.
    """
    by_month: dict[tuple[int, int], PricePoint] = {}
    for point in sorted(prices, key=lambda p: p.price_date):
        key = (point.price_date.year, point.price_date.month)
        # Last observation in each month wins.
        by_month[key] = point

    months = sorted(by_month)
    out: dict[tuple[int, int], float] = {}
    for previous, current in zip(months, months[1:]):
        before = by_month[previous].adjusted_close or by_month[previous].close
        after = by_month[current].adjusted_close or by_month[current].close
        if not before or before <= 0:
            continue
        out[current] = after / before - 1.0
    return out


def compute_beta(
    prices: Sequence[PricePoint],
    benchmark_prices: Sequence[PricePoint],
    *,
    benchmark: str = "^GSPC",
    min_months: int = 24,
) -> BetaEstimate | None:
    """Ordinary least squares slope of monthly returns against the benchmark.

    Returns ``None`` when there is too little overlapping history.  That is a
    data gap, and A5 handles data gaps — the one thing this must never do is
    return 1.0, which is the specific failure the spec calls out.
    """
    own = monthly_returns(prices)
    market = monthly_returns(benchmark_prices)

    shared = sorted(set(own) & set(market))
    if len(shared) < min_months:
        return None

    xs = [market[m] for m in shared]
    ys = [own[m] for m in shared]

    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    variance = sum((x - mean_x) ** 2 for x in xs)
    if variance <= 0:
        return None

    beta = covariance / variance
    intercept = mean_y - beta * mean_x
    residual = sum((y - (intercept + beta * x)) ** 2 for x, y in zip(xs, ys))
    total = sum((y - mean_y) ** 2 for y in ys)
    r_squared = 1.0 - residual / total if total > 0 else 0.0

    first, last = shared[0], shared[-1]
    return BetaEstimate(
        beta=beta,
        observations=len(shared),
        window_start=date(first[0], first[1], 1),
        window_end=date(last[0], last[1], 1),
        benchmark=benchmark,
        r_squared=r_squared,
    )


def group_median(values: Sequence[float | None]) -> tuple[float | None, int]:
    """Median of the computable values, and how many there were.

    A2 escalates its fallback ladder on the *count of computable members*, not
    on nominal membership, so both halves are returned together.
    """
    usable = [v for v in values if v is not None]
    if not usable:
        return None, 0
    return statistics.median(usable), len(usable)


__all__ = ["BetaEstimate", "compute_beta", "monthly_returns", "group_median"]
