"""Module A — health gate and classification router.

Prime Directive 2: no stock reaches valuation without clearing this module.
A3 in particular runs *before* any valuation work, because it catches GAAP
profit inflated by unrealised investment gains rather than operations — the
pattern that disqualified three of eleven candidates in live screening.

Every gate here returns its computed value, and A1's and A3's branch, because
"which arm of the rule applied" is itself the audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from ..classification import AMBIGUITY_PRIORITY, Classification, is_growth_routed
from ..config import Config
from ..data.taxonomy import Grouping, resolve_grouping
from ..ledger import (
    AuditLedger,
    GateResult,
    Outcome,
    gate_fail,
    gate_pass,
    gate_uncomputable,
)
from ..types import CompanyData, MarketData, PeriodFinancials, TaxonomyLevel


@dataclass
class HealthAssessment:
    """Module A's full output."""

    symbol: str
    passed: bool
    classification: Classification | None
    gates: list[GateResult] = field(default_factory=list)
    considered_tags: list[Classification] = field(default_factory=list)
    grouping: Grouping | None = None
    data_gaps: list[str] = field(default_factory=list)

    @property
    def failures(self) -> list[GateResult]:
        return [g for g in self.gates if g.failed]

    @property
    def uncomputable(self) -> list[GateResult]:
        return [g for g in self.gates if not g.computable]

    def gate(self, name: str) -> GateResult | None:
        for g in self.gates:
            if g.gate == name:
                return g
        return None


# -- helpers -------------------------------------------------------------


def _sum(rows: Sequence[PeriodFinancials], attr: str) -> float | None:
    """Sum a field across periods, or ``None`` if any period is missing it.

    Partial sums are how a four-quarter test quietly becomes a three-quarter
    test.  Missing means missing.
    """
    total = 0.0
    for row in rows:
        value = getattr(row, attr)
        if value is None:
            return None
        total += value
    return total


def _months_between(earlier: date, later: date) -> float:
    return (later - earlier).days / 30.44


# -- A1 ------------------------------------------------------------------


def gate_a1_solvency(data: CompanyData, config: Config) -> GateResult:
    """Current ratio >= 1.0 OR positive operating cash flow across TTM.

    Structurally negative working capital is a feature in some businesses
    (subscription prepayments, float), never an automatic fail — hence the OR.
    """
    quarters = data.trailing_quarters(4)
    latest = data.latest_quarter

    current_ratio = latest.current_ratio if latest else None
    ttm_ocf = _sum(quarters, "operating_cash_flow") if len(quarters) == 4 else None

    if current_ratio is None and ttm_ocf is None:
        return gate_uncomputable(
            "A1",
            "neither current ratio nor four quarters of operating cash flow available",
        )

    if current_ratio is not None and current_ratio >= config.health.min_current_ratio:
        return gate_pass(
            "A1",
            current_ratio,
            threshold=config.health.min_current_ratio,
            branch="current_ratio",
            detail={"ttm_ocf": ttm_ocf},
        )

    if ttm_ocf is not None and ttm_ocf > 0:
        return gate_pass(
            "A1",
            ttm_ocf,
            threshold=0.0,
            branch="operating_cash_flow",
            detail={"current_ratio": current_ratio},
            reason="current ratio below 1.0 but operations are cash-generative",
        )

    return gate_fail(
        "A1",
        current_ratio if current_ratio is not None else ttm_ocf,
        threshold=config.health.min_current_ratio,
        branch="current_ratio" if current_ratio is not None else "operating_cash_flow",
        reason="neither branch satisfied",
        detail={"current_ratio": current_ratio, "ttm_ocf": ttm_ocf},
    )


# -- A2 ------------------------------------------------------------------


def net_debt_to_ebitda(data: CompanyData) -> float | None:
    """TTM net debt / EBITDA, or ``None`` when either leg is missing."""
    latest = data.latest_quarter
    if latest is None:
        return None
    net_debt = latest.net_debt
    ebitda = _sum(data.trailing_quarters(4), "ebitda")
    if net_debt is None or ebitda is None or ebitda <= 0:
        return None
    return net_debt / ebitda


