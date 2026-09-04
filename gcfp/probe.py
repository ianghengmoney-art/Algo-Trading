"""§18 — the critical first task.

    "Before writing any strategy code, verify the data source can supply what
    each classification path requires. ... Report all findings before
    proceeding to build the gates."

This module is that verification, made repeatable.  It pulls one company per
classification plus the four special cases §18 names, attempts every Module A
gate input, every Module B input for that company's path, seven years of the
relevant C1 multiple, and a full C2 peer set — then reports what is missing,
stale, or unreliable, and evaluates the four stop conditions.

The stop conditions are the point.  They are not warnings to note and work
around; three of them say to change what gets built, and the fourth says the
dual-anchor premise itself does not hold for this source.  So they are computed
here rather than left to a reader's judgement, and
:meth:`CoverageReport.build_directives` turns them into the concrete
instructions they imply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from .classification import Classification
from .config import Config
from .data.adapter import Capability, DataAdapter, DataUnavailable
from .data.taxonomy import TaxonomyAvailability, assess_taxonomy
from .modules import a_health, c_anchors
from .types import CompanyData, CompanyProfile, MarketData, TaxonomyLevel


@dataclass(frozen=True)
class ProbeTarget:
    """One of the ten companies §18 requires."""

    symbol: str
    role: str
    expected_classification: Classification | None
    tests: str

    @property
    def label(self) -> str:
        return f"{self.symbol} ({self.role})"


#: §18's roster.  Symbols are the default US-listed choices; an operator can
#: substitute their own, but the *roles* are fixed — each exists to exercise a
#: path the others do not reach.
DEFAULT_TARGETS: tuple[ProbeTarget, ...] = (
    ProbeTarget("CAT", "mature industrial", Classification.CORE_STABLE, "B1 two-stage DCF"),
    ProbeTarget("NVDA", "profitable fast-grower", Classification.CORE_GROWTH, "B2 scenario DCF, Rule of 40"),
    ProbeTarget("RIVN", "unprofitable grower", Classification.SPEC_GROWTH, "A3 pre-profit branch, cash burn"),
    ProbeTarget("JPM", "bank", Classification.FINANCIAL_BANK, "B3 P/B and P/TBV, ROE history"),
    ProbeTarget("O", "REIT", Classification.REIT, "B4 P/AFFO, FFO/AFFO availability"),
    ProbeTarget("PGR", "insurer", Classification.INSURER, "B5 combined ratio"),
    ProbeTarget("TSM", "foreign ADR", None, "Module K — underlying currency exposure"),
    ProbeTarget("GE", "spinoff in the last 7 years", None, "C1.1 series discontinuity"),
    ProbeTarget("TPL", "fewer than 4 obvious peers", None, "C5 SINGLE-ANCHOR MODE"),
    ProbeTarget("SIVBQ", "delisted name", None, "survivorship bias / point-in-time"),
)


#: Every input the probe attempts, grouped by the gate that needs it.  A gate
#: whose inputs are missing cannot be enforced, and §18 stop condition 1 is
#: explicit that an unenforceable gate means disabling a path, not building
#: around the gap.
GATE_INPUTS: dict[str, tuple[str, ...]] = {
    "A1 solvency": ("current_ratio", "ttm_operating_cash_flow"),
    "A2 leverage": ("net_debt", "ttm_ebitda", "industry_grouping"),
    "A3 earnings quality": ("ttm_net_income", "ttm_operating_cash_flow", "cash_and_equivalents"),
    "A4 red flags": ("share_count_history", "filing_dates"),
    "A5 data integrity": ("latest_filing_date", "current_price", "market_cap"),
    "A6 classification": ("annual_revenue_3y", "annual_net_income_3y", "free_cash_flow_5y"),
    "B discount rate": ("beta", "interest_expense", "total_debt", "risk_free_rate"),
    "B path-specific": ("path_inputs",),
    "C1 own history": ("multiple_history_7y", "corporate_actions"),
    "C2 peer set": ("peer_candidates", "peer_multiples"),
}


@dataclass
class InputResult:
    name: str
    available: bool
    value: object = None
    detail: str = ""

    def as_report_line(self) -> str:
        mark = "OK " if self.available else "MISSING"
        line = f"      [{mark}] {self.name}"
        if self.available and self.value is not None:
            line += f" = {_fmt(self.value)}"
        if self.detail:
            line += f" — {self.detail}"
        return line


def _fmt(value: object) -> str:
    if isinstance(value, float):
        if abs(value) >= 1e9:
            return f"{value:,.3g}"
        return f"{value:,.4g}"
    return str(value)


@dataclass
class TargetCoverage:
    """What the source could supply for one probe target."""

    target: ProbeTarget
    reached: bool
    profile: CompanyProfile | None = None
    actual_classification: Classification | None = None
    gate_inputs: dict[str, list[InputResult]] = field(default_factory=dict)
    c1_years_available: float | None = None
    #: Set when C1.1 truncated the series at a corporate action.  A short
    #: window for this reason is the gate working, not the source failing.
    c1_discontinuity_date: date | None = None
    c1_raw_observations: int = 0
    #: The span the source supplied, before C1.1 truncation.
    c1_raw_years: float = 0.0
    c2_peer_count: int | None = None
    single_anchor: bool = False
    #: C2's decision for every candidate — the exclusion log the spec requires.
    peer_decisions: list = field(default_factory=list)
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def missing(self) -> list[str]:
        return [
            f"{gate}: {r.name}"
            for gate, results in self.gate_inputs.items()
            for r in results
            if not r.available
        ]

    @property
    def coverage_rate(self) -> float:
        total = sum(len(v) for v in self.gate_inputs.values())
        if not total:
            return 0.0
        have = sum(1 for v in self.gate_inputs.values() for r in v if r.available)
        return have / total

    def as_report_lines(self) -> list[str]:
        lines = [f"  {self.target.label} — tests {self.target.tests}"]
        if not self.reached:
            lines.append(f"    UNREACHABLE: {self.error}")
            return lines
        lines.append(f"    coverage: {self.coverage_rate:.0%} of attempted inputs")
        if self.actual_classification:
            expected = (
                self.target.expected_classification.value
                if self.target.expected_classification
                else "n/a"
            )
            match = (
                "matches"
                if self.target.expected_classification is self.actual_classification
                else f"EXPECTED {expected}"
            )
            lines.append(
                f"    A6 routed to: {self.actual_classification.value} ({match})"
            )
        else:
            lines.append("    A6 routed to: NO TAG — the router could not classify it")
        if self.c1_years_available is not None:
            lines.append(f"    C1 usable history: {self.c1_years_available:.1f} years")
        if self.c2_peer_count is not None:
            lines.append(f"    C2 genuine peers: {self.c2_peer_count}")
            for d in self.peer_decisions:
                verdict = "INCLUDED" if d.included else "excluded"
                lines.append(f"      {d.candidate.symbol}: {verdict} — {d.reason}")
        if self.single_anchor:
            lines.append("    -> SINGLE-ANCHOR MODE")
        for gate, results in self.gate_inputs.items():
            lines.append(f"    {gate}:")
            lines.extend(r.as_report_line() for r in results)
        lines.extend(f"    note: {n}" for n in self.notes)
        return lines


@dataclass
class StopCondition:
    number: int
    name: str
    tripped: bool
    finding: str
    directive: str

    def as_report_lines(self) -> list[str]:
        status = "TRIPPED" if self.tripped else "clear"
        lines = [f"  [{status}] Stop condition {self.number}: {self.name}"]
        lines.append(f"    finding: {self.finding}")
        if self.tripped:
            lines.append(f"    DIRECTIVE: {self.directive}")
        return lines


@dataclass
class CoverageReport:
    """§18's deliverable."""

    generated_on: date
    source_name: str
    capabilities: list[Capability]
    coverages: list[TargetCoverage]
    taxonomy: TaxonomyAvailability
    stop_conditions: list[StopCondition]
    point_in_time_available: bool
    notes: list[str] = field(default_factory=list)

    @property
    def reached(self) -> list[TargetCoverage]:
        return [c for c in self.coverages if c.reached]

    @property
    def single_anchor_rate(self) -> float | None:
        reached = self.reached
        if not reached:
            return None
        return sum(1 for c in reached if c.single_anchor) / len(reached)

    @property
    def any_tripped(self) -> bool:
        return any(s.tripped for s in self.stop_conditions)

    def build_directives(self) -> list[str]:
        """What the findings require to change about the build."""
        out = [s.directive for s in self.stop_conditions if s.tripped]
        if not self.point_in_time_available:
            out.append(
                "Point-in-time constituent and peer data is unavailable. §13.8 "
                "requires this be stated in the report header, not footnoted: "
                "backtest results are inflated by an unknown material amount."
            )
        return out

    def render(self) -> str:
        lines = [
            "=" * 78,
            f"GCFP v4 — §18 DATA FEASIBILITY REPORT",
            f"source: {self.source_name} · generated {self.generated_on.isoformat()}",
            "=" * 78,
            "",
            "ADAPTER CAPABILITIES",
        ]
        for cap in self.capabilities:
            mark = "OK " if cap.supported else "NO "
            lines.append(f"  [{mark}] {cap.name}" + (f" — {cap.detail}" if cap.detail else ""))

        lines.extend(["", "TAXONOMY", f"  {self.taxonomy.as_report_line()}", f"  {self.taxonomy.detail}"])

        lines.extend(["", "PER-TARGET COVERAGE"])
        for coverage in self.coverages:
            lines.extend(coverage.as_report_lines())
            lines.append("")

        lines.append("STOP CONDITIONS")
        for stop in self.stop_conditions:
            lines.extend(stop.as_report_lines())

        rate = self.single_anchor_rate
        lines.extend(
            [
                "",
                "SUMMARY",
                f"  targets reached: {len(self.reached)}/{len(self.coverages)}",
                f"  SINGLE-ANCHOR MODE rate: "
                + (f"{rate:.0%}" if rate is not None else "not measurable"),
                f"  point-in-time data: {'available' if self.point_in_time_available else 'NOT AVAILABLE'}",
            ]
        )

        directives = self.build_directives()
        if directives:
            lines.extend(["", "BUILD DIRECTIVES — these change what gets built:"])
            lines.extend(f"  - {d}" for d in directives)
        else:
            lines.extend(["", "No stop condition tripped. Proceed to build the gates."])

        if self.notes:
            lines.extend(["", "NOTES"])
            lines.extend(f"  {n}" for n in self.notes)

        lines.append("=" * 78)
        return "\n".join(lines)


