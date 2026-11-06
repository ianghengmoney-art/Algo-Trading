"""Module H — holdings monitor.

H1 fixes v3 flaw 11: the quarterly cadence assumed quarterly reporting.  Many
foreign issuers and ADRs report semi-annually, and a rigid quarterly re-run
either processes stale data or silently skips.  Cadence follows the holding's
own reporting frequency.

H2 fixes v3 flaw 7: a position could decay into "does not qualify" without a
flag.  v3 triggered only on a *relative* drop, so a name bought at 62 that
drifted to 45 fell 17 points and flagged nothing, while sitting squarely in
marginal territory.  Absolute score bands now trigger independently.

H3 fixes v3 flaw 13: v3 flagged a classification change for REVIEW but never
said what to do about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Sequence

from ..classification import Classification
from ..config import Config
from ..ledger import AuditLedger
from ..types import CompanyData, ReportingFrequency


class MonitorFlag(str, Enum):
    SELL = "SELL"
    REVIEW = "REVIEW"
    GREEN = "GREEN"

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.value


@dataclass(frozen=True)
class CadenceDecision:
    """H1's verdict for one holding on one date."""

    should_full_rerun: bool
    should_price_and_news_check: bool
    frequency: ReportingFrequency
    reason: str

    def as_log_line(self) -> str:
        action = (
            "FULL RE-RUN A->E"
            if self.should_full_rerun
            else "PRICE-AND-NEWS CHECK"
            if self.should_price_and_news_check
            else "no action"
        )
        return f"H1 cadence: {action} ({self.frequency.value}) — {self.reason}"


def cadence(
    data: CompanyData,
    config: Config,
    last_full_rerun: date | None,
    as_of: date | None = None,
) -> CadenceDecision:
    """Full re-run of Modules A->E after each of the holding's *own* earnings
    reports, not on a fixed calendar quarter.

    Semi-annual reporters additionally get a price-and-news-only check at the
    six-month midpoint, flagging any move > 25% or any A4 red-flag event.
    """
    as_of = as_of or date.today()
    profile = data.profile
    freq = profile.reporting_frequency
    last_report = profile.last_report_date

    if last_report is None:
        return CadenceDecision(
            False, False, freq, "no last-report date; cadence cannot be determined"
        )

    # A report the holding has filed since the last full re-run triggers one.
    if last_full_rerun is None or last_report > last_full_rerun:
        return CadenceDecision(
            True,
            False,
            freq,
            f"report filed {last_report.isoformat()} has not been processed",
        )

    if freq is ReportingFrequency.SEMIANNUAL:
        midpoint = last_report + timedelta(days=183)
        if as_of >= midpoint:
            return CadenceDecision(
                False,
                True,
                freq,
                f"six-month midpoint reached ({midpoint.isoformat()}); "
                "price-and-news check only",
            )

    return CadenceDecision(
        False, False, freq, f"next report not yet filed (last {last_report.isoformat()})"
    )


def filing_is_late(
    data: CompanyData, config: Config, as_of: date | None = None
) -> bool:
    """A scheduled report more than 15 days late is an automatic A4 failure."""
    as_of = as_of or date.today()
    due = data.profile.next_report_due_date
    if due is None:
        return False
    last = data.profile.last_report_date
    if last is not None and last >= due:
        return False
    return (as_of - due).days > config.health.max_filing_delay_days


@dataclass
class MonitorVerdict:
    symbol: str
    flag: MonitorFlag
    sell_reasons: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_report_lines(self) -> list[str]:
        lines = [f"{self.flag.value} · {self.symbol}"]
        lines.extend(f"  SELL: {r}" for r in self.sell_reasons)
        lines.extend(f"  REVIEW: {r}" for r in self.review_reasons)
        lines.extend(f"  {n}" for n in self.notes)
        return lines


@dataclass
class GrowthDeterioration:
    """The GROWTH-only SELL conditions, each needing two consecutive reports."""

    revenue_growth_below_10_streak: int = 0
    rule_of_40_below_20_streak: int = 0
    runway_below_12_months: bool = False
    has_credible_financing_plan: bool = False