def gate_a2_leverage(
    data: CompanyData,
    market: MarketData,
    config: Config,
    ledger: AuditLedger | None = None,
) -> tuple[GateResult, Grouping]:
    """Net debt / EBITDA below the grouping median x 1.5.

    Sequencing note (fixes v3 flaw 3): this median comes from the *entire
    universe* grouped by industry code, taken from the data source.  It is
    deliberately not C2's curated 4-8 peer set, which requires size and growth
    matching and cannot exist before classification.  Two different peer
    concepts, two different purposes, computed at two different stages.  Both
    are logged; neither substitutes for the other.
    """
    # A grouping's usable size is how many members had the metric computable,
    # not how many names it nominally contains: a sub-industry with 30 members
    # and 5 computable net-debt/EBITDA figures must escalate.
    counts = dict(market.group_member_counts)
    if not counts:
        # No explicit counts supplied.  Treat a published median as evidence
        # the grouping was large enough to compute one — adapters are expected
        # to withhold medians for groupings they could not populate.
        counts = {
            k: config.health.min_peers_for_subindustry_median
            for k in market.group_net_debt_ebitda_median
        }

    grouping = resolve_grouping(
        data.profile, counts, config.health.min_peers_for_subindustry_median
    )
    if ledger is not None:
        ledger.note(f"A2 universe grouping · {grouping.as_log_line()}")
        if grouping.is_substitute:
            ledger.note(
                "A2 grouping is a VENDOR-SUBSTITUTE for GICS sub-industry; "
                "the median is wider than the spec assumes"
            )

    ratio = net_debt_to_ebitda(data)
    median = market.group_net_debt_ebitda_median.get(grouping.key)

    if ratio is None:
        return (
            gate_uncomputable(
                "A2",
                "net debt or TTM EBITDA not computable",
                detail={"grouping": grouping.key, "level": grouping.level.value},
            ),
            grouping,
        )
    if median is None:
        return (
            gate_uncomputable(
                "A2",
                f"no leverage median for grouping {grouping.key!r} at "
                f"{grouping.level.value} level",
                value=ratio,
            ),
            grouping,
        )
    if not grouping.meets_minimum:
        # Every rung of the ladder was too thin.  Falling back on the median
        # anyway would use exactly the number the escalation existed to avoid.
        return (
            gate_uncomputable(
                "A2",
                f"no grouping reached {config.health.min_peers_for_subindustry_median} "
                f"computable members; finest available was {grouping.key!r} at "
                f"{grouping.level.value} with {grouping.member_count}",
                value=ratio,
                detail={"group": grouping.key, "group_median": median},
            ),
            grouping,
        )

    # A company with net cash has negative net debt and clears on any median.
    threshold = median * config.health.leverage_median_multiple
    outcome = gate_pass if ratio <= threshold else gate_fail
    return (
        outcome(
            "A2",
            ratio,
            threshold=threshold,
            branch=grouping.level.value,
            detail={
                "group": grouping.key,
                "group_median": median,
                "taxonomy": "GICS" if grouping.is_gics else "VENDOR-SUBSTITUTE",
            },
        ),
        grouping,
    )


# -- A3 ------------------------------------------------------------------


def is_profitable_ttm(data: CompanyData) -> bool | None:
    net_income = _sum(data.trailing_quarters(4), "net_income")
    if net_income is None:
        return None
    return net_income > 0


def cash_runway_months(data: CompanyData) -> float | None:
    """Cash divided by TTM burn, in months.  ``None`` if either leg is missing.

    Without this, gate A3's pre-profit branch cannot be enforced — and §18 stop
    condition 1 says that in the absence of reliable burn history, SPEC-GROWTH
    should be disabled entirely rather than built around.
    """
    latest = data.latest_quarter
    if latest is None or latest.cash_and_equivalents is None:
        return None
    ttm_ocf = _sum(data.trailing_quarters(4), "operating_cash_flow")
    if ttm_ocf is None:
        return None
    if ttm_ocf >= 0:
        return float("inf")  # not burning
    monthly_burn = abs(ttm_ocf) / 12.0
    if monthly_burn == 0:
        return float("inf")
    return latest.cash_and_equivalents / monthly_burn