# -- the probe ------------------------------------------------------------


def _probe_target(
    adapter: DataAdapter,
    target: ProbeTarget,
    market: MarketData,
    config: Config,
) -> TargetCoverage:
    """Attempt every input one target needs."""
    try:
        data = adapter.load_company(target.symbol, multiple=None)
    except DataUnavailable as exc:
        return TargetCoverage(target, False, error=str(exc))
    except Exception as exc:  # provider-specific transport failures
        return TargetCoverage(target, False, error=f"{type(exc).__name__}: {exc}")

    coverage = TargetCoverage(target, True, profile=data.profile)
    coverage.notes.extend(data.source_notes)

    tag, considered, _ = a_health.classify(data, config)
    coverage.actual_classification = tag

    q4 = data.trailing_quarters(4)
    latest_q = data.latest_quarter
    ttm_ocf = a_health._sum(q4, "operating_cash_flow") if len(q4) == 4 else None
    ttm_ni = a_health._sum(q4, "net_income") if len(q4) == 4 else None
    ttm_ebitda = a_health._sum(q4, "ebitda") if len(q4) == 4 else None

    coverage.gate_inputs["A1 solvency"] = [
        InputResult("current_ratio", latest_q is not None and latest_q.current_ratio is not None,
                    latest_q.current_ratio if latest_q else None),
        InputResult("ttm_operating_cash_flow", ttm_ocf is not None, ttm_ocf),
    ]

    grouping = (
        data.profile.gics_sub_industry_code
        or data.profile.sub_industry
        or data.profile.industry
        or data.profile.sector
    )
    coverage.gate_inputs["A2 leverage"] = [
        InputResult("net_debt", latest_q is not None and latest_q.net_debt is not None,
                    latest_q.net_debt if latest_q else None),
        InputResult("ttm_ebitda", ttm_ebitda is not None, ttm_ebitda),
        InputResult(
            "industry_grouping", grouping is not None, grouping,
            "GICS sub-industry" if data.profile.taxonomy_is_gics else "VENDOR SUBSTITUTE",
        ),
    ]

    runway = a_health.cash_runway_months(data)
    coverage.gate_inputs["A3 earnings quality"] = [
        InputResult("ttm_net_income", ttm_ni is not None, ttm_ni),
        InputResult("ttm_operating_cash_flow", ttm_ocf is not None, ttm_ocf),
        InputResult(
            "cash_runway_months",
            runway is not None,
            runway if runway not in (None, float("inf")) else ("not burning" if runway else None),
            "pre-profit branch cannot be enforced without this" if runway is None else "",
        ),
    ]

    share_growth = a_health.share_count_cagr(data, years=2)
    has_filing_dates = any(q.filing_date is not None for q in data.quarterly)
    coverage.gate_inputs["A4 red flags"] = [
        InputResult(
            "share_count_history", share_growth is not None, share_growth,
            "dilution flag cannot be enforced without this" if share_growth is None else "",
        ),
        InputResult("filing_dates", has_filing_dates, None),
    ]

    coverage.gate_inputs["A5 data integrity"] = [
        InputResult("latest_filing_date", latest_q is not None and latest_q.filing_date is not None,
                    latest_q.filing_date if latest_q else None),
        InputResult("current_price", data.current_price is not None, data.current_price),
        InputResult("market_cap", data.profile.market_cap is not None, data.profile.market_cap),
    ]

    annual3 = data.trailing_years(3)
    annual5 = data.trailing_years(5)
    coverage.gate_inputs["A6 classification"] = [
        InputResult("annual_revenue_3y", len(annual3) == 3 and all(a.revenue is not None for a in annual3), len(annual3)),
        InputResult("annual_net_income_3y", len(annual3) == 3 and all(a.net_income is not None for a in annual3), len(annual3)),
        InputResult(
            "free_cash_flow_5y",
            len(annual5) == 5 and all(
                a.free_cash_flow is not None
                or (a.operating_cash_flow is not None and a.capital_expenditure is not None)
                for a in annual5
            ),
            len(annual5),
        ),
    ]

    latest_a = data.latest_annual
    coverage.gate_inputs["B discount rate"] = [
        InputResult("beta", data.profile.beta is not None, data.profile.beta,
                    "1.0 is never assumed; absence is an A5 failure" if data.profile.beta is None else ""),
        InputResult("interest_expense", latest_a is not None and latest_a.interest_expense is not None,
                    latest_a.interest_expense if latest_a else None),
        InputResult("total_debt", latest_a is not None and latest_a.total_debt is not None,
                    latest_a.total_debt if latest_a else None),
        InputResult("risk_free_rate", market.risk_free_rate is not None, market.risk_free_rate),
    ]

    coverage.gate_inputs["B path-specific"] = _path_inputs(data, tag or target.expected_classification)

    # C1: seven years of the relevant multiple.
    multiple_name = (
        c_anchors.ANCHOR_MULTIPLE[tag]
        if tag
        else c_anchors.ANCHOR_MULTIPLE.get(
            target.expected_classification or Classification.CORE_STABLE, "trailing_pe"
        )
    )
    try:
        history = adapter.get_historical_multiples(
            target.symbol, multiple_name, config.anchors.history_window_years
        )
        history_detail = ""
    except DataUnavailable as exc:
        history = ()
        history_detail = str(exc)
    except Exception as exc:
        history = ()
        history_detail = f"{type(exc).__name__}: {exc}"

    try:
        actions = adapter.get_corporate_actions(
            target.symbol, config.anchors.history_window_years
        )
        actions_available = True
    except Exception:
        actions = ()
        actions_available = False

    coverage.c1_raw_observations = len(history)
    if history:
        adjusted = c_anchors.adjust_series(history, actions, config)
        coverage.c1_years_available = adjusted.years_available
        coverage.c1_discontinuity_date = adjusted.discontinuity_date
        raw_span = (
            max(o.observation_date for o in history)
            - min(o.observation_date for o in history)
        ).days / 365.25
        coverage.c1_raw_years = raw_span
    else:
        coverage.c1_years_available = 0.0

    coverage.gate_inputs["C1 own history"] = [
        InputResult(
            f"multiple_history_7y ({multiple_name})",
            coverage.c1_years_available >= config.anchors.min_history_years,
            coverage.c1_years_available,
            history_detail or f"{len(history)} observations",
        ),
        InputResult(
            "corporate_actions", actions_available, len(actions),
            "an empty list must not be read as 'no spinoffs occurred'"
            if actions_available and not actions else "",
        ),
    ]

    # C2: a full peer set.
    try:
        peer_symbols = list(adapter.get_peer_symbols(target.symbol))
        peers_detail = f"{len(peer_symbols)} vendor candidates"
    except Exception as exc:
        peer_symbols = []
        peers_detail = f"{type(exc).__name__}: {exc}"

    # Build each candidate the way C2 will: the same multiple as the subject,
    # computed from the peer's own fundamentals.  Passing nulls in here would
    # reject every peer for "multiple not computable" and report a
    # SINGLE-ANCHOR MODE rate that is an artefact of the probe rather than a
    # property of the data source.
    subject_growth = c_anchors.revenue_growth_yoy(data)
    peer_multiples_available = 0
    unresolved_peers: list[str] = []
    candidates: list[c_anchors.PeerCandidate] = []
    for peer in peer_symbols[:12]:
        try:
            peer_data = adapter.load_company(peer, multiple=None)
        except Exception:
            # A peer the source names but cannot describe is a C2 gap, not a
            # peer that failed the screen.  The two look identical in a bare
            # peer count, so they are separated here.
            unresolved_peers.append(peer)
            continue
        peer_multiple = c_anchors.compute_current_multiple(peer_data, multiple_name)
        if peer_multiple is not None:
            peer_multiples_available += 1
        candidates.append(
            c_anchors.PeerCandidate(
                symbol=peer,
                multiple=peer_multiple,
                market_cap=peer_data.profile.market_cap,
                revenue_growth=c_anchors.revenue_growth_yoy(peer_data),
                group=(
                    peer_data.profile.gics_sub_industry_code
                    or peer_data.profile.sub_industry
                    or peer_data.profile.industry
                ),
            )
        )

    kept, decisions = c_anchors.select_peers(
        grouping, data.profile.market_cap, subject_growth, candidates, config
    )
    coverage.c2_peer_count = len(kept)
    coverage.peer_decisions = decisions

    coverage.gate_inputs["C2 peer set"] = [
        InputResult("peer_candidates", bool(peer_symbols), len(peer_symbols), peers_detail),
        InputResult(
            "peer_fundamentals_resolvable",
            not unresolved_peers,
            len(candidates),
            f"{len(unresolved_peers)} named peers could not be described: "
            f"{unresolved_peers}" if unresolved_peers else "",
        ),
        InputResult(
            "peer_multiples_computable",
            peer_multiples_available > 0,
            peer_multiples_available,
            f"{peer_multiples_available}/{len(candidates)} candidates had a "
            f"computable {multiple_name}",
        ),
        InputResult(
            "peer_set",
            len(kept) >= config.anchors.peer_min,
            len(kept),
            f"{len(kept)} survive C2's screen; {config.anchors.peer_min} required",
        ),
    ]

    c1_ok = coverage.c1_years_available >= config.anchors.min_history_years
    c2_ok = coverage.c2_peer_count >= config.anchors.peer_min
    coverage.single_anchor = (c1_ok != c2_ok)
    if not c1_ok and not c2_ok:
        coverage.notes.append(
            "BOTH ANCHORS UNCOMPUTABLE — C5 makes this an automatic FAIL"
        )

    return coverage


