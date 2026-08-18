"""Module I -- expectation and drought discipline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional, Sequence

from ..config import Params

EXPECTATION_STATEMENT = (
    "Value and quality factors have historically delivered roughly 2-5%/year of excess "
    "return, arriving unevenly, with underperformance stretches lasting 5-10 years. "
    "Growth-classified positions are expected to produce total losses on roughly a third of "
    "holdings and sleeve drawdowns of 40-60% in adverse markets. Neither anchor in this "
    "system is a discovered truth: own-history assumes the past multiple range still "
    "applies, peer comparison assumes peers are fairly priced. Agreement between them "
    "raises confidence. It does not eliminate the possibility that both are wrong in the "
    "same direction at once."
)

STRATEGY_BREAK_CRITERIA = (
    "Demonstrated data corruption or systematic gate miscomputation",
    "Systematic misclassification (Module A6 repeatedly routing companies to the wrong "
    "valuation method)",
    "Peer-selection methodology proven gameable in backtest",
    "Realised loss rate materially exceeding expectations over a full cycle with 20+ "
    "closed positions",
)

NOT_STRATEGY_BREAK_CRITERIA = (
    "Drawdown depth",
    "Underperformance duration",
    "Any individual position's outcome",
)


@dataclass
class PerformanceBand:
    horizon: str
    expected_low_pct: float
    expected_high_pct: float
    actual_pct: Optional[float] = None

    @property
    def within_expectation(self) -> Optional[bool]:
        if self.actual_pct is None:
            return None
        return self.expected_low_pct <= self.actual_pct <= self.expected_high_pct

    def describe(self) -> str:
        if self.actual_pct is None:
            return f"{self.horizon} vs index: no data yet (pre-registered band {self.expected_low_pct:+.0f}% to {self.expected_high_pct:+.0f}%)"
        verdict = "within" if self.within_expectation else "outside"
        return (
            f"{self.horizon} vs index: {self.actual_pct:+.1f}% -- {verdict} the pre-registered "
            f"band of {self.expected_low_pct:+.0f}% to {self.expected_high_pct:+.0f}%"
        )


def default_bands() -> list[PerformanceBand]:
    """Pre-registered relative-performance bands.

    Registered in advance so that performance is judged against what was
    forecast rather than against whatever feels disappointing at the time.
    """
    return [
        PerformanceBand("rolling 1y", -15.0, 20.0),
        PerformanceBand("rolling 3y", -10.0, 15.0),
        PerformanceBand("rolling 5y", -5.0, 12.0),
    ]


@dataclass
class CoolingOffEntry:
    requested_on: date
    change_description: str
    is_sleeve_cap_increase: bool
    required_days: int
    eligible_on: date
    resolved: bool = False

    def is_eligible(self, as_of: date) -> bool:
        return as_of >= self.eligible_on


@dataclass
class DisciplineLedger:
    entries: list[CoolingOffEntry] = field(default_factory=list)

    def request_change(
        self, requested_on: date, description: str, params: Params, sleeve_cap_increase: bool = False
    ) -> CoolingOffEntry:
        """Log a mid-year change request and start its cooling-off clock.

        Sleeve cap increases cool off for longer because raising the cap after a
        good run is the most likely way this system does real damage.
        """
        dp = params.discipline
        days = (
            dp.sleeve_cap_increase_cooling_off_days
            if sleeve_cap_increase
            else dp.parameter_change_cooling_off_days
        )
        entry = CoolingOffEntry(
            requested_on=requested_on,
            change_description=description,
            is_sleeve_cap_increase=sleeve_cap_increase,
            required_days=days,
            eligible_on=requested_on + timedelta(days=days),
        )
        self.entries.append(entry)
        return entry

    def pending(self, as_of: date) -> list[CoolingOffEntry]:
        return [e for e in self.entries if not e.resolved and not e.is_eligible(as_of)]


def report_header(
    params: Params,
    bands: Optional[Sequence[PerformanceBand]] = None,
    extra_notices: Optional[Sequence[str]] = None,
) -> str:
    """The block that opens every report, expectation statement verbatim."""
    lines: list[str] = []
    for notice in extra_notices or ():
        lines.append(f"!! {notice}")
    if lines:
        lines.append("")
    lines.append("EXPECTATION STATEMENT (Module I)")
    lines.append(EXPECTATION_STATEMENT)
    lines.append("")
    lines.append("PRE-REGISTERED PERFORMANCE BANDS")
    for band in bands or default_bands():
        lines.append(f"  {band.describe()}")
    lines.append("")
    lines.append(
        f"Minimum evaluation horizon: {params.discipline.min_evaluation_horizon_years} years. "
        f"Parameter revision in force: {params.revision}."
    )
    return "\n".join(lines)