def gate_a3_earnings_quality(data: CompanyData, config: Config) -> GateResult:
    """Profitable: OCF >= 80% of net income over TTM.
    Pre-profit: cash / TTM burn >= 24 months, OR positive OCF.
    """
    quarters = data.trailing_quarters(4)
    if len(quarters) < 4:
        return gate_uncomputable(
            "A3", f"only {len(quarters)} trailing quarters available, need 4"
        )

    profitable = is_profitable_ttm(data)
    if profitable is None:
        return gate_uncomputable("A3", "TTM net income not computable")

    ttm_ocf = _sum(quarters, "operating_cash_flow")
    if ttm_ocf is None:
        return gate_uncomputable("A3", "TTM operating cash flow not computable")

    if profitable:
        net_income = _sum(quarters, "net_income")
        ratio = ttm_ocf / net_income if net_income else None
        if ratio is None:
            return gate_uncomputable("A3", "TTM net income is zero")
        outcome = gate_pass if ratio >= config.health.min_ocf_to_net_income else gate_fail
        return outcome(
            "A3",
            ratio,
            threshold=config.health.min_ocf_to_net_income,
            branch="profitable",
            reason=(
                None
                if ratio >= config.health.min_ocf_to_net_income
                else "GAAP profit not backed by operating cash — the unrealised-gains pattern"
            ),
            detail={"ttm_ocf": ttm_ocf, "ttm_net_income": net_income},
        )

    if ttm_ocf > 0:
        return gate_pass(
            "A3",
            ttm_ocf,
            threshold=0.0,
            branch="pre_profit_positive_ocf",
            detail={"note": "unprofitable on GAAP but cash-generative"},
        )

    runway = cash_runway_months(data)
    if runway is None:
        return gate_uncomputable(
            "A3",
            "pre-profit branch needs cash and burn; one is missing",
            branch="pre_profit_runway",
        )
    outcome = gate_pass if runway >= config.health.min_runway_months else gate_fail
    return outcome(
        "A3",
        runway,
        threshold=config.health.min_runway_months,
        branch="pre_profit_runway",
        detail={"ttm_ocf": ttm_ocf},
    )


# -- A4 ------------------------------------------------------------------


def gate_a4_red_flags(
    data: CompanyData, config: Config, as_of: date | None = None
) -> GateResult:
    """Any one of these disqualifies.  All are checked and all are logged —
    the first hit does not short-circuit, because the count matters to a
    reviewer even when one would have been enough."""
    as_of = as_of or date.today()
    hits: list[str] = []
    checked: list[str] = []

    # Restatement in trailing 3 years.
    checked.append("restatement")
    cutoff = date(as_of.year - config.health.restatement_lookback_years, as_of.month, as_of.day)
    if any(d >= cutoff for d in data.restatements):
        hits.append("restatement within 3 years")

    # Auditor change within 12 months without a stated benign reason.
    checked.append("auditor_change")
    for event in data.auditor_events:
        if not event.changed:
            continue
        age = _months_between(event.event_date, as_of)
        if age <= config.health.auditor_change_lookback_months and not event.benign:
            hits.append(f"auditor change {age:.0f} months ago without benign reason")

    # Going-concern language.
    checked.append("going_concern")
    if data.going_concern_language:
        hits.append("going-concern language")

    # Negative shareholder equity with negative OCF.
    checked.append("negative_equity_with_negative_ocf")
    latest = data.latest_quarter
    ttm_ocf = _sum(data.trailing_quarters(4), "operating_cash_flow")
    if (
        latest is not None
        and latest.total_equity is not None
        and latest.total_equity < 0
        and ttm_ocf is not None
        and ttm_ocf < 0
    ):
        hits.append("negative shareholder equity with negative operating cash flow")

    # Delayed filing.
    checked.append("delayed_filing")
    due = data.profile.next_report_due_date
    if due is not None and data.profile.last_report_date is not None:
        if data.profile.last_report_date < due and (as_of - due).days > config.health.max_filing_delay_days:
            hits.append(f"report {(as_of - due).days} days late")

    # Share count growth > 15%/year over trailing two years.
    checked.append("share_count_growth")
    growth = share_count_cagr(data, years=2)
    if growth is None:
        return gate_uncomputable(
            "A4",
            "share count history unavailable; dilution flag cannot be enforced",
            branch="share_count_growth",
            detail={"other_flags_hit": hits, "checked": checked},
        )
    if growth > config.health.max_share_count_growth:
        hits.append(f"share count +{growth:.1%}/yr")

    if hits:
        return gate_fail(
            "A4",
            float(len(hits)),
            threshold=0.0,
            reason="; ".join(hits),
            detail={"share_count_cagr": growth, "checked": checked},
        )
    return gate_pass(
        "A4",
        0.0,
        threshold=0.0,
        detail={"share_count_cagr": growth, "checked": checked},
    )


