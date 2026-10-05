"""Report renderers.

Every report opens with current sleeve utilisation versus targets, the
index/ballast gap if below target, and the Module I expectation statement
verbatim. All output is files plus console. Nothing executes.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional, Sequence

from ..config import CORE_SLEEVE, GROWTH_SLEEVE, Params
from ..engine import CandidateAssessment, ScreenResult
from ..modules.f_sizing import AllocatorReport, Portfolio, growth_sleeve_breach
from ..modules.g_execution import DisciplineReport
from ..modules.h_monitor import HoldingReview
from ..modules.i_expectations import PerformanceBand, report_header
from ..modules.j_tax import trim_tax_cost_note

RULE = "=" * 78
THIN = "-" * 78


def _pct(value: Optional[float], places: int = 1, sign: bool = False) -> str:
    if value is None:
        return "n/a"
    fmt = f"{{:+.{places}f}}%" if sign else f"{{:.{places}f}}%"
    return fmt.format(value)


def _money(value: Optional[float], currency: str = "USD") -> str:
    if value is None:
        return "n/a"
    return f"{currency} {value:,.0f}"


def _num(value: Optional[float], places: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{places}f}"


def sleeve_block(allocator: Optional[AllocatorReport], portfolio: Optional[Portfolio], params: Params) -> list[str]:
    lines = ["SLEEVE UTILISATION VS TARGETS"]
    if allocator is None or portfolio is None:
        lines.append("  No portfolio supplied -- run with --portfolio to see utilisation and headroom.")
        return lines
    for sleeve in (CORE_SLEEVE, GROWTH_SLEEVE):
        stats = allocator.sleeve_utilisation.get(sleeve, {})
        lines.append(
            f"  {sleeve:<7} {_money(stats.get('value'))} of {_money(stats.get('capacity'))} "
            f"({_pct(stats.get('utilisation_pct'))} used) | "
            f"{stats.get('names', 0)} names, target {stats.get('target_names')} | "
            f"headroom {_money(stats.get('headroom'))}"
        )
        if stats.get("max_pct_of_portfolio") is not None:
            lines.append(
                f"          {_pct(stats.get('pct_of_portfolio'))} of total portfolio against a "
                f"{stats['max_pct_of_portfolio']:.0f}% hard cap"
            )
    lines.append(
        f"  BALLAST {_pct(allocator.ballast_pct)} against a "
        f"{allocator.ballast_target[0]:.0f}-{allocator.ballast_target[1]:.0f}% target"
    )
    breach = growth_sleeve_breach(portfolio, params)
    if breach:
        lines.append(f"  !! {breach}")
    return lines


def _anchor_line(assessment: CandidateAssessment) -> list[str]:
    tri = assessment.triangulation
    if tri is None:
        return ["    anchors: not computed"]
    own, peer = tri.own_history, tri.peer
    lines = [
        f"    C1 own-history  {own.multiple_name:<12} current {_num(own.current_multiple)} vs "
        f"median {_num(own.reference)} -> {_pct(own.implied_upside_pct, 0, sign=True)} "
        f"[z {_num(own.detail.get('z_score'))}, pctile {_num(own.detail.get('percentile'), 0)}, "
        f"{own.confidence}]",
        f"    C2 peers        {peer.multiple_name:<12} current {_num(peer.current_multiple)} vs "
        f"median {_num(peer.reference)} -> {_pct(peer.implied_upside_pct, 0, sign=True)} "
        f"[{peer.detail.get('peer_count', 0)} peers]",
    ]
    if tri.divergence_pp is not None:
        lines.append(f"    C4 divergence   {tri.divergence_pp:.0f}pp -- {tri.combined_verdict}")
    lines.append(f"    C3 {tri.pegy_note}")
    return lines


def weekly_report(
    screen_result: ScreenResult,
    params: Params,
    allocator: Optional[AllocatorReport] = None,
    portfolio: Optional[Portfolio] = None,
    bands: Optional[Sequence[PerformanceBand]] = None,
    theses: Optional[dict[str, str]] = None,
) -> str:
    """Weekly new-passer report (Section 14)."""
    notices = list(screen_result.header_notices)
    if allocator:
        notices = list(allocator.header_notices) + notices

    lines = [RULE, f"GCFP v3 -- WEEKLY NEW-PASSER REPORT -- {screen_result.as_of.isoformat()}", RULE, ""]
    lines += sleeve_block(allocator, portfolio, params)
    lines += ["", report_header(params, bands, notices), "", RULE, ""]

    passers = screen_result.passers()
    deferred = screen_result.qualified_but_deferred()

    if not passers and not deferred:
        lines += [
            "NO PASSERS THIS WEEK.",
            "",
            "Unfilled slots stay in cash. Zero passers in an expensive market is the system",
            "working, not the system failing. No name is admitted to fill a slot.",
            "",
        ]
    else:
        lines.append(f"NEW PASSERS: {len(passers)}")
        lines.append("")

    sized = {d.symbol: d for d in (allocator.decisions if allocator else [])}

    for assessment in passers + deferred:
        val = assessment.valuation
        conv = assessment.conviction
        price = assessment.candidate.profile.price if assessment.candidate else None
        lines.append(THIN)
        lines.append(
            f"{assessment.symbol}  [{assessment.classification}]  "
            f"{assessment.trigger.action if assessment.trigger else ''}"
            + ("  (ALERT DEFERRED)" if assessment.trigger and assessment.trigger.deferred else "")
        )
        if val:
            lines.append(f"    fair value  {_num(val.fair_value_per_share)} via {val.label}")
        lines.append(
            f"    price       {_num(price)}   discount "
            f"{_pct(100.0 * (1 - price / val.fair_value_per_share) if price and val and val.fair_value_per_share else None)}"
        )
        if conv:
            b = conv.breakdown()
            lines.append(
                f"    conviction  {b['total']:.1f} ({conv.band(params)}) = health {b['health_margin']:.1f}"
                f" + valuation {b['valuation_margin']:.1f} + anchors {b['anchor_agreement']:.1f}"
                f" + quality {b['business_quality']:.1f} + momentum {b['momentum']:.1f}"
            )
        lines += _anchor_line(assessment)

        decision = sized.get(assessment.symbol)
        if decision:
            if decision.status == "SIZED":
                lines.append(
                    f"    size        {decision.intended_sleeve_pct:.1f}% of the {decision.sleeve} sleeve "
                    f"= {_money(decision.intended_value)} ({_pct(decision.intended_portfolio_pct, 2)} of portfolio)"
                )
            else:
                lines.append(f"    size        {decision.status} -- {'; '.join(decision.reasons)}")
        if assessment.tax:
            tax = assessment.tax
            if tax.headline_yield_pct is not None:
                lines.append(
                    f"    yield       headline {_pct(tax.headline_yield_pct, 2)} -> after withholding "
                    f"{_pct(tax.after_withholding_yield_pct, 2)}"
                )
            for note in tax.notes:
                lines.append(f"    tax         {note}")
        thesis = (theses or {}).get(assessment.symbol)
        lines.append(f"    what would change my mind: {thesis or 'NOT SET -- required before any execution'}")
        for warning in assessment.all_warnings():
            lines.append(f"    !! {warning}")
        if assessment.trigger and assessment.trigger.deferred:
            lines.append(f"    !! {assessment.trigger.deferral_reason}")
        lines.append("")

    rejected = [a for a in screen_result.assessments if not a.is_buy and a not in deferred]
    if rejected:
        lines += [THIN, "NOT PASSING", ""]
        for a in rejected:
            if a.stopped_at == "A" and a.health:
                reason = "; ".join(a.health.reasons[:2])
            elif a.trigger:
                reason = "; ".join(a.trigger.reasons[:2])
            else:
                reason = "; ".join(a.notes[:2]) or "no verdict"
            lines.append(f"  {a.symbol:<10} {str(a.classification or '-'):<14} {reason}")
        lines.append("")

    lines += [RULE, "ALERT ONLY. This system cannot place an order. A human reads and decides.", RULE]
    return "\n".join(lines)


def quarterly_report(
    reviews: Sequence[HoldingReview],
    discipline: DisciplineReport,
    params: Params,
    as_of: date,
    allocator: Optional[AllocatorReport] = None,
    portfolio: Optional[Portfolio] = None,
    bands: Optional[Sequence[PerformanceBand]] = None,
) -> str:
    """Quarterly holdings health report (Section 14)."""
    notices = list(allocator.header_notices) if allocator else []
    lines = [RULE, f"GCFP v3 -- QUARTERLY HOLDINGS HEALTH REPORT -- {as_of.isoformat()}", RULE, ""]
    lines += sleeve_block(allocator, portfolio, params)
    lines += ["", report_header(params, bands, notices), "", RULE, ""]

    lines.append("POSITIONS")
    lines.append("")
    for review in reviews:
        flag = "/".join(review.flags) if review.flags else "-"
        lines.append(THIN)
        lines.append(
            f"{review.symbol:<10} [{review.classification}]  {review.status}  {flag}"
        )
        if review.conviction_now is not None:
            change = (
                f" ({review.conviction_change:+.1f} since purchase)"
                if review.conviction_change is not None
                else ""
            )
            lines.append(f"    conviction {review.conviction_now:.1f}{change}")
        for reason in review.reasons:
            lines.append(f"    - {reason}")
    lines.append("")

    lines += [THIN, "EXECUTION DISCIPLINE (Module G)", ""]
    lines.append(
        f"  SIZE DEVIATION positions: {discipline.size_deviation_count}"
        + (
            f" | cumulative drift {discipline.cumulative_drift_pct:+.1f}%"
            if discipline.cumulative_drift_pct is not None
            else ""
        )
    )
    for row in discipline.intended_vs_actual:
        actual = _money(row["actual_value"]) if row["actual_value"] is not None else "not executed"
        marker = "  SIZE DEVIATION" if row["size_deviation"] else ""
        lines.append(
            f"    {row['symbol']:<10} intended {_money(row['intended_value'])} -> actual {actual}"
            f" ({_pct(row['deviation_pct'], 1, sign=True)}){marker}"
        )
        if row.get("reason"):
            lines.append(f"               reason: {row['reason']}")
    for flag in discipline.flags:
        lines.append(f"  !! {flag.flag}  {flag.symbol}: {flag.detail}")
    lines.append("")
    lines.append(f"  {trim_tax_cost_note(params)}")
    lines.append("")
    lines += [RULE, "ALERT ONLY. This system cannot place an order. A human reads and decides.", RULE]
    return "\n".join(lines)


def write_report(text: str, directory: str | Path, filename: str) -> Path:
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    target = path / filename
    target.write_text(text, encoding="utf-8")
    return target
