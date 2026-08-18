"""Section 12 validation protocol, enforced rather than described.

The harness refuses to certify a backtest that skips the windows where the
strategy's three failure modes actually happened, that blends classifications
into one number, or that silently uses today's index membership and today's
peer lists to judge yesterday's decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Sequence

from ..config import ALL_CLASSIFICATIONS

# Periods where growth names, financials, and multiple expansion respectively
# got hit hardest. A backtest excluding these proves nothing about the
# scenarios that matter.
MANDATORY_WINDOWS: tuple[tuple[date, date, str], ...] = (
    (date(2000, 3, 1), date(2002, 10, 31), "2000-2002 -- growth names"),
    (date(2008, 1, 1), date(2009, 6, 30), "2008-2009 -- financials"),
    (date(2022, 1, 1), date(2022, 12, 31), "2022 -- multiple compression"),
)

MIN_BACKTEST_YEARS = 15

BENCHMARKS = (
    "index",
    "gcfp_v2_rules",
    "equal_weight_all_passers",
    "no_momentum_overlay",
    "single_anchor_valuation",
)


@dataclass
class BacktestCoverage:
    start: date
    end: date
    point_in_time_constituents: bool
    point_in_time_peers: bool
    classifications_reported: Sequence[str] = field(default_factory=tuple)
    benchmarks_reported: Sequence[str] = field(default_factory=tuple)
    walk_forward_holdout: Optional[tuple[date, date]] = None
    tuned_on_holdout: bool = False

    @property
    def years(self) -> float:
        return (self.end - self.start).days / 365.25


@dataclass
class ProtocolVerdict:
    certified: bool
    blocking: list[str] = field(default_factory=list)
    header_warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = []
        for warning in self.header_warnings:
            lines.append(f"!! {warning}")
        if self.certified:
            lines.append("VALIDATION PROTOCOL: satisfied.")
        else:
            lines.append("VALIDATION PROTOCOL: NOT SATISFIED. This backtest does not certify the system.")
            for item in self.blocking:
                lines.append(f"  - {item}")
        return "\n".join(lines)


def check(coverage: BacktestCoverage) -> ProtocolVerdict:
    """Validate a backtest's shape before anyone reads its numbers."""
    blocking: list[str] = []
    warnings: list[str] = []

    if coverage.years < MIN_BACKTEST_YEARS:
        blocking.append(
            f"backtest spans {coverage.years:.1f} years, {MIN_BACKTEST_YEARS} required"
        )

    for start, end, label in MANDATORY_WINDOWS:
        if coverage.start > start or coverage.end < end:
            blocking.append(f"mandatory window not covered: {label}")

    missing_classes = [
        c for c in ALL_CLASSIFICATIONS if c not in set(coverage.classifications_reported)
    ]
    if missing_classes:
        blocking.append(
            "results not reported separately per classification; missing "
            + ", ".join(missing_classes)
            + " -- a blended backtest hides whether the Growth path is carrying or dragging"
        )

    missing_benchmarks = [b for b in BENCHMARKS if b not in set(coverage.benchmarks_reported)]
    if missing_benchmarks:
        blocking.append("benchmarks missing: " + ", ".join(missing_benchmarks))

    if coverage.walk_forward_holdout is None:
        blocking.append("no walk-forward holdout defined")
    if coverage.tuned_on_holdout:
        blocking.append("parameters were tuned on the holdout -- the holdout is spent")

    if not coverage.point_in_time_constituents or not coverage.point_in_time_peers:
        missing = []
        if not coverage.point_in_time_constituents:
            missing.append("index constituents")
        if not coverage.point_in_time_peers:
            missing.append("peer sets")
        warnings.append(
            "RESULTS ARE INFLATED BY AN UNKNOWN BUT MATERIAL AMOUNT: no point-in-time "
            + " or ".join(missing)
            + ". Using today's list to backtest yesterday's decisions bakes survivorship "
            "bias into the methodology itself."
        )

    return ProtocolVerdict(certified=not blocking, blocking=blocking, header_warnings=warnings)
