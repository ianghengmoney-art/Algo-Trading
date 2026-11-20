"""Module G — execution discipline.

This module exists because the observed failure mode is not bad screening — it
is the gap between intended size and executed size.

Nothing here can close that gap; §17 is explicit that Module G measures it and
cannot fix it.  What it can do is make the gap impossible to lose track of: the
recommendation record is immutable, the deviation flag is permanent, and the
count of deviating positions is reported as a standalone discipline metric
rather than buried in a per-position table.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Sequence

from ..classification import Classification
from ..config import Config
from ..modules.c_anchors import AnchorMode
from ..modules.d_conviction import ConvictionScore


@dataclass(frozen=True)
class RecommendationRecord:
    """The immutable record written at recommendation time.

    Frozen deliberately.  A recommendation that can be edited after the fact is
    not a record, and the intended-vs-actual comparison this module exists to
    make depends on the "intended" half being fixed before the outcome is known.
    """

    symbol: str
    recommended_on: date
    classification: Classification
    conviction_total: float
    #: Every component, so a later score change can be attributed.
    conviction_components: tuple[tuple[str, float], ...]
    fair_value: float
    fair_value_method: str
    price_at_recommendation: float
    c1_reading: str
    c2_reading: str
    anchor_mode: AnchorMode
    intended_sleeve_share: float
    intended_amount_base: float
    base_currency: str
    #: The specific, falsifiable condition that would invalidate the buy.
    thesis_invalidation: str
    config_fingerprint: str
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def as_report_lines(self) -> list[str]:
        return [
            f"RECOMMENDATION · {self.symbol} · {self.recommended_on.isoformat()}",
            f"  classification: {self.classification.value}",
            f"  conviction: {self.conviction_total:.1f} "
            + ", ".join(f"{n}={p:.1f}" for n, p in self.conviction_components),
            f"  fair value: {self.fair_value:,.2f} via {self.fair_value_method}",
            f"  price: {self.price_at_recommendation:,.2f}",
            f"  anchors: {self.c1_reading} | {self.c2_reading}",
            f"  anchor mode: {self.anchor_mode.value}",
            f"  intended size: {self.intended_sleeve_share:.1%} of sleeve "
            f"= {self.intended_amount_base:,.0f} {self.base_currency}",
            f"  what would change my mind: {self.thesis_invalidation}",
            f"  parameters: {self.config_fingerprint}",
        ]


def build_recommendation(
    symbol: str,
    classification: Classification,
    conviction: ConvictionScore,
    fair_value: float,
    fair_value_method: str,
    price: float,
    c1_reading: str,
    c2_reading: str,
    anchor_mode: AnchorMode,
    intended_sleeve_share: float,
    intended_amount_base: float,
    base_currency: str,
    thesis_invalidation: str,
    config: Config,
    recommended_on: date | None = None,
) -> RecommendationRecord:
    if not thesis_invalidation.strip():
        raise ValueError(
            "a recommendation requires a falsifiable 'what would change my mind' "
            "condition; Module G will not write a record without one"
        )
    return RecommendationRecord(
        symbol=symbol,
        recommended_on=recommended_on or date.today(),
        classification=classification,
        conviction_total=conviction.total,
        conviction_components=tuple(
            (c.name, c.points) for c in conviction.components
        ),
        fair_value=fair_value,
        fair_value_method=fair_value_method,
        price_at_recommendation=price,
        c1_reading=c1_reading,
        c2_reading=c2_reading,
        anchor_mode=anchor_mode,
        intended_sleeve_share=intended_sleeve_share,
        intended_amount_base=intended_amount_base,
        base_currency=base_currency,
        thesis_invalidation=thesis_invalidation,
        config_fingerprint=config.fingerprint,
    )


@dataclass
class ExecutionRecord:
    """What the operator actually did."""

    symbol: str
    executed_on: date
    intended_amount_base: float
    actual_amount_base: float
    reason: str | None = None
    #: Once set, never cleared — the flag is permanent by design.
    size_deviation: bool = False

    @property
    def deviation(self) -> float:
        if self.intended_amount_base <= 0:
            return 0.0
        return (
            self.actual_amount_base - self.intended_amount_base
        ) / self.intended_amount_base

    def as_report_line(self) -> str:
        line = (
            f"{self.symbol}: intended {self.intended_amount_base:,.0f}, "
            f"actual {self.actual_amount_base:,.0f} ({self.deviation:+.1%})"
        )
        if self.size_deviation:
            line += " · SIZE DEVIATION"
            if self.reason:
                line += f" — {self.reason}"
        return line


def log_execution(
    symbol: str,
    intended_amount_base: float,
    actual_amount_base: float,
    config: Config,
    reason: str | None = None,
    executed_on: date | None = None,
) -> ExecutionRecord:
    """Record actual size.  Deviation beyond 25% requires a logged reason and
    marks the position SIZE DEVIATION permanently."""
    record = ExecutionRecord(
        symbol=symbol,
        executed_on=executed_on or date.today(),
        intended_amount_base=intended_amount_base,
        actual_amount_base=actual_amount_base,
        reason=reason,
    )
    if abs(record.deviation) > config.execution.size_deviation_threshold:
        if not reason:
            raise ValueError(
                f"{symbol}: deviation {record.deviation:+.1%} exceeds "
                f"{config.execution.size_deviation_threshold:.0%} and requires a "
                "logged reason"
            )
        record = replace(record, size_deviation=True)
    return record


@dataclass
class QuarterlyDisciplineReport:
    """The standalone discipline metrics G requires each quarter."""

    as_of: date
    intended_vs_actual: list[ExecutionRecord]
    trim_to_cap: list[str]
    thesis_broken: list[str]
    size_deviation_count: int
    cumulative_drift: float

    def as_report_lines(self) -> list[str]:
        lines = [f"EXECUTION DISCIPLINE · {self.as_of.isoformat()}"]
        lines.append(
            f"  SIZE DEVIATION positions: {self.size_deviation_count} "
            "(standalone discipline metric)"
        )
        lines.append(f"  cumulative sizing drift: {self.cumulative_drift:+.1%}")
        for record in self.intended_vs_actual:
            lines.append(f"    {record.as_report_line()}")
        for symbol in self.trim_to_cap:
            lines.append(
                f"  TRIM-TO-CAP · {symbol} — grew past its hard cap on "
                "appreciation. Not a winner to celebrate: an unmanaged risk the "
                "portfolio did not choose to take at that size."
            )
        for symbol in self.thesis_broken:
            lines.append(
                f"  THESIS BROKEN · {symbol} — the 'what would change my mind' "
                "condition has been met, independent of price."
            )
        return lines


def quarterly_review(
    executions: Sequence[ExecutionRecord],
    position_shares: dict[str, float],
    name_caps: dict[str, float],
    thesis_broken: Sequence[str],
    as_of: date | None = None,
) -> QuarterlyDisciplineReport:
    """Intended vs actual, TRIM-TO-CAP, THESIS BROKEN, and the deviation count."""
    as_of = as_of or date.today()
    trim = [
        symbol
        for symbol, share in position_shares.items()
        if symbol in name_caps and share > name_caps[symbol]
    ]
    deviations = [e for e in executions if e.size_deviation]
    drift = (
        sum(e.deviation for e in executions) / len(executions) if executions else 0.0
    )
    return QuarterlyDisciplineReport(
        as_of=as_of,
        intended_vs_actual=list(executions),
        trim_to_cap=trim,
        thesis_broken=list(thesis_broken),
        size_deviation_count=len(deviations),
        cumulative_drift=drift,
    )


__all__ = [
    "RecommendationRecord",
    "ExecutionRecord",
    "QuarterlyDisciplineReport",
    "build_recommendation",
    "log_execution",
    "quarterly_review",
]