def _path_inputs(
    data: CompanyData, classification: Classification | None
) -> list[InputResult]:
    """Module B inputs specific to the path this company routes to."""
    latest = data.latest_annual
    if classification is Classification.REIT:
        return [
            InputResult("adjusted_funds_from_operations",
                        latest is not None and latest.adjusted_funds_from_operations is not None,
                        latest.adjusted_funds_from_operations if latest else None,
                        "B4 primary measure; P/E is banned on this path"),
            InputResult("funds_from_operations",
                        latest is not None and latest.funds_from_operations is not None,
                        latest.funds_from_operations if latest else None),
        ]
    if classification is Classification.INSURER:
        return [
            InputResult("combined_ratio",
                        latest is not None and latest.combined_ratio is not None,
                        latest.combined_ratio if latest else None,
                        "B5's primary underwriting read"),
        ]
    if classification is Classification.FINANCIAL_BANK:
        return [
            InputResult("tangible_book_value",
                        latest is not None and latest.tangible_book_value is not None,
                        latest.tangible_book_value if latest else None),
            InputResult("total_equity",
                        latest is not None and latest.total_equity is not None,
                        latest.total_equity if latest else None),
        ]
    # B1 / B2 both need cash-flow depth.
    fcf_ok = latest is not None and (
        latest.free_cash_flow is not None
        or (latest.operating_cash_flow is not None and latest.capital_expenditure is not None)
    )
    return [
        InputResult("free_cash_flow", fcf_ok,
                    latest.free_cash_flow if latest else None,
                    "B1/B2 cash-flow depth"),
        InputResult("shares_diluted",
                    latest is not None and latest.shares_diluted is not None,
                    latest.shares_diluted if latest else None),
    ]


