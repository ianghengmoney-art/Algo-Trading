"""Module G -- execution discipline.

This module exists because the observed failure mode in practice is not bad
screening. It is the gap between intended size and executed size.

Module G measures that gap. It cannot close it -- that remains a human
decision, every time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Sequence

from ..config import CORE_SLEEVE, GROWTH_SLEEVE, Params
from ..modules.f_sizing import Portfolio, sleeve_capacity_value, sleeve_rules

SIZE_DEVIATION = "SIZE DEVIATION"
TRIM_TO_CAP = "TRIM-TO-CAP"
THESIS_BROKEN = "THESIS BROKEN"


@dataclass(frozen=True)
class RecommendationRecord:
    """Written at recommendation time and never rewritten.

    Immutability is the point: the record is what a later quarter is compared
    against, and a thesis that can be edited after the fact measures nothing.
    """

    symbol: str
    recommended_on: date
    classification: str
    conviction_score: float
    fair_value_per_share: float
    valuation_method: str
    own_history_reading_pct: Optional[float]
    peer_reading_pct: Optional[float]
    intended_sleeve_pct: float
    intended_value: float
    currency: str
    thesis_invalidation: str

    def __post_init__(self) -> None:
        if not self.thesis_invalidation or not self.thesis_invalidation.strip():
            raise ValueError(
                f"{self.symbol}: a recommendation requires a specific, falsifiable "
                "'what would change my mind' condition"
            )


@dataclass
class ExecutionRecord:
    """What the operator actually did."""

    symbol: str
    executed_on: date
    actual_value: float
    reason_for_deviation: Optional[str] = None
    size_deviation: bool = False
    deviation_pct: Optional[float] = None


def log_execution(
    record: RecommendationRecord,
    executed_on: date,
    actual_value: float,
    params: Params,
    reason: Optional[str] = None,
) -> ExecutionRecord:
    """Record the executed size and mark deviation permanently.

    A deviation beyond the threshold requires a logged reason; the flag itself
    is not removable, because the count of deviating positions is the
    discipline metric.
    """
    ep = params.execution
    deviation_pct = None
    if record.intended_value > 0:
        deviation_pct = 100.0 * (actual_value / record.intended_value - 1.0)

    deviated = deviation_pct is not None and abs(deviation_pct) > ep.size_deviation_flag_pct
    if deviated and not (reason and reason.strip()):
        raise ValueError(
            f"{record.symbol}: actual size deviates {deviation_pct:+.1f}% from intended "
            f"(limit {ep.size_deviation_flag_pct:.0f}%). A logged reason is required."
        )

    return ExecutionRecord(
        symbol=record.symbol,
        executed_on=executed_on,
        actual_value=actual_value,
        reason_for_deviation=reason if deviated else None,
        size_deviation=deviated,
        deviation_pct=deviation_pct,
    )


@dataclass
class DisciplineFlag:
    symbol: str
    flag: str
    detail: str


def trim_to_cap_flags(portfolio: Portfolio, params: Params) -> list[DisciplineFlag]:
    """Positions sitting above their sleeve's hard cap.

    Measured against sleeve *capacity* rather than current sleeve value. Using
    current value as the denominator would flag every name in an underfilled
    sleeve -- two positions in a half-empty sleeve are 50% of it each -- which
    is a statement about the sleeve being empty, not about the position being
    oversized.

    A position that grew past its cap through appreciation is not a winner to
    celebrate; it is an unmanaged risk the portfolio did not choose to take at
    that size. One that was above the cap on the day it was bought is a
    different failure, and is reported as such.
    """
    flags: list[DisciplineFlag] = []
    for sleeve in (CORE_SLEEVE, GROWTH_SLEEVE):
        positions = portfolio.sleeve_positions(sleeve)
        if not positions:
            continue
        rules = sleeve_rules(sleeve, params)
        basis = sleeve_capacity_value(portfolio, sleeve, params) or portfolio.sleeve_value(sleeve)
        if basis <= 0:
            continue
        for pos in positions:
            current_pct = 100.0 * pos.current_value / basis
            if current_pct <= rules.hard_cap_pct:
                continue
            cost_pct = 100.0 * pos.cost_value / basis
            if cost_pct <= rules.hard_cap_pct:
                detail = (
                    f"{current_pct:.1f}% of the {sleeve} sleeve against a "
                    f"{rules.hard_cap_pct:.0f}% hard cap -- grown past cap on appreciation "
                    f"(was {cost_pct:.1f}% at cost)."
                )
            else:
                detail = (
                    f"{current_pct:.1f}% of the {sleeve} sleeve against a "
                    f"{rules.hard_cap_pct:.0f}% hard cap -- above the cap at cost "
                    f"({cost_pct:.1f}%), so this was a sizing failure rather than appreciation."
                )
            flags.append(DisciplineFlag(pos.symbol, TRIM_TO_CAP, detail))
    return flags


def thesis_broken_flags(
    records: Sequence[RecommendationRecord], met_conditions: dict[str, bool]
) -> list[DisciplineFlag]:
    """Flag positions whose stated invalidation condition has been met.

    Independent of price: a thesis breaks when the condition written at
    purchase comes true, whatever the quote is doing.
    """
    flags = []
    for record in records:
        if met_conditions.get(record.symbol):
            flags.append(
                DisciplineFlag(
                    record.symbol,
                    THESIS_BROKEN,
                    f"condition met: {record.thesis_invalidation}",
                )
            )
    return flags


@dataclass
class DisciplineReport:
    intended_vs_actual: list[dict] = field(default_factory=list)
    cumulative_drift_pct: Optional[float] = None
    size_deviation_count: int = 0
    flags: list[DisciplineFlag] = field(default_factory=list)


def quarterly_discipline_report(
    records: Sequence[RecommendationRecord],
    executions: Sequence[ExecutionRecord],
    portfolio: Portfolio,
    params: Params,
    thesis_conditions_met: Optional[dict[str, bool]] = None,
) -> DisciplineReport:
    by_symbol = {e.symbol: e for e in executions}
    report = DisciplineReport()

    total_intended = 0.0
    total_actual = 0.0
    for record in records:
        execution = by_symbol.get(record.symbol)
        row = {
            "symbol": record.symbol,
            "intended_value": record.intended_value,
            "intended_sleeve_pct": record.intended_sleeve_pct,
            "actual_value": execution.actual_value if execution else None,
            "deviation_pct": execution.deviation_pct if execution else None,
            "size_deviation": execution.size_deviation if execution else False,
            "reason": execution.reason_for_deviation if execution else None,
        }
        report.intended_vs_actual.append(row)
        if execution:
            total_intended += record.intended_value
            total_actual += execution.actual_value
            if execution.size_deviation:
                report.size_deviation_count += 1

    if total_intended > 0:
        report.cumulative_drift_pct = 100.0 * (total_actual / total_intended - 1.0)

    report.flags.extend(trim_to_cap_flags(portfolio, params))
    report.flags.extend(thesis_broken_flags(records, thesis_conditions_met or {}))
    return report
