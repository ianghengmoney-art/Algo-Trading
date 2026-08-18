"""Section 17 -- the critical first task, as runnable code.

Before any strategy code is trusted, verify the data source can supply what
each classification path requires. This module pulls one company per
classification plus a foreign ADR and a delisted name, attempts every Module A
gate input and every Module B input for that company's path, seven years of the
relevant multiple for C1 and a full peer set for C2, and reports what is
missing, stale or unreliable.

It reports. It never fills a gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional, Sequence

from ..config import (
    CORE_GROWTH,
    CORE_STABLE,
    FINANCIAL_BANK,
    INSURER,
    Params,
    REIT,
    SPEC_GROWTH,
)
from ..modules.a_health import build_trailing_window
from ..modules.b_valuation import compute_affo, compute_ffo, combined_ratio_pct
from ..modules.c_triangulation import MULTIPLE_FOR_CLASSIFICATION
from .base import (
    CapabilityGate,
    CoverageReport,
    DataAdapter,
    DataUnavailable,
    ProbeResult,
)


@dataclass(frozen=True)
class ProbeTarget:
    symbol: str
    path: str
    note: str = ""


DEFAULT_PROBE_TARGETS: tuple[ProbeTarget, ...] = (
    ProbeTarget("CAT", CORE_STABLE, "mature industrial"),
    ProbeTarget("CRWD", CORE_GROWTH, "profitable fast-grower"),
    ProbeTarget("RKLB", SPEC_GROWTH, "unprofitable grower"),
    ProbeTarget("JPM", FINANCIAL_BANK, "bank"),
    ProbeTarget("O", REIT, "REIT"),
    ProbeTarget("TRV", INSURER, "insurer"),
    ProbeTarget("TSM", CORE_STABLE, "foreign ADR"),
    ProbeTarget("SIVBQ", CORE_STABLE, "delisted name"),
)

FIXTURE_PROBE_TARGETS: tuple[ProbeTarget, ...] = (
    ProbeTarget("MATURE", CORE_STABLE, "mature industrial"),
    ProbeTarget("FASTPROF", CORE_GROWTH, "profitable fast-grower"),
    ProbeTarget("BURNER", SPEC_GROWTH, "unprofitable grower"),
    ProbeTarget("BANKCO", FINANCIAL_BANK, "bank"),
    ProbeTarget("REITCO", REIT, "REIT"),
    ProbeTarget("INSURCO", INSURER, "insurer"),
    ProbeTarget("ADRCO", CORE_STABLE, "foreign ADR"),
)

# (requirement, module, accessor) for the inputs every path needs.
MODULE_A_REQUIREMENTS = (
    ("current ratio", "A1", lambda w, c: w and w.latest("current_assets") is not None and w.latest("current_liabilities") is not None),
    ("trailing 4Q operating cash flow", "A1/A3", lambda w, c: w and w.total("operating_cash_flow") is not None),
    ("net debt", "A2", lambda w, c: w and w.periods and w.periods[0].net_debt is not None),
    ("trailing EBITDA", "A2", lambda w, c: w and w.total("ebitda") is not None),
    ("sector leverage aggregates", "A2", lambda w, c: len(c.sector_net_debt_ebitda) >= 4),
    ("trailing net income", "A3", lambda w, c: w and w.total("net_income") is not None),
    ("cash and short-term investments", "A3", lambda w, c: w and w.latest("cash_and_equivalents") is not None),
    ("capital expenditure (cash burn)", "A3", lambda w, c: w and w.total("capital_expenditure") is not None),
    ("share count history (2y)", "A4", lambda w, c: len([f for f in c.annual if f.shares_diluted]) >= 3),
    ("restatement / auditor / going-concern / late-filing flags", "A4", lambda w, c: c.filing_flags.going_concern_language is not None),
    ("period dating for staleness", "A5", lambda w, c: bool(w and w.period_end)),
    ("price", "A5", lambda w, c: c.profile.price is not None),
    ("market cap", "UNIVERSE", lambda w, c: c.profile.market_cap is not None),
    ("average daily dollar volume", "UNIVERSE", lambda w, c: c.profile.avg_daily_dollar_volume is not None),
    ("annual revenue history (3y+)", "A6", lambda w, c: len([f for f in c.annual if f.revenue is not None]) >= 3),
)

MODULE_B_REQUIREMENTS = {
    CORE_STABLE: (
        ("trailing free cash flow", lambda w, c: w and (w.total("free_cash_flow") is not None or (w.total("operating_cash_flow") is not None and w.total("capital_expenditure") is not None))),
        ("5y FCF history for the growth cap", lambda w, c: len([f for f in c.annual if f.computed_free_cash_flow is not None]) >= 6),
        ("beta for CAPM", lambda w, c: c.profile.beta is not None),
        ("diluted share count", lambda w, c: w and (w.latest("shares_diluted") or c.profile.shares_outstanding)),
    ),
    CORE_GROWTH: (
        ("trailing revenue", lambda w, c: w and w.total("revenue") is not None),
        ("3y revenue history for the 2y CAGR", lambda w, c: len([f for f in c.annual if f.revenue is not None]) >= 3),
        ("beta for CAPM", lambda w, c: c.profile.beta is not None),
        ("diluted share count", lambda w, c: w and (w.latest("shares_diluted") or c.profile.shares_outstanding)),
    ),
    FINANCIAL_BANK: (
        ("tangible book value", lambda w, c: w and w.periods and w.periods[0].tangible_book_value is not None),
        ("10y ROE history", lambda w, c: len([f for f in c.annual if f.net_income is not None and f.total_equity]) >= 10),
    ),
    REIT: (
        ("FFO (or the inputs to compute it)", lambda w, c: w and w.periods and compute_ffo(w.periods[0]) is not None),
        ("AFFO (recurring capex + straight-line rent)", lambda w, c: w and w.periods and compute_affo(w.periods[0]) is not None),
    ),
    INSURER: (
        ("combined ratio (losses+LAE, underwriting expense, earned premium)", lambda w, c: w and w.periods and combined_ratio_pct(w.periods[0]) is not None),
        ("unrealised investment gains, separable", lambda w, c: w and w.periods and w.periods[0].unrealised_investment_gains is not None),
        ("book value", lambda w, c: w and w.latest("total_equity") is not None),
    ),
}
MODULE_B_REQUIREMENTS[SPEC_GROWTH] = MODULE_B_REQUIREMENTS[CORE_GROWTH]


def _safe(fn, window, candidate) -> bool:
    try:
        return bool(fn(window, candidate))
    except Exception:
        return False


def run_coverage_probe(
    adapter: DataAdapter,
    params: Params,
    targets: Sequence[ProbeTarget] = DEFAULT_PROBE_TARGETS,
    as_of: Optional[date] = None,
) -> CoverageReport:
    """Attempt every required input for every classification path."""
    as_of = as_of or date.today()
    report = CoverageReport(adapter_name=adapter.name, generated_on=as_of)
    observed_multiple_years: list[float] = []

    for target in targets:
        try:
            candidate = adapter.load_candidate(target.symbol, as_of=as_of)
        except DataUnavailable as exc:
            report.add(
                ProbeResult(
                    target.symbol, target.path, "company loadable", "LOAD", False, str(exc)
                )
            )
            continue

        window = build_trailing_window(candidate)
        report.add(
            ProbeResult(
                target.symbol, target.path, "company loadable", "LOAD", True,
                target.note, candidate.profile.name,
            )
        )

        for requirement, module, accessor in MODULE_A_REQUIREMENTS:
            report.add(
                ProbeResult(
                    target.symbol, target.path, requirement, module,
                    _safe(accessor, window, candidate),
                )
            )

        for requirement, accessor in MODULE_B_REQUIREMENTS.get(target.path, ()):
            report.add(
                ProbeResult(
                    target.symbol, target.path, requirement, "B", _safe(accessor, window, candidate)
                )
            )

        # C1: seven years of the classification-appropriate multiple.
        multiple_name = MULTIPLE_FOR_CLASSIFICATION.get(target.path, "multiple")
        years = 0.0
        detail = ""
        try:
            series = adapter.get_multiple_series(target.symbol, multiple_name, years=10)
            years = series.years_covered
            observed_multiple_years.append(years)
            detail = f"{years:.1f}y, {len(series.points)} observations"
        except DataUnavailable as exc:
            detail = str(exc)
        report.add(
            ProbeResult(
                target.symbol, target.path,
                f"7y history of {multiple_name}", "C1",
                years >= params.triangulation.c1_min_window_years, detail,
                f"{years:.1f}y" if years else None,
            )
        )

        # C2: a full peer set.
        peers = list(candidate.peer_symbols)
        report.add(
            ProbeResult(
                target.symbol, target.path, "peer set (4-8 genuine peers)", "C2",
                len(peers) >= params.triangulation.c2_min_peers,
                f"{len(peers)} peers offered by the source",
                str(len(peers)),
            )
        )

        # Staleness.
        if window and window.period_end:
            age_months = (as_of - window.period_end).days / 30.4375
            fresh_limit = (
                params.health.max_data_age_months_growth
                if target.path in (CORE_GROWTH, SPEC_GROWTH)
                else params.health.max_data_age_months
            )
            report.add(
                ProbeResult(
                    target.symbol, target.path, "data freshness", "A5",
                    age_months <= fresh_limit,
                    f"{age_months:.1f} months old against a {fresh_limit:.0f}-month limit",
                    f"{age_months:.1f}m",
                )
            )

    report.capability_gate = CapabilityGate(
        adapter_name=adapter.name,
        capabilities=adapter.capabilities(),
        multiple_history_years=min(observed_multiple_years) if observed_multiple_years else None,
        required_multiple_history_years=float(params.triangulation.c1_min_window_years),
    )
    report.notes.extend(report.capability_gate.blocking_notices())
    return report


def render_coverage_report(report: CoverageReport) -> str:
    """Human-readable coverage report, stop conditions first."""
    lines = [
        "=" * 78,
        f"GCFP v3 -- DATA COVERAGE REPORT ({report.adapter_name}) -- {report.generated_on.isoformat()}",
        "=" * 78,
        "",
    ]

    if report.notes:
        lines.append("STOP CONDITIONS AND BLOCKING NOTICES")
        for note in report.notes:
            lines.append(f"  !! {note}")
        lines.append("")

    lines.append(f"OVERALL COVERAGE: {report.coverage_pct():.0f}% of probed requirements available")
    lines.append("")

    grouped = report.by_module()
    lines.append("BY MODULE")
    for module in sorted(grouped):
        results = grouped[module]
        available = sum(1 for r in results if r.available)
        lines.append(f"  {module:<10} {available}/{len(results)} available")
    lines.append("")

    missing = report.missing()
    if missing:
        lines.append("MISSING OR UNRELIABLE")
        for r in missing:
            detail = f" -- {r.detail}" if r.detail else ""
            lines.append(f"  {r.symbol:<8} [{r.classification_path:<12}] {r.module:<8} {r.requirement}{detail}")
        lines.append("")

    lines += [
        "POINT-IN-TIME LIMITATIONS",
        "  Peer sets and index membership are current-only unless the adapter advertises",
        "  Capability.POINT_IN_TIME. Where it does not, any backtest built on this source",
        "  is inflated by an unknown but material amount and must say so in its header,",
        "  not in a footnote.",
        "",
        "=" * 78,
    ]
    return "\n".join(lines)