def share_count_cagr(data: CompanyData, years: int = 2) -> float | None:
    """Annualised diluted share count growth over ``years``.

    Uses quarterly data so a recent raise shows up without waiting for the
    annual.  Returns ``None`` — never 0.0 — when the history is absent, since
    "no dilution" and "no data" must not look alike to A4.
    """
    quarters = [q for q in data.quarterly if q.shares_diluted]
    needed = 4 * years + 1
    if len(quarters) < needed:
        return None
    newest = quarters[0].shares_diluted
    oldest = quarters[needed - 1].shares_diluted
    if not newest or not oldest or oldest <= 0:
        return None
    return (newest / oldest) ** (1.0 / years) - 1.0


# -- A5 ------------------------------------------------------------------


def gate_a5_data_integrity(
    data: CompanyData,
    config: Config,
    growth_routed: bool,
    as_of: date | None = None,
) -> GateResult:
    """All inputs present and fresh.  Missing or stale = automatic fail,
    logged as a data gap, never imputed."""
    as_of = as_of or date.today()
    limit = (
        config.health.max_data_age_months_growth
        if growth_routed
        else config.health.max_data_age_months
    )

    gaps: list[str] = []
    if not data.quarterly:
        gaps.append("no quarterly financials")
    if not data.annual:
        gaps.append("no annual financials")
    if data.current_price is None:
        gaps.append("no current price")
    if data.profile.market_cap is None:
        gaps.append("no market cap")

    age_months = None
    latest = data.latest_quarter
    if latest is not None:
        reference = latest.filing_date or latest.period_end
        age_months = _months_between(reference, as_of)
        if age_months > limit:
            gaps.append(
                f"latest financials {age_months:.1f} months old (limit {limit})"
            )

    # Source-level capability gaps are not per-company data gaps.  A source
    # with no corporate-action feed lacks it for every company, and failing
    # every name on it would make the system produce nothing while looking
    # like a health verdict.  Those gaps belong in the coverage report (§18)
    # and are surfaced here as flags; only notes about inputs A5 itself
    # requires are failures.
    required_inputs = ("annual", "quarterly", "prices")
    capability_notes: list[str] = []
    for note in data.source_notes:
        if "unavailable" not in note and "error" not in note:
            continue
        if any(note.startswith(kind) for kind in required_inputs):
            gaps.append(note)
        else:
            capability_notes.append(note)

    if gaps:
        return gate_fail(
            "A5",
            age_months,
            threshold=float(limit),
            reason="; ".join(gaps),
            detail={
                "gap_count": len(gaps),
                "growth_routed": growth_routed,
                "source_capability_gaps": capability_notes,
            },
        )
    return gate_pass(
        "A5",
        age_months,
        threshold=float(limit),
        detail={
            "growth_routed": growth_routed,
            "source_capability_gaps": capability_notes,
        },
    )


# -- universe screen -----------------------------------------------------


def gate_universe(
    data: CompanyData, config: Config, growth_routed: bool
) -> GateResult:
    """Market cap and liquidity floor.  Growth-routed names need the higher ADV."""
    profile = data.profile
    min_adv = (
        config.universe.min_adv_usd_growth
        if growth_routed
        else config.universe.min_adv_usd
    )
    if profile.market_cap is None:
        return gate_uncomputable("UNIVERSE", "market cap unavailable")
    if profile.market_cap < config.universe.min_market_cap_usd:
        return gate_fail(
            "UNIVERSE",
            profile.market_cap,
            threshold=config.universe.min_market_cap_usd,
            branch="market_cap",
        )
    if profile.adv_3m_usd is None:
        return gate_uncomputable("UNIVERSE", "3-month ADV unavailable", value=profile.market_cap)
    if profile.adv_3m_usd < min_adv:
        return gate_fail(
            "UNIVERSE",
            profile.adv_3m_usd,
            threshold=min_adv,
            branch="adv",
        )
    return gate_pass(
        "UNIVERSE",
        profile.adv_3m_usd,
        threshold=min_adv,
        branch="adv",
        detail={"market_cap": profile.market_cap},
    )