def _evaluate_stop_conditions(
    coverages: Sequence[TargetCoverage],
    taxonomy: TaxonomyAvailability,
    config: Config,
    point_in_time: bool,
) -> list[StopCondition]:
    """The four stop conditions, computed rather than left to judgement."""
    reached = [c for c in coverages if c.reached]

    # 1. Cash-burn and share-count history.
    burn_missing = []
    share_missing = []
    for c in reached:
        for r in c.gate_inputs.get("A3 earnings quality", []):
            if r.name == "cash_runway_months" and not r.available:
                burn_missing.append(c.target.symbol)
        for r in c.gate_inputs.get("A4 red flags", []):
            if r.name == "share_count_history" and not r.available:
                share_missing.append(c.target.symbol)
    cond1 = bool(burn_missing or share_missing)
    stop1 = StopCondition(
        1,
        "cash-burn and share-count history computable",
        cond1,
        (
            f"cash runway not computable for {burn_missing or 'none'}; "
            f"share count history not computable for {share_missing or 'none'}"
            if cond1
            else "both computable across all reached targets"
        ),
        "DISABLE SPEC-GROWTH ENTIRELY rather than building around the gap. "
        "Without A3's pre-profit branch and A4's dilution flag, SPEC-GROWTH is "
        "not a strategy, it is a way to buy companies shortly before they run "
        "out of money.",
    )

    # 2. Seven years of historical multiples.  Compared against the same
    # tolerance C1 applies: a series fetched for a 7-year window has its
    # newest and oldest observations inside that window and so can never span
    # quite 7.0 years.  Without the tolerance this condition trips for every
    # source, which would make it carry no information.
    full_window = config.anchors.history_window_years - c_anchors._WINDOW_TOLERANCE_YEARS
    # A window shortened by C1.1's discontinuity handling is the gate working
    # as designed, not the source failing to supply history.  Only the latter
    # is what stop condition 2 asks about.
    source_short = [
        c.target.symbol
        for c in reached
        if c.c1_raw_years < full_window
    ]
    truncated = [
        (c.target.symbol, c.c1_discontinuity_date)
        for c in reached
        if c.c1_discontinuity_date is not None
    ]
    cond2 = bool(source_short)
    finding2 = (
        f"{len(source_short)}/{len(reached)} targets: the source supplied under "
        f"{full_window:.2f} years: {source_short}"
        if cond2
        else f"the source supplied the full window for all {len(reached)} reached targets"
    )
    if truncated:
        finding2 += (
            "; separately, C1.1 truncated "
            + ", ".join(f"{sym} at {d.isoformat()}" for sym, d in truncated)
            + " — that is the gate working, not a sourcing gap"
        )
    stop2 = StopCondition(
        2,
        "seven years of historical multiples sourceable",
        cond2,
        finding2,
        "REPORT BEFORE BUILDING — do not silently shorten the C1 window. C1 "
        "cannot function at full strength and the system degrades toward "
        "single-anchor operation, which C5 penalises but does not make free.",
    )

    # 3. GICS sub-industry codes.
    cond3 = not taxonomy.gics_available
    stop3 = StopCondition(
        3,
        "GICS sub-industry codes available",
        cond3,
        taxonomy.as_report_line(),
        (
            "REPORT WHICH FALLBACK LEVEL IS AVAILABLE BEFORE BUILDING. A2's "
            f"sector-relative leverage and C2's peer matching both run at "
            f"{taxonomy.finest_level_available.value} level against a vendor "
            "taxonomy, which is wider than the spec assumes. Every grouping is "
            "logged VENDOR-SUBSTITUTE, and Module I's peer-gameability break "
            "criterion applies with more force, not less."
            if not taxonomy.blocks_build
            else "A2 and C2 both fail outright — no taxonomy at any level."
        ),
    )

    # 4. SINGLE-ANCHOR MODE rate.
    rate = (
        sum(1 for c in reached if c.single_anchor) / len(reached) if reached else None
    )
    cond4 = rate is not None and rate > config.expectations.single_anchor_rate_break
    stop4 = StopCondition(
        4,
        "SINGLE-ANCHOR MODE below 40% of test companies",
        cond4,
        (
            f"{rate:.0%} of reached targets fall into SINGLE-ANCHOR MODE"
            if rate is not None
            else "not measurable — no targets reached"
        ),
        "THIS IS A DESIGN-LEVEL FINDING, NOT A DATA GAP TO WORK AROUND. The "
        "dual-anchor premise does not hold for this data source and universe. "
        "Report it as such before proceeding.",
    )

    return [stop1, stop2, stop3, stop4]


