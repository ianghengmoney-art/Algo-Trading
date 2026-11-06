"""Report rendering.

§15 fixes what every report opens with, and the opening block is not
decoration: the BALLAST gap appears before any stock recommendation because a
portfolio below its ballast floor has a more urgent problem than a new idea,
and the Module I statement is printed verbatim so a drought is never a surprise.

All output is files plus console.  Nothing executes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

from .config import Config
from .modules import f_sizing, i_expectations, j_tax
from .modules.c_anchors import AnchorMode
from .modules.e_triggers import SignalType
from .modules.f_sizing import PortfolioState
from .modules.k_currency import ExposureReport, FxTable
from .pipeline import Evaluation

RULE = "=" * 78
THIN = "-" * 78


def opening_block(
    portfolio: PortfolioState,
    fx: FxTable,
    config: Config,
    evaluations: Sequence[Evaluation],
    exposure: ExposureReport | None = None,
) -> list[str]:
    """§15's mandatory header."""
    lines = [RULE, f"GCFP v4 · {date.today().isoformat()}", RULE]
    lines.append(f"BASE CURRENCY: {config.currency.base_currency}")

    if fx.rates:
        stamp = fx.as_of.isoformat() if fx.as_of else "undated"
        lines.append("FX RATES USED:")
        lines.extend(f"  {p}={r:.6g} @ {stamp}" for p, r in sorted(fx.rates.items()))
    else:
        lines.append("FX RATES USED: none — no foreign-currency positions priced")

    lines.append("")
    lines.append("BUCKET UTILISATION:")
    lines.extend(f"  {l}" for l in f_sizing.opening_report_lines(portfolio, config))

    # C5's portfolio-level cap.
    single_positions = len(portfolio.single_anchor_positions)
    total_positions = len(portfolio.position_values)
    cap = config.anchors.single_anchor_portfolio_cap
    lines.append("")
    if total_positions == 0:
        lines.append(
            f"SINGLE-ANCHOR POSITIONS: none held (cap {cap:.0%} of positions)"
        )
    else:
        rate = single_positions / total_positions
        status = " — AT OR OVER CAP" if rate >= cap else ""
        lines.append(
            f"SINGLE-ANCHOR POSITIONS: {single_positions} of {total_positions} "
            f"({rate:.0%}) against a {cap:.0%} cap{status}"
        )

    if exposure is not None:
        lines.append("")
        lines.extend(exposure.as_report_lines())

    lines.append("")
    lines.extend(i_expectations.report_header_lines(config))
    lines.append(RULE)
    return lines


def new_passer_report(
    evaluations: Sequence[Evaluation],
    portfolio: PortfolioState,
    fx: FxTable,
    config: Config,
    exposure: ExposureReport | None = None,
    diagnostics=None,
    sensitivity=None,
) -> str:
    """The weekly new-passer report.

    Every field §15 lists is present per name, including the ones that are
    "n/a" — a missing PEGY line and a PEGY that could not be computed look
    identical to a reader otherwise.
    """
    lines = opening_block(portfolio, fx, config, evaluations, exposure)

    buys = [e for e in evaluations if e.is_buy]
    reviews = [
        e
        for e in evaluations
        if e.signal is not None and e.signal.signal is SignalType.REVIEW
    ]
    stopped = [e for e in evaluations if e.stopped_at is not None]

    lines.append("")
    lines.append(f"NEW PASSERS: {len(buys)}")
    if not buys:
        lines.append(
            "  Zero passers is the system working, not a failure to find ideas "
            "(Prime Directive 6). No forced deployment."
        )

    for ev in buys:
        lines.append(THIN)
        lines.extend(_candidate_block(ev, config))

    if reviews:
        lines.append(THIN)
        lines.append(f"REVIEW — MIXED VALUATION SIGNAL: {len(reviews)}")
        for ev in reviews:
            lines.append(f"  {ev.symbol}: " + "; ".join(ev.signal.reasons))
            if ev.triangulation:
                lines.append(f"    {ev.triangulation.c1.as_log_line()}")
                lines.append(f"    {ev.triangulation.c2.as_log_line()}")

    if stopped:
        lines.append(THIN)
        lines.append(f"DID NOT REACH A VERDICT: {len(stopped)}")
        for ev in stopped:
            lines.append(f"  {ev.symbol}: stopped at Module {ev.stopped_at}")
            lines.append(f"    {ev.stop_reason}")

    if diagnostics is not None:
        lines.append(THIN)
        lines.append("DIAGNOSTICS — why the screen produced what it produced")
        lines.append(THIN)
        lines.extend(f"  {l}" for l in diagnostics.as_report_lines(config))

    if sensitivity is not None and sensitivity.reports:
        lines.append(THIN)
        lines.extend(f"  {l}" for l in sensitivity.as_report_lines())

    lines.append(RULE)
    lines.append("All output is files plus console. Nothing executes.")
    return "\n".join(lines)