# -- A6 ------------------------------------------------------------------


def _revenue_growth_yoy(data: CompanyData) -> float | None:
    annual = data.trailing_years(2)
    if len(annual) < 2 or not annual[1].revenue:
        return None
    if annual[0].revenue is None:
        return None
    return annual[0].revenue / annual[1].revenue - 1.0


def _revenue_cagr_2y(data: CompanyData) -> float | None:
    annual = data.trailing_years(3)
    if len(annual) < 3 or not annual[2].revenue or annual[0].revenue is None:
        return None
    return (annual[0].revenue / annual[2].revenue) ** 0.5 - 1.0


def _rule_of_40(data: CompanyData) -> float | None:
    """Revenue growth % + FCF margin %."""
    growth = _revenue_growth_yoy(data)
    latest = data.latest_annual
    if growth is None or latest is None or not latest.revenue:
        return None
    fcf = latest.free_cash_flow
    if fcf is None:
        if latest.operating_cash_flow is None or latest.capital_expenditure is None:
            return None
        # FMP and most vendors report capex as a negative number.
        fcf = latest.operating_cash_flow + latest.capital_expenditure
    return growth * 100.0 + (fcf / latest.revenue) * 100.0


def _profitable_years(data: CompanyData, n: int) -> bool | None:
    rows = data.trailing_years(n)
    if len(rows) < n:
        return None
    if any(r.net_income is None for r in rows):
        return None
    return all(r.net_income > 0 for r in rows)


def _positive_fcf_years(data: CompanyData, of_last: int) -> int | None:
    rows = data.trailing_years(of_last)
    if len(rows) < of_last:
        return None
    count = 0
    for r in rows:
        fcf = r.free_cash_flow
        if fcf is None and r.operating_cash_flow is not None and r.capital_expenditure is not None:
            fcf = r.operating_cash_flow + r.capital_expenditure
        if fcf is None:
            return None
        if fcf > 0:
            count += 1
    return count


def classify(data: CompanyData, config: Config) -> tuple[Classification | None, list[Classification], list[str]]:
    """A6.  Returns (applied tag, tags considered, reasons).

    Every tag the company satisfied is returned, not just the winner, because
    the ambiguity rule is only auditable if you can see what it chose between.
    """
    profile = data.profile
    considered: list[Classification] = []
    reasons: list[str] = []

    if profile.is_reit:
        considered.append(Classification.REIT)
        reasons.append("REIT structure")
    if profile.is_insurer:
        considered.append(Classification.INSURER)
        reasons.append("insurance underwriter")
    if profile.is_bank:
        considered.append(Classification.FINANCIAL_BANK)
        reasons.append("banking or lending institution")

    growth_yoy = _revenue_growth_yoy(data)
    cagr_2y = _revenue_cagr_2y(data)
    rule40 = _rule_of_40(data)
    latest = data.latest_annual
    profitable_3y = _profitable_years(data, 3)
    positive_fcf = _positive_fcf_years(data, 5)
    ttm_profitable = is_profitable_ttm(data)

    revenue = latest.revenue if latest else None
    fcf_negative = False
    if latest is not None:
        fcf = latest.free_cash_flow
        if fcf is None and latest.operating_cash_flow is not None and latest.capital_expenditure is not None:
            fcf = latest.operating_cash_flow + latest.capital_expenditure
        fcf_negative = fcf is not None and fcf < 0

    # SPEC-GROWTH: revenue >= $50M and growing, unprofitable or FCF-negative.
    if (
        revenue is not None
        and revenue >= 50_000_000
        and growth_yoy is not None
        and growth_yoy > 0
        and (ttm_profitable is False or fcf_negative)
    ):
        considered.append(Classification.SPEC_GROWTH)
        reasons.append(
            f"revenue ${revenue/1e6:.0f}M growing {growth_yoy:.1%}, "
            + ("unprofitable" if ttm_profitable is False else "FCF-negative")
        )

    # CORE-GROWTH: profitable, growth >= 20% YoY and >= 15% 2yr CAGR, Rule of 40 >= 40.
    if (
        ttm_profitable
        and growth_yoy is not None
        and growth_yoy >= 0.20
        and cagr_2y is not None
        and cagr_2y >= 0.15
        and rule40 is not None
        and rule40 >= 40.0
    ):
        considered.append(Classification.CORE_GROWTH)
        reasons.append(
            f"profitable, growth {growth_yoy:.1%} YoY / {cagr_2y:.1%} 2yr CAGR, "
            f"Rule of 40 = {rule40:.0f}"
        )

    # CORE-STABLE: profitable >= 3 consecutive years, growth < 20%,
    # positive FCF in >= 4 of last 5 years.
    if (
        profitable_3y
        and growth_yoy is not None
        and growth_yoy < 0.20
        and positive_fcf is not None
        and positive_fcf >= 4
    ):
        considered.append(Classification.CORE_STABLE)
        reasons.append(
            f"profitable 3yr, growth {growth_yoy:.1%}, positive FCF in "
            f"{positive_fcf}/5 years"
        )

    if not considered:
        return None, [], ["satisfied no classification's criteria"]

    for tag in AMBIGUITY_PRIORITY:
        if tag in considered:
            return tag, considered, reasons
    return None, considered, reasons


