"""§13.5 — classification accuracy, and it is a strategy-break criterion.

    "Classification accuracy as its own metric — how often did A6 route a
    company to the method that best explained its subsequent behaviour?
    Below 70% is a strategy-break criterion."

That sentence has two defensible readings, and since the number can end the
strategy, which one is meant matters:

1. **Criteria persistence** — did the tag's own A6 criteria still hold a year
   later?  A CORE-STABLE that stayed profitable with growth under 20% was
   correctly routed; one that became a 40% grower was not.
2. **Method fit** — did the assigned method's fair value track subsequent price
   better than the alternatives would have?

This module implements **(1)**, by decision.  The reasoning: (2) partly measures
whether the *market* agreed with the valuation, which is a different question
from whether the routing was right, and a strategy-break criterion should not
be contaminated by it.  A6 is a router; the thing to test is whether it routed
to a box the company stayed in long enough for the method to apply.

The known weakness of (1) is stated rather than hidden: a company can satisfy
its tag's criteria for a year while the valuation method still fits it badly.
This measures routing *stability*, which is necessary for the method to have
been appropriate but is not sufficient.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Sequence

from ..classification import Classification
from ..config import Config
from ..data.adapter import DataAdapter, DataUnavailable
from ..modules import a_health

#: How far forward to look before asking whether the tag still fits.  Twelve
#: months is the shortest horizon over which a full year of fresh fundamentals
#: exists for every reporting frequency.
LOOKFORWARD_MONTHS = 12


@dataclass(frozen=True)
class ClassificationOutcome:
    """One routing decision, judged a year later."""

    symbol: str
    assigned_on: date
    assigned: Classification
    reviewed_on: date
    later: Classification | None
    #: ``None`` when the company could not be re-evaluated at all, which is
    #: counted separately from a wrong routing.
    correct: bool | None

    @property
    def evaluable(self) -> bool:
        return self.correct is not None

    def as_report_line(self) -> str:
        if self.correct is None:
            return (
                f"  {self.symbol}: {self.assigned.value} -> NOT EVALUABLE "
                f"(no usable data at {self.reviewed_on.isoformat()})"
            )
        verdict = "held" if self.correct else f"became {self.later.value if self.later else 'unclassifiable'}"
        return (
            f"  {self.symbol}: {self.assigned.value} @ "
            f"{self.assigned_on.isoformat()} -> {verdict}"
        )


@dataclass
class AccuracyReport:
    """§13.5's number, overall and per classification."""

    outcomes: list[ClassificationOutcome] = field(default_factory=list)
    floor: float = 0.70
    method: str = "criteria persistence at 12 months"

    @property
    def evaluable(self) -> list[ClassificationOutcome]:
        return [o for o in self.outcomes if o.evaluable]

    @property
    def accuracy(self) -> float | None:
        rows = self.evaluable
        if not rows:
            return None
        return sum(1 for o in rows if o.correct) / len(rows)

    @property
    def tripped(self) -> bool:
        """Below 70% is a strategy-break criterion."""
        accuracy = self.accuracy
        return accuracy is not None and accuracy < self.floor

    def by_classification(self) -> dict[Classification, tuple[float, int]]:
        grouped: dict[Classification, list[ClassificationOutcome]] = {}
        for outcome in self.evaluable:
            grouped.setdefault(outcome.assigned, []).append(outcome)
        return {
            tag: (sum(1 for o in rows if o.correct) / len(rows), len(rows))
            for tag, rows in sorted(grouped.items(), key=lambda kv: kv[0].value)
        }

    def as_report_lines(self) -> list[str]:
        lines = [
            "CLASSIFICATION ACCURACY (§13.5)",
            f"  method: {self.method}",
            f"  routings judged: {len(self.evaluable)} of {len(self.outcomes)} "
            f"({len(self.outcomes) - len(self.evaluable)} not evaluable)",
        ]
        accuracy = self.accuracy
        if accuracy is None:
            lines.append("  accuracy: not measurable — no routing could be judged")
            return lines

        status = "TRIPPED — strategy-break criterion" if self.tripped else "ok"
        lines.append(f"  accuracy: {accuracy:.1%} against a {self.floor:.0%} floor [{status}]")
        for tag, (rate, count) in self.by_classification().items():
            lines.append(f"    {tag.value}: {rate:.1%} of {count}")
        if self.tripped:
            lines.append(
                "  A6 is routing companies to methods their subsequent behaviour "
                "did not bear out. This is one of the only valid reasons to "
                "abandon the strategy (Module I)."
            )
        return lines


def _shift_months(day: date, months: int) -> date:
    year = day.year + (day.month - 1 + months) // 12
    month = (day.month - 1 + months) % 12 + 1
    return date(year, month, min(day.day, 28))


def judge_routing(
    adapter: DataAdapter,
    config: Config,
    symbol: str,
    assigned: Classification,
    assigned_on: date,
    *,
    lookforward_months: int = LOOKFORWARD_MONTHS,
) -> ClassificationOutcome:
    """Re-classify one name a year on and compare.

    Pins the adapter to the review date so the re-classification uses only what
    was filed by then — the same point-in-time discipline the rest of the
    backtest runs under.
    """
    reviewed_on = _shift_months(assigned_on, lookforward_months)

    previous = getattr(adapter, "as_of", None)
    try:
        if hasattr(adapter, "as_of"):
            setattr(adapter, "as_of", reviewed_on)
        inner = getattr(adapter, "fundamentals", None)
        if inner is not None and hasattr(inner, "as_of"):
            setattr(inner, "as_of", reviewed_on)

        try:
            data = adapter.load_company(symbol)
        except (DataUnavailable, Exception):
            return ClassificationOutcome(
                symbol, assigned_on, assigned, reviewed_on, None, None
            )

        later, _considered, _reasons = a_health.classify(data, config)
    finally:
        if hasattr(adapter, "as_of"):
            setattr(adapter, "as_of", previous)
        inner = getattr(adapter, "fundamentals", None)
        if inner is not None and hasattr(inner, "as_of"):
            setattr(inner, "as_of", previous)

    if later is None:
        # The company no longer satisfies any tag's criteria.  That is a
        # routing that did not hold, not an unevaluable one — the company is
        # readable, it has simply left every box.
        return ClassificationOutcome(
            symbol, assigned_on, assigned, reviewed_on, None, False
        )

    return ClassificationOutcome(
        symbol, assigned_on, assigned, reviewed_on, later, later is assigned
    )


def measure_accuracy(
    adapter: DataAdapter,
    config: Config,
    routings: Sequence[tuple[str, Classification, date]],
    *,
    lookforward_months: int = LOOKFORWARD_MONTHS,
    horizon_end: date | None = None,
) -> AccuracyReport:
    """Judge every routing that has had time to be judged.

    Routings whose review date falls beyond the data's end are excluded rather
    than counted as failures — a decision cannot be wrong for not yet having
    had a year to play out.
    """
    report = AccuracyReport(floor=config.expectations.classification_accuracy_floor)

    for symbol, assigned, assigned_on in routings:
        reviewed_on = _shift_months(assigned_on, lookforward_months)
        if horizon_end is not None and reviewed_on > horizon_end:
            continue
        report.outcomes.append(
            judge_routing(
                adapter, config, symbol, assigned, assigned_on,
                lookforward_months=lookforward_months,
            )
        )
    return report


__all__ = [
    "AccuracyReport",
    "ClassificationOutcome",
    "LOOKFORWARD_MONTHS",
    "judge_routing",
    "measure_accuracy",
]
