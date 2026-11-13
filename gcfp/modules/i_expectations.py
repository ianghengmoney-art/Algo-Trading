"""Module I — expectation and drought discipline.

The verbatim statement below is printed on every report.  It is not decoration:
Prime Directives 6 and 8 both depend on the operator having already been told,
in writing, that zero passers and long droughts are the system working.  A
drawdown is a bad time to first encounter that idea.

The process-based break criteria fix v3 flaw 14.  v3's only break criterion
required 20+ closed positions, which at this cadence could take a decade — long
enough for a broken system to do real damage before its own rules admitted it.
These can be evaluated in year one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Sequence

from ..config import Config

#: §10 requires this printed verbatim on every report.  Do not paraphrase.
EXPECTATION_STATEMENT = (
    "Value and quality factors have historically delivered roughly 2-5%/year of "
    "excess return, arriving unevenly, with underperformance stretches lasting "
    "5-10 years. Growth-classified positions are expected to produce total "
    "losses on roughly a third of holdings and sleeve drawdowns of 40-60% in "
    "adverse markets. Neither anchor in this system is a discovered truth: "
    "own-history assumes the past multiple range still applies, peer comparison "
    "assumes peers are fairly priced. Agreement between them raises confidence. "
    "It does not eliminate the possibility that both are wrong in the same "
    "direction at once."
)

#: Explicitly not valid reasons to abandon the strategy.
NOT_VALID_REASONS = (
    "drawdown depth",
    "underperformance duration",
    "any individual position's outcome",
)


@dataclass(frozen=True)
class BreakCriterion:
    name: str
    tripped: bool
    kind: str  # "process" or "outcome"
    value: float | None
    threshold: float | None
    detail: str

    def as_report_line(self) -> str:
        status = "TRIPPED" if self.tripped else "ok"
        bits = f"[{status}] {self.kind}: {self.name}"
        if self.value is not None and self.threshold is not None:
            bits += f" — {self.value:.1%} vs {self.threshold:.1%}"
        if self.detail:
            bits += f" · {self.detail}"
        return bits


@dataclass
class StrategyIntegrityReport:
    as_of: date
    criteria: list[BreakCriterion]
    closed_positions: int

    @property
    def any_tripped(self) -> bool:
        return any(c.tripped for c in self.criteria)

    @property
    def tripped(self) -> list[BreakCriterion]:
        return [c for c in self.criteria if c.tripped]

    def as_report_lines(self) -> list[str]:
        lines = [f"STRATEGY INTEGRITY · {self.as_of.isoformat()}"]
        lines.extend(f"  {c.as_report_line()}" for c in self.criteria)
        if self.any_tripped:
            lines.append(
                "  ** A break criterion has tripped. This is one of the only "
                "valid reasons to abandon the strategy. **"
            )
        lines.append(
            "  Not valid reasons to abandon: " + " · ".join(NOT_VALID_REASONS)
        )
        return lines


def evaluate_break_criteria(
    config: Config,
    *,
    closed_positions: int,
    realised_loss_rate: float | None = None,
    expected_loss_rate: float | None = None,
    data_corruption_demonstrated: bool = False,
    classification_accuracy: float | None = None,
    peer_methodology_spread: float | None = None,
    single_anchor_rate: float | None = None,
    divergence_rate: float | None = None,
    as_of: date | None = None,
) -> StrategyIntegrityReport:
    """The only valid reasons to abandon, split by when they become evaluable.

    Outcome-based criteria need a full cycle and 20+ closed positions.  The
    process-based ones are evaluable from the first year, and catch a broken
    system far earlier than waiting for outcomes to prove it.
    """
    cfg = config.expectations
    as_of = as_of or date.today()
    criteria: list[BreakCriterion] = []

    # Outcome-based.
    outcome_evaluable = closed_positions >= cfg.min_closed_positions_for_outcome_break
    tripped = False
    detail = (
        f"{closed_positions} closed positions; needs "
        f"{cfg.min_closed_positions_for_outcome_break}"
    )
    if outcome_evaluable and realised_loss_rate is not None and expected_loss_rate is not None:
        tripped = realised_loss_rate > expected_loss_rate * 1.5
        detail = "evaluable"
    criteria.append(
        BreakCriterion(
            "realised loss rate materially exceeding expectations",
            tripped,
            "outcome",
            realised_loss_rate,
            expected_loss_rate,
            detail if not outcome_evaluable else "",
        )
    )

    # Process-based — evaluable from year one.
    criteria.append(
        BreakCriterion(
            "demonstrated data corruption or systematic gate miscomputation",
            data_corruption_demonstrated,
            "process",
            None,
            None,
            "asserted by the operator or by a failing integrity check",
        )
    )
    criteria.append(
        BreakCriterion(
            "classification accuracy below 70% on the backtest's own metric",
            classification_accuracy is not None
            and classification_accuracy < cfg.classification_accuracy_floor,
            "process",
            classification_accuracy,
            cfg.classification_accuracy_floor,
            "" if classification_accuracy is not None else "not yet measured",
        )
    )
    criteria.append(
        BreakCriterion(
            "peer-selection methodology proven gameable",
            peer_methodology_spread is not None
            and peer_methodology_spread > cfg.peer_gameability_spread,
            "process",
            peer_methodology_spread,
            cfg.peer_gameability_spread,
            "two reasonable analysts producing >30% different fair values means "
            "C2 is not a method, it is an opinion",
        )
    )
    criteria.append(
        BreakCriterion(
            "SINGLE-ANCHOR MODE exceeding 40% of evaluated candidates",
            single_anchor_rate is not None
            and single_anchor_rate > cfg.single_anchor_rate_break,
            "process",
            single_anchor_rate,
            cfg.single_anchor_rate_break,
            "the anchors are not available at the assumed rate and the system is "
            "not doing what it claims",
        )
    )
    criteria.append(
        BreakCriterion(
            "anchor divergence (C4) firing on more than 50% of candidates",
            divergence_rate is not None and divergence_rate > cfg.divergence_rate_break,
            "process",
            divergence_rate,
            cfg.divergence_rate_break,
            "the two methods disagree so often that 'triangulation' is not "
            "producing signal",
        )
    )

    return StrategyIntegrityReport(as_of, criteria, closed_positions)


# -- pre-commitments ------------------------------------------------------


@dataclass(frozen=True)
class CoolingOffRecord:
    """A logged parameter-change request and the date it may be acted on.

    Sleeve cap increases carry the longer wait because raising the cap after a
    good run is the most likely way this system does real damage.
    """

    requested_on: date
    change: str
    is_sleeve_cap_increase: bool
    eligible_on: date
    rationale: str

    def as_report_line(self) -> str:
        kind = "sleeve cap increase" if self.is_sleeve_cap_increase else "parameter change"
        return (
            f"COOLING-OFF · {kind} requested {self.requested_on.isoformat()}, "
            f"eligible {self.eligible_on.isoformat()}: {self.change}"
        )


def request_parameter_change(
    change: str,
    rationale: str,
    config: Config,
    is_sleeve_cap_increase: bool = False,
    requested_on: date | None = None,
) -> CoolingOffRecord:
    """Mid-year change requests trigger a logged cooling-off period."""
    requested_on = requested_on or date.today()
    days = (
        config.expectations.sleeve_cap_increase_cooling_off_days
        if is_sleeve_cap_increase
        else config.expectations.parameter_change_cooling_off_days
    )
    return CoolingOffRecord(
        requested_on=requested_on,
        change=change,
        is_sleeve_cap_increase=is_sleeve_cap_increase,
        eligible_on=requested_on + timedelta(days=days),
        rationale=rationale,
    )


def cooling_off_active(record: CoolingOffRecord, as_of: date | None = None) -> bool:
    return (as_of or date.today()) < record.eligible_on


def report_header_lines(config: Config) -> list[str]:
    """The Module I block that opens every report."""
    return [
        "MODULE I — EXPECTATION STATEMENT (printed verbatim, every report):",
        f'  "{EXPECTATION_STATEMENT}"',
        f"  Minimum evaluation horizon: {config.expectations.min_evaluation_years} years.",
    ]


__all__ = [
    "EXPECTATION_STATEMENT",
    "NOT_VALID_REASONS",
    "BreakCriterion",
    "StrategyIntegrityReport",
    "CoolingOffRecord",
    "evaluate_break_criteria",
    "request_parameter_change",
    "cooling_off_active",
    "report_header_lines",
]