def gate_a6_classification(
    data: CompanyData, config: Config
) -> tuple[GateResult, Classification | None, list[Classification]]:
    tag, considered, reasons = classify(data, config)
    if tag is None:
        return (
            gate_fail(
                "A6",
                0.0,
                reason="; ".join(reasons),
                detail={"considered": [c.value for c in considered]},
            ),
            None,
            considered,
        )
    return (
        gate_pass(
            "A6",
            float(len(considered)),
            branch=tag.value,
            reason="; ".join(reasons),
            detail={
                "considered": [c.value for c in considered],
                "applied": tag.value,
                "ambiguity_rule_used": len(considered) > 1,
            },
        ),
        tag,
        considered,
    )


# -- orchestration -------------------------------------------------------


def run_module_a(
    data: CompanyData,
    market: MarketData,
    config: Config,
    ledger: AuditLedger,
    as_of: date | None = None,
) -> HealthAssessment:
    """Run the universe screen, A1-A5, and the A6 router.

    Classification is resolved first because A5's freshness limit and the
    universe ADV floor both depend on whether the name is growth-routed — but
    the classification itself is only *reported* as passing once the health
    gates clear, preserving "health before value".
    """
    as_of = as_of or date.today()
    a6, tag, considered = gate_a6_classification(data, config)
    growth_routed = tag is not None and is_growth_routed(tag)

    gates: list[GateResult] = []
    gates.append(gate_universe(data, config, growth_routed))
    a2, grouping = gate_a2_leverage(data, market, config, ledger)
    gates.append(gate_a1_solvency(data, config))
    gates.append(a2)
    gates.append(gate_a3_earnings_quality(data, config))
    gates.append(gate_a4_red_flags(data, config, as_of))
    gates.append(gate_a5_data_integrity(data, config, growth_routed, as_of))
    gates.append(a6)

    ledger.record_all(gates)

    # A5's rule — missing is a fail, never an imputation — applies to every
    # gate: a NOT_COMPUTABLE verdict blocks the name just as a FAIL does, and
    # is reported distinctly so the operator can tell a sick company from an
    # unreadable one.
    blocking = [g for g in gates if g.failed or not g.computable]
    passed = not blocking

    return HealthAssessment(
        symbol=data.profile.symbol,
        passed=passed,
        classification=tag if passed else None,
        gates=gates,
        considered_tags=considered,
        grouping=grouping,
        data_gaps=[g.gate for g in gates if not g.computable],
    )


__all__ = [
    "HealthAssessment",
    "run_module_a",
    "classify",
    "gate_a1_solvency",
    "gate_a2_leverage",
    "gate_a3_earnings_quality",
    "gate_a4_red_flags",
    "gate_a5_data_integrity",
    "gate_a6_classification",
    "gate_universe",
    "net_debt_to_ebitda",
    "cash_runway_months",
    "share_count_cagr",
    "is_profitable_ttm",
]