def run_probe(
    adapter: DataAdapter,
    config: Config,
    targets: Sequence[ProbeTarget] = DEFAULT_TARGETS,
    market: MarketData | None = None,
) -> CoverageReport:
    """Run §18's critical first task against one adapter."""
    if market is None:
        try:
            market = adapter.get_market_data()
        except Exception:
            market = MarketData()

    capabilities = list(adapter.capabilities())
    coverages = [_probe_target(adapter, t, market, config) for t in targets]

    profiles = [c.profile for c in coverages if c.profile is not None]
    taxonomy = assess_taxonomy(profiles)

    point_in_time = adapter.supports("point_in_time")
    stops = _evaluate_stop_conditions(coverages, taxonomy, config, point_in_time)

    notes: list[str] = []
    unreached = [c.target.symbol for c in coverages if not c.reached]
    if unreached:
        notes.append(
            f"Targets the source could not return at all: {unreached}. A probe "
            "that cannot reach a target has not cleared it — treat these as "
            "unknown, not as passing."
        )
    if not point_in_time:
        notes.append(
            "Point-in-time data unavailable. §13.8: using today's peer list to "
            "backtest yesterday's decisions bakes survivorship bias into the "
            "methodology. State this in the report header, do not footnote it."
        )

    return CoverageReport(
        generated_on=date.today(),
        source_name=adapter.name,
        capabilities=capabilities,
        coverages=coverages,
        taxonomy=taxonomy,
        stop_conditions=stops,
        point_in_time_available=point_in_time,
        notes=notes,
    )


__all__ = [
    "ProbeTarget",
    "DEFAULT_TARGETS",
    "TargetCoverage",
    "CoverageReport",
    "StopCondition",
    "InputResult",
    "run_probe",
]