def evaluate_holding(
    symbol: str,
    classification: Classification,
    config: Config,
    *,
    module_a_failed: bool,
    both_anchors_overvalued: bool,
    premium_above_threshold: bool,
    thesis_broken: bool,
    conviction_now: float,
    conviction_at_purchase: float,
    gates_weakening: bool = False,
    anchors_now_diverging: bool = False,
    re_rating_detected: bool = False,
    past_hard_cap: bool = False,
    classification_changed: bool = False,
    growth: GrowthDeterioration | None = None,
    ledger: AuditLedger | None = None,
) -> MonitorVerdict:
    """H2 — apply every SELL and REVIEW trigger.

    Banned as triggers: any function of price history alone.  Note that
    ``premium_above_threshold`` is not sufficient on its own; it only counts
    alongside both anchors confirming overvaluation, exactly as Module E
    requires.
    """
    cfg = config.monitor
    sell: list[str] = []
    review: list[str] = []
    notes: list[str] = []

    if module_a_failed:
        sell.append("Module A outright FAIL")
    if both_anchors_overvalued and premium_above_threshold:
        sell.append("both anchors confirm overvaluation past the Y% threshold")
    if thesis_broken:
        sell.append("thesis-broken condition met")
    if conviction_now < cfg.sell_conviction_floor:
        sell.append(
            f"conviction {conviction_now:.1f} below the absolute floor of "
            f"{cfg.sell_conviction_floor:.0f} — independent of how far it fell"
        )

    if growth is not None and classification in (
        Classification.CORE_GROWTH,
        Classification.SPEC_GROWTH,
    ):
        need = cfg.growth_consecutive_reports_to_sell
        if growth.revenue_growth_below_10_streak >= need:
            sell.append(
                f"revenue growth below {cfg.growth_revenue_growth_floor:.0%} for "
                f"{growth.revenue_growth_below_10_streak} consecutive reports"
            )
        if growth.rule_of_40_below_20_streak >= need:
            sell.append(
                f"Rule of 40 below {cfg.growth_rule_of_40_floor:.0f} for "
                f"{growth.rule_of_40_below_20_streak} consecutive reports"
            )
        if growth.runway_below_12_months and not growth.has_credible_financing_plan:
            sell.append(
                f"cash runway below {cfg.growth_min_runway_months:.0f} months with "
                "no credible financing plan"
            )

    drop = conviction_at_purchase - conviction_now
    if drop >= cfg.review_conviction_drop:
        review.append(
            f"conviction fell {drop:.1f} points since purchase "
            f"({conviction_at_purchase:.1f} -> {conviction_now:.1f})"
        )
    # The absolute band, independent of drop size (fixes v3 flaw 7).
    if conviction_now < cfg.review_conviction_floor:
        review.append(
            f"conviction {conviction_now:.1f} below {cfg.review_conviction_floor:.0f} "
            f"in absolute terms, regardless of drop size (fell {drop:.1f})"
        )
    if gates_weakening:
        review.append("Module A gates weakening but not failing")
    if anchors_now_diverging:
        review.append("anchors that agreed at purchase now diverging")
    if re_rating_detected:
        review.append("re-rating step-change newly detected (C1.2)")
    if past_hard_cap:
        review.append("position past its hard cap on appreciation")
    if classification_changed:
        review.append("classification changed — H3 reclassification protocol applies")

    flag = MonitorFlag.SELL if sell else MonitorFlag.REVIEW if review else MonitorFlag.GREEN
    verdict = MonitorVerdict(symbol, flag, sell, review, notes)
    if ledger is not None:
        for line in verdict.as_report_lines():
            ledger.note(f"H2 · {line}")
    return verdict


# -- H3 -------------------------------------------------------------------


@dataclass
class ReclassificationStep:
    number: int
    name: str
    complete: bool
    detail: str = ""


@dataclass
class ReclassificationProtocol:
    """H3 — the defined action v3 was missing (fixes flaw 13)."""

    symbol: str
    old_tag: Classification
    new_tag: Classification
    triggered_on: date
    steps: list[ReclassificationStep] = field(default_factory=list)
    would_be_bought_today: bool | None = None
    outcome: str | None = None

    @property
    def frozen(self) -> bool:
        """Step 1 holds until the review completes."""
        return not all(s.complete for s in self.steps)

    def as_report_lines(self) -> list[str]:
        lines = [
            f"RECLASSIFICATION · {self.symbol}: {self.old_tag.value} -> "
            f"{self.new_tag.value} on {self.triggered_on.isoformat()}",
            f"  POSITION FROZEN: {'yes — no adds, no trims' if self.frozen else 'no — review complete'}",
        ]
        for step in self.steps:
            mark = "done" if step.complete else "pending"
            lines.append(f"  {step.number}. {step.name} [{mark}] {step.detail}".rstrip())
        if self.outcome:
            lines.append(f"  OUTCOME: {self.outcome}")
        return lines


def start_reclassification(
    symbol: str,
    old_tag: Classification,
    new_tag: Classification,
    triggered_on: date | None = None,
) -> ReclassificationProtocol:
    """Open the six-step protocol with every step pending.

    The position is frozen — no adds, no trims — until all six complete.
    """
    return ReclassificationProtocol(
        symbol=symbol,
        old_tag=old_tag,
        new_tag=new_tag,
        triggered_on=triggered_on or date.today(),
        steps=[
            ReclassificationStep(1, "Freeze the position", False),
            ReclassificationStep(
                2,
                "Rebuild from scratch under the new classification "
                "(Module B method, C2 peer set, C1 series with C1.1 applied to "
                "the transition date)",
                False,
            ),
            ReclassificationStep(
                3, "Recompute conviction under the new classification's thresholds", False
            ),
            ReclassificationStep(4, "Re-test the buy gate at current price", False),
            ReclassificationStep(5, "Re-size to the new classification's regime", False),
            ReclassificationStep(6, "Log the transition permanently", False),
        ],
    )


def complete_reclassification_step(
    protocol: ReclassificationProtocol, number: int, detail: str = ""
) -> ReclassificationProtocol:
    for i, step in enumerate(protocol.steps):
        if step.number == number:
            protocol.steps[i] = ReclassificationStep(
                step.number, step.name, True, detail
            )
    return protocol


def resolve_reclassification(
    protocol: ReclassificationProtocol, would_be_bought_today: bool
) -> ReclassificationProtocol:
    """Step 4's verdict.

    A "no" is a review, not an automatic sell: a business that grew from
    SPEC-GROWTH into CORE-GROWTH has improved, and forcing an exit on
    reclassification alone would sell exactly the winners the system exists to
    find.
    """
    protocol.would_be_bought_today = would_be_bought_today
    protocol.outcome = (
        "still qualifies under the new classification"
        if would_be_bought_today
        else (
            "NO LONGER QUALIFIES — REVIEW FOR EXIT. This is a review, not an "
            "automatic sell."
        )
    )
    return protocol


__all__ = [
    "MonitorFlag",
    "MonitorVerdict",
    "CadenceDecision",
    "GrowthDeterioration",
    "ReclassificationProtocol",
    "ReclassificationStep",
    "cadence",
    "filing_is_late",
    "evaluate_holding",
    "start_reclassification",
    "complete_reclassification_step",
    "resolve_reclassification",
]
