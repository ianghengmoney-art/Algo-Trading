"""The §13 validation report.

Structured as the protocol's own eleven numbered requirements, so a reader can
check compliance point by point rather than take the report's word for it — and
so the two requirements this build *cannot* satisfy are visible as gaps rather
than absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from ..config import Config
from ..modules.i_expectations import EXPECTATION_STATEMENT, evaluate_break_criteria
from .accuracy import AccuracyReport
from .engine import MANDATORY_WINDOWS, BacktestResult, WalkForwardSplit
from .metrics import (
    PerformanceSummary,
    annualised_return,
    distribution_by_classification,
    rolling_returns,
    summarise,
    window_slice,
)
from .sweep import SweepReport
from .variants import MISSING_BENCHMARKS

RULE = "=" * 78
THIN = "-" * 78


@dataclass
class ValidationReport:
    """§13, assembled."""

    primary: BacktestResult
    benchmarks: list[tuple[str, str, PerformanceSummary]] = field(default_factory=list)
    accuracy: AccuracyReport | None = None
    sweep: SweepReport | None = None
    point_in_time: bool = False
    paper_traded_months: float = 0.0
    notes: list[str] = field(default_factory=list)

    def render(self, config: Config) -> str:
        result = self.primary
        summary = summarise(
            result.label, result.equity_curve, result.book.closed, result.benchmark_curve
        )

        lines = [RULE, "GCFP v4 — §13 VALIDATION REPORT", RULE]

        # §13.8 first: the caveat that conditions everything below it.
        if not self.point_in_time:
            lines.extend([
                "",
                "** POINT-IN-TIME DATA NOT USED **",
                "  §13.8 requires point-in-time constituents and peer sets. Without",
                "  them, today's peer list is being used to backtest yesterday's",
                "  decisions, which bakes survivorship bias into the methodology.",
                "  RESULTS BELOW ARE INFLATED BY AN UNKNOWN MATERIAL AMOUNT.",
                "  The spec is explicit that this belongs in the header, not a footnote.",
            ])
        else:
            lines.extend([
                "",
                "Point-in-time data: IN USE. Every evaluation saw only what had been",
                "filed by its own date, so restatements and late filings enter the",
                "record when they actually became knowable.",
            ])

        lines.extend([
            "",
            f"period: {result.settings.start.isoformat()}..{result.settings.end.isoformat()}"
            f"  ({(result.settings.end - result.settings.start).days / 365.25:.1f} years)",
            f"parameters: {result.config_fingerprint}",
        ])
        if result.split:
            lines.append(f"walk-forward (§13.3): {result.split.as_report_line()}")

        # §13.1
        lines.extend(["", THIN, "§13.1 — RESULTS, AND PER CLASSIFICATION", THIN])
        lines.extend(summary.as_report_lines())
        by_class = distribution_by_classification(result.book.closed)
        if by_class:
            lines.append("")
            lines.append(
                "  Per classification (a blended backtest hides whether Growth is"
            )
            lines.append("  carrying or dragging the system):")
            for tag, stats in by_class.items():
                lines.append(f"    {tag.value}")
                lines.extend(f"    {l}" for l in stats.as_report_lines())
        else:
            lines.append("  no closed positions to split by classification")

        # §13.2
        lines.extend(["", THIN, "§13.2 — MANDATORY WINDOWS", THIN])
        curve = result.equity_curve
        for name, start, end in MANDATORY_WINDOWS:
            window = window_slice(curve, start, end)
            if len(window) < 2:
                lines.append(
                    f"  {name} ({start.year}-{end.year}): NOT COVERED by this "
                    "backtest period"
                )
                continue
            bench = window_slice(result.benchmark_curve, start, end)
            own = annualised_return(window)
            benchmark = annualised_return(bench)
            line = f"  {name} ({start.year}-{end.year}): {own:+.2%}/yr"
            if benchmark is not None:
                line += f" vs benchmark {benchmark:+.2%}/yr"
            lines.append(line)

        uncovered = [
            name for name, start, end in MANDATORY_WINDOWS
            if len(window_slice(curve, start, end)) < 2
        ]
        if uncovered:
            lines.append("")
            lines.append(
                f"  ** {len(uncovered)} of {len(MANDATORY_WINDOWS)} mandatory windows "
                "were not covered. §13.2 requires all three; extend the backtest "
                "period or state this as an incomplete validation. **"
            )

        # §13.4
        lines.extend(["", THIN, "§13.4 — BENCHMARKS", THIN])
        base_annual = summary.annualised
        for name, proves, bench_summary in self.benchmarks:
            delta = (
                base_annual - bench_summary.annualised
                if base_annual is not None and bench_summary.annualised is not None
                else None
            )
            verdict = ""
            if delta is not None:
                verdict = (
                    "  <- GCFP v4 ahead" if delta > 0 else "  <- BENCHMARK AHEAD"
                )
            lines.append(
                f"  {name}: {bench_summary.annualised:+.2%}/yr"
                if bench_summary.annualised is not None
                else f"  {name}: not measurable"
            )
            lines.append(f"      proves: {proves}")
            if delta is not None:
                lines.append(f"      difference: {delta:+.2%}/yr{verdict}")
            if delta is not None and delta <= 0:
                lines.append(
                    "      A benchmark that matches or beats the full system is the "
                    "protocol working: it says this component does not earn its "
                    "complexity."
                )
        for name, why in MISSING_BENCHMARKS.items():
            lines.append(f"  {name}: NOT RUN")
            lines.append(f"      {why}")

        # §13.5
        lines.extend(["", THIN, "§13.5 — CLASSIFICATION ACCURACY", THIN])
        if self.accuracy is not None:
            lines.extend(f"  {l}" for l in self.accuracy.as_report_lines()[1:])
        else:
            lines.append("  not measured")

        # §13.6 is folded into §13.1's distribution output above.
        lines.extend(["", THIN, "§13.6 — RETURN DISTRIBUTION", THIN])
        lines.append("  reported above under §13.1, per classification.")
        for years in (1, 3, 5):
            rolling = rolling_returns(curve, years)
            if rolling:
                values = [r for _, r in rolling]
                lines.append(
                    f"  rolling {years}y: min {min(values):+.1%} · "
                    f"median {sorted(values)[len(values)//2]:+.1%} · "
                    f"max {max(values):+.1%}"
                )

        # §13.7
        lines.extend(["", THIN, "§13.7 — PARAMETER SWEEP", THIN])
        if self.sweep is not None:
            lines.extend(f"  {l}" for l in self.sweep.as_report_lines()[1:])
        else:
            lines.append("  not run")

        # §13.9
        lines.extend(["", THIN, "§13.9 — C5 FREQUENCY", THIN])
        rate = result.single_anchor_rate
        if rate is None:
            lines.append("  no anchor modes recorded")
        else:
            status = (
                " ** ABOVE the 40% break criterion **"
                if rate > config.expectations.single_anchor_rate_break
                else ""
            )
            lines.append(
                f"  SINGLE-ANCHOR MODE: {rate:.1%} of evaluated candidates{status}"
            )
            single_closed = [
                c for c in result.book.closed if c.anchor_mode.startswith("SINGLE")
            ]
            dual_closed = [
                c for c in result.book.closed if not c.anchor_mode.startswith("SINGLE")
            ]
            if single_closed and dual_closed:
                from .metrics import distribution

                single = distribution(single_closed)
                dual = distribution(dual_closed)
                lines.append(
                    f"  realised outcomes — single-anchor median "
                    f"{single.median_return:+.1%} (n={single.count}) vs dual "
                    f"{dual.median_return:+.1%} (n={dual.count})"
                )
                lines.append(
                    "  This is what validates whether C5's penalties are calibrated."
                )
            else:
                lines.append(
                    "  too few closed positions in one mode to compare outcomes"
                )

        divergence = result.divergence_rate
        if divergence is not None:
            status = (
                " ** ABOVE the 50% break criterion **"
                if divergence > config.expectations.divergence_rate_break
                else ""
            )
            lines.append(f"  C4 divergence fired on {divergence:.1%} of dual-anchor candidates{status}")

        # §13.10
        lines.extend(["", THIN, "§13.10 — RECLASSIFICATION FREQUENCY", THIN])
        lines.append(f"  H3 fired {result.reclassification_count} time(s)")
        transitions = [
            t for r in result.rebalances for t in r.reclassifications
        ]
        for symbol, old, new in transitions[:20]:
            lines.append(f"    {symbol}: {old} -> {new}")

        # §13.11
        lines.extend(["", THIN, "§13.11 — PAPER TRADING", THIN])
        if self.paper_traded_months >= 2:
            lines.append(f"  {self.paper_traded_months:.1f} months completed")
        else:
            lines.append(
                "  NOT DONE. §13.11 requires 2-3 months minimum before real money."
            )
            lines.append(
                "  Backtests catch strategy flaws; paper trading catches pipeline "
                "flaws. Different failure classes — a clean backtest does not "
                "substitute."
            )

        # Module I break criteria, computed off this run.
        lines.extend(["", THIN, "MODULE I — STRATEGY-BREAK CRITERIA", THIN])
        integrity = evaluate_break_criteria(
            config,
            closed_positions=len(result.book.closed),
            classification_accuracy=self.accuracy.accuracy if self.accuracy else None,
            single_anchor_rate=result.single_anchor_rate,
            divergence_rate=result.divergence_rate,
        )
        lines.extend(f"  {l}" for l in integrity.as_report_lines()[1:])

        lines.extend(["", THIN, "MODULE I — EXPECTATION STATEMENT (verbatim)", THIN])
        lines.append(f'  "{EXPECTATION_STATEMENT}"')

        if self.notes:
            lines.extend(["", THIN, "NOTES", THIN])
            lines.extend(f"  {n}" for n in self.notes)

        lines.append(RULE)
        return "\n".join(lines)


__all__ = ["ValidationReport"]