def _boundary_lines(ev: Evaluation, config: Config) -> list[str]:
    """A6 boundary warnings, when the evaluation carries them."""
    proximities = getattr(ev, "boundary_proximities", None)
    if not proximities:
        return []
    from .boundaries import NEAR_MARGIN

    lines = [
        f"  ** NEAR CLASSIFICATION BOUNDARY — {len(proximities)} threshold(s) "
        f"within {NEAR_MARGIN:.0%} **"
    ]
    lines.extend(p.as_report_line() for p in proximities)
    return lines


def _candidate_block(ev: Evaluation, config: Config) -> list[str]:
    """One passer, with every §15 field."""
    lines: list[str] = []
    tags = (
        ", ".join(t.value for t in ev.health.considered_tags)
        if ev.health.considered_tags
        else "none"
    )
    lines.append(f"{ev.symbol} · {ev.classification.value}")
    lines.append(f"  tags considered: {tags}")

    if ev.conviction:
        lines.append(f"  conviction: {ev.conviction.total:.1f} ({ev.conviction.band})")
        for c in ev.conviction.components:
            lines.append(f"    {c.as_log_line()}")

    if ev.fair_value:
        fv = ev.fair_value
        lines.append(f"  fair value: {fv.fair_value_per_share:,.2f} {fv.currency} via {fv.method}")
        if fv.terminal_value_share is not None:
            lines.append(f"    terminal value {fv.terminal_value_share:.0%} of EV")
        if fv.half_growth_fair_value is not None:
            lines.append(
                f"    at half year-1 growth: {fv.half_growth_fair_value:,.2f} "
                f"{fv.currency}"
            )
        for s in fv.scenarios:
            lines.append(
                f"    {s.name} ({s.weight:.0%}): {s.per_share:,.2f} "
                f"[g1={s.growth_rate:.1%}, TV {s.terminal_value_share:.0%}]"
            )

    if ev.dual_currency:
        lines.extend(ev.dual_currency.as_report_lines())

    if ev.triangulation:
        t = ev.triangulation
        lines.append(f"  {t.c1.as_log_line()}")
        lines.append(f"  {t.c2.as_log_line()}")
        lines.append(f"  ANCHOR MODE: {t.mode.value}")
        if t.divergence is not None:
            lines.append(
                f"  divergence: {t.divergence:.1%}"
                + (" — ANCHORS DISAGREE" if t.anchors_disagree else "")
            )
        lines.append(
            f"  PEGY: {t.c3_pegy:.2f}" + (" — FLAG" if t.c3_flag else "")
            if t.c3_pegy is not None
            else "  PEGY n/a"
        )

    if ev.fair_value:
        tam = ev.fair_value.diagnostics.get("tam")
        if tam is not None:
            for line in tam.as_log_lines():
                lines.append(f"  {line}")

    if ev.signal is not None and ev.classification is not None:
        discount = ev.signal.values.get("discount")
        threshold = ev.signal.values.get("threshold")
        if discount is not None and threshold is not None:
            from .boundaries import RoundTripCost

            lines.append(RoundTripCost(discount).as_report_line(threshold))

    boundary = _boundary_lines(ev, config)
    if boundary:
        lines.extend(boundary)

    if ev.sizing:
        lines.append(f"  {ev.sizing.as_report_line()}")

    if ev.yield_assessment:
        lines.append(f"  {ev.yield_assessment.as_report_line()}")

    if ev.adr:
        lines.append(ev.adr.as_report_line())

    if ev.signal:
        for f in ev.signal.flags:
            lines.append(f"  FLAG: {f}")
        if ev.signal.suppressed_until:
            lines.append(
                f"  ALERT SUPPRESSED until {ev.signal.suppressed_until.isoformat()} "
                "— earnings blackout; the name stays on the pass list"
            )
    return lines


def holdings_health_report(
    verdicts: Sequence,
    discipline,
    portfolio: PortfolioState,
    fx: FxTable,
    config: Config,
) -> str:
    """The per-earnings-event holdings report."""
    lines = opening_block(portfolio, fx, config, [])
    lines.append("")
    lines.append("HOLDINGS HEALTH")
    for verdict in verdicts:
        lines.extend(f"  {l}" for l in verdict.as_report_lines())
    lines.append("")
    lines.extend(discipline.as_report_lines())
    lines.append("")
    lines.append(j_tax.trim_note(config))
    lines.append(RULE)
    lines.append("All output is files plus console. Nothing executes.")
    return "\n".join(lines)


def write_report(text: str, directory: Path, name: str) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{date.today().isoformat()}-{name}.txt"
    path.write_text(text)
    return path


__all__ = [
    "opening_block",
    "new_passer_report",
    "holdings_health_report",
    "write_report",
]
