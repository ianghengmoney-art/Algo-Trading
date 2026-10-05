"""Module A -- health gate and classification router.

Runs first on every candidate. Nothing reaches the valuation layer without
clearing it: a cheap unhealthy business is a value trap, not a signal.

Ordering note. The spec lists A6 last, but A5's staleness rule and the universe
liquidity floor are both *path-dependent* (6 months and USD 5M ADV for Growth
names, 12 months and USD 2M otherwise). The router therefore runs twice: a
provisional pass to learn which path the candidate is on, then the gates using
that path's thresholds, then a final confirmation that the tag did not change.
The provisional tag is never used for anything except selecting thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
from ..data.base import CapabilityGate
from ..types import (
    CandidateData,
    Financials,
    GateOutcome,
    PeriodType,
    Verdict,
)

MONTHS_PER_YEAR = 12.0


# ---------------------------------------------------------------------------
# trailing-period helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrailingWindow:
    """A trailing-twelve-month view and the basis it was built from.

    ``basis`` is reported with every gate that uses it. A fiscal year is
    literally four quarters, so an annual fallback is real reported data rather
    than an imputation -- but the reader must be able to see which one answered
    the question, because the annual basis can be up to twelve months stale
    where the quarterly basis cannot.
    """

    basis: str
    periods: Sequence[Financials]
    period_end: Optional[date]

    def total(self, attribute: str) -> Optional[float]:
        values = [getattr(p, attribute, None) for p in self.periods]
        if any(v is None for v in values) or not values:
            return None
        return float(sum(values))

    def latest(self, attribute: str) -> Optional[float]:
        if not self.periods:
            return None
        return getattr(self.periods[0], attribute, None)


def build_trailing_window(candidate: CandidateData) -> Optional[TrailingWindow]:
    """Trailing four quarters, or the latest fiscal year if quarters are absent."""
    quarterly = [f for f in candidate.quarterly if f.period_type is PeriodType.QUARTER]
    quarterly = sorted(quarterly, key=lambda f: f.period_end, reverse=True)
    if len(quarterly) >= 4:
        window = quarterly[:4]
        return TrailingWindow("trailing-4-quarters", window, window[0].period_end)

    annual = [f for f in candidate.annual if f.period_type is PeriodType.ANNUAL]
    annual = sorted(annual, key=lambda f: f.period_end, reverse=True)
    if annual:
        return TrailingWindow("latest-fiscal-year", annual[:1], annual[0].period_end)
    return None


def sorted_annuals(candidate: CandidateData) -> list[Financials]:
    return sorted(
        [f for f in candidate.annual if f.period_type is PeriodType.ANNUAL],
        key=lambda f: f.period_end,
        reverse=True,
    )


def _months_between(earlier: date, later: date) -> float:
    return (later - earlier).days / 30.4375


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass
class HealthResult:
    symbol: str
    verdict: Verdict
    classification: Optional[str]
    gates: list[GateOutcome] = field(default_factory=list)
    a1_branch: Optional[str] = None
    trailing_basis: Optional[str] = None
    reasons: list[str] = field(default_factory=list)
    classification_detail: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.verdict is Verdict.PASS

    def gate(self, name: str) -> Optional[GateOutcome]:
        for g in self.gates:
            if g.gate == name:
                return g
        return None

    def margins(self) -> dict:
        """Per-gate headroom above the threshold, for Module D's health score."""
        out: dict = {}
        for g in self.gates:
            if "margin" in g.detail:
                out[g.gate] = g.detail["margin"]
        return out


# ---------------------------------------------------------------------------
# A6 -- classification router
# ---------------------------------------------------------------------------


def _revenue_growth_pct(annuals: Sequence[Financials]) -> Optional[float]:
    if len(annuals) < 2:
        return None
    latest, prior = annuals[0].revenue, annuals[1].revenue
    if latest is None or prior is None or prior <= 0:
        return None
    return 100.0 * (latest / prior - 1.0)


def _revenue_cagr_pct(annuals: Sequence[Financials], years: int) -> Optional[float]:
    if len(annuals) < years + 1:
        return None
    latest, base = annuals[0].revenue, annuals[years].revenue
    if latest is None or base is None or base <= 0 or latest <= 0:
        return None
    return 100.0 * ((latest / base) ** (1.0 / years) - 1.0)


def _fcf_margin_pct(period: Financials) -> Optional[float]:
    fcf = period.computed_free_cash_flow
    if fcf is None or period.revenue is None or period.revenue <= 0:
        return None
    return 100.0 * fcf / period.revenue


def rule_of_40(annuals: Sequence[Financials]) -> Optional[float]:
    """Revenue growth % + FCF margin %.

    FCF margin rather than operating margin: for the companies this tag is
    meant to catch, the difference between the two is capitalised spend, and
    the cash version is the one that cannot be dressed up.
    """
    growth = _revenue_growth_pct(annuals)
    if growth is None or not annuals:
        return None
    margin = _fcf_margin_pct(annuals[0])
    if margin is None:
        return None
    return growth + margin


def classify(
    candidate: CandidateData,
    params: Params,
    capability_gate: Optional[CapabilityGate] = None,
) -> tuple[Optional[str], dict]:
    """Assign exactly one tag, or none.

    Returning ``None`` is a legitimate and important outcome. Prime Directive 3
    makes forcing a tag a first-order modelling error, so a company that fits no
    row of the A6 table is rejected rather than routed to the nearest method.
    """
    cp = params.classification
    profile = candidate.profile
    annuals = sorted_annuals(candidate)
    detail: dict = {"basis_years": len(annuals)}

    # Structural tags take precedence: a bank that happens to be growing 25% is
    # still valued as a bank.
    if profile.is_bank:
        return FINANCIAL_BANK, {**detail, "route": "structural: banking/lending institution"}
    if profile.is_reit:
        return REIT, {**detail, "route": "structural: REIT structure"}
    if profile.is_insurer:
        return INSURER, {**detail, "route": "structural: insurance underwriter"}

    if not annuals:
        return None, {**detail, "route": "no annual history -- cannot classify"}

    growth = _revenue_growth_pct(annuals)
    cagr_2y = _revenue_cagr_pct(annuals, 2)
    r40 = rule_of_40(annuals)
    latest = annuals[0]

    profitable_years = 0
    for period in annuals:
        if period.net_income is not None and period.net_income > 0:
            profitable_years += 1
        else:
            break

    fcf_window = annuals[: cp.core_stable_fcf_window_years]
    positive_fcf_years = sum(
        1 for p in fcf_window if (p.computed_free_cash_flow or 0.0) > 0 and p.computed_free_cash_flow is not None
    )

    is_profitable = latest.net_income is not None and latest.net_income > 0
    latest_fcf = latest.computed_free_cash_flow

    detail.update(
        {
            "revenue_growth_pct": growth,
            "revenue_2y_cagr_pct": cagr_2y,
            "rule_of_40": r40,
            "consecutive_profitable_years": profitable_years,
            "positive_fcf_years_in_window": positive_fcf_years,
            "latest_net_income": latest.net_income,
            "latest_fcf": latest_fcf,
            "latest_revenue": latest.revenue,
        }
    )

    # CORE-STABLE.
    if (
        profitable_years >= cp.core_stable_min_profitable_years
        and growth is not None
        and growth < cp.core_stable_max_revenue_growth_pct
        and positive_fcf_years >= cp.core_stable_min_positive_fcf_years
    ):
        return CORE_STABLE, {**detail, "route": "profitable, sub-20% growth, durable FCF"}

    # CORE-GROWTH.
    if (
        is_profitable
        and growth is not None
        and growth >= cp.core_growth_min_revenue_growth_pct
        and cagr_2y is not None
        and cagr_2y >= cp.core_growth_min_2y_cagr_pct
        and r40 is not None
        and r40 >= cp.core_growth_min_rule_of_40
    ):
        return CORE_GROWTH, {**detail, "route": "profitable fast-grower clearing Rule of 40"}

    # SPEC-GROWTH.
    fcf_negative = latest_fcf is not None and latest_fcf < 0
    if (
        latest.revenue is not None
        and latest.revenue >= cp.spec_growth_min_revenue_usd
        and growth is not None
        and growth > 0
        and (not is_profitable or fcf_negative)
    ):
        if capability_gate is not None and not capability_gate.spec_growth_enabled:
            return None, {
                **detail,
                "route": "SPEC-GROWTH matched but disabled by the data-capability gate "
                "(cash-burn / share-count history unavailable)",
                "spec_growth_disabled": True,
            }
        return SPEC_GROWTH, {**detail, "route": "revenue scale and growth, but unprofitable or FCF-negative"}

    return None, {
        **detail,
        "route": "no A6 row matched -- refusing to force a tag (Prime Directive 3)",
    }


# ---------------------------------------------------------------------------
# gates A1-A5
# ---------------------------------------------------------------------------


def gate_universe(candidate: CandidateData, params: Params, classification: Optional[str]) -> GateOutcome:
    up = params.universe
    profile = candidate.profile
    is_growth = classification in (CORE_GROWTH, SPEC_GROWTH)
    min_adv = up.min_adv_usd_growth if is_growth else up.min_adv_usd

    detail = {
        "market_cap": profile.market_cap,
        "min_market_cap": up.min_market_cap_usd,
        "avg_daily_dollar_volume": profile.avg_daily_dollar_volume,
        "min_avg_daily_dollar_volume": min_adv,
        "liquidity_floor_basis": "growth" if is_growth else "standard",
        "is_etf": profile.is_etf,
        "is_fund": profile.is_fund,
    }

    if profile.market_cap is None or profile.avg_daily_dollar_volume is None:
        return GateOutcome("UNIVERSE", Verdict.DATA_GAP, "market cap or ADV missing", detail)
    if profile.is_etf or profile.is_fund:
        return GateOutcome("UNIVERSE", Verdict.FAIL, "not common stock", detail)
    if profile.is_actively_trading is False:
        return GateOutcome("UNIVERSE", Verdict.FAIL, "not actively trading", detail)
    if profile.market_cap < up.min_market_cap_usd:
        return GateOutcome(
            "UNIVERSE", Verdict.FAIL,
            f"market cap {profile.market_cap:,.0f} below {up.min_market_cap_usd:,.0f}", detail,
        )
    if profile.avg_daily_dollar_volume < min_adv:
        return GateOutcome(
            "UNIVERSE", Verdict.FAIL,
            f"ADV {profile.avg_daily_dollar_volume:,.0f} below {min_adv:,.0f}", detail,
        )
    return GateOutcome("UNIVERSE", Verdict.PASS, "in universe", detail)


def gate_a1_solvency(window: Optional[TrailingWindow], params: Params) -> GateOutcome:
    """Current ratio >= 1.0 OR demonstrated positive trailing OCF.

    The OR branch exists because structurally negative working capital is a
    feature in some businesses -- Apple collects from customers faster than it
    pays suppliers. Which branch answered is always recorded; negative net
    working capital is never an automatic failure.
    """
    if window is None:
        return GateOutcome("A1", Verdict.DATA_GAP, "no trailing financials", {})

    current_assets = window.latest("current_assets")
    current_liabilities = window.latest("current_liabilities")
    ocf = window.total("operating_cash_flow")

    current_ratio = None
    if current_assets is not None and current_liabilities not in (None, 0):
        current_ratio = current_assets / current_liabilities

    detail = {
        "current_ratio": current_ratio,
        "min_current_ratio": params.health.min_current_ratio,
        "trailing_operating_cash_flow": ocf,
        "basis": window.basis,
    }

    if current_ratio is None and ocf is None:
        return GateOutcome("A1", Verdict.DATA_GAP, "neither current ratio nor OCF computable", detail)

    if current_ratio is not None and current_ratio >= params.health.min_current_ratio:
        detail["branch"] = "current_ratio"
        detail["margin"] = current_ratio / params.health.min_current_ratio - 1.0
        return GateOutcome("A1", Verdict.PASS, f"current ratio {current_ratio:.2f}", detail)

    if ocf is not None and ocf > 0:
        detail["branch"] = "positive_operating_cash_flow"
        # Headroom on this branch is unbounded above; express it as OCF against
        # the working-capital shortfall it has to fund.
        shortfall = None
        if current_assets is not None and current_liabilities is not None:
            shortfall = max(0.0, current_liabilities - current_assets)
        detail["working_capital_shortfall"] = shortfall
        detail["margin"] = 1.0 if not shortfall else min(2.0, ocf / shortfall)
        return GateOutcome(
            "A1", Verdict.PASS,
            f"negative working capital offset by trailing OCF {ocf:,.0f}", detail,
        )

    detail["branch"] = "none"
    return GateOutcome("A1", Verdict.FAIL, "current ratio below 1.0 and trailing OCF not positive", detail)


def gate_a2_leverage(
    candidate: CandidateData, window: Optional[TrailingWindow], params: Params
) -> GateOutcome:
    """Net debt / EBITDA below sector median x 1.5.

    A universal fixed ceiling misclassifies utilities, REITs and capital-
    intensive industrials, where high leverage is normal operating practice.
    """
    hp = params.health
    if window is None:
        return GateOutcome("A2", Verdict.DATA_GAP, "no trailing financials", {})

    net_debt = window.periods[0].net_debt if window.periods else None
    ebitda = window.total("ebitda")
    peers = [v for v in candidate.sector_net_debt_ebitda if v is not None]

    detail = {
        "net_debt": net_debt,
        "trailing_ebitda": ebitda,
        "sector_peer_count": len(peers),
        "sector": candidate.profile.sector,
        "basis": window.basis,
    }

    if net_debt is None:
        return GateOutcome("A2", Verdict.DATA_GAP, "net debt not computable", detail)

    if net_debt <= 0:
        detail["ratio"] = 0.0
        detail["margin"] = 1.0
        return GateOutcome("A2", Verdict.PASS, "net cash position", detail)

    if ebitda is None:
        return GateOutcome("A2", Verdict.DATA_GAP, "EBITDA not available", detail)
    if ebitda <= 0:
        detail["ratio"] = None
        return GateOutcome(
            "A2", Verdict.FAIL, "net debt against non-positive EBITDA", detail
        )

    ratio = net_debt / ebitda
    detail["ratio"] = ratio

    if len(peers) < hp.min_sector_peers_for_median:
        return GateOutcome(
            "A2", Verdict.DATA_GAP,
            f"only {len(peers)} sector observations, {hp.min_sector_peers_for_median} required "
            "for a median -- not substituting a universal ceiling",
            detail,
        )

    ordered = sorted(peers)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else 0.5 * (ordered[mid - 1] + ordered[mid])
    ceiling = median * hp.net_debt_ebitda_sector_multiple
    detail["sector_median"] = median
    detail["ceiling"] = ceiling

    if ceiling <= 0:
        return GateOutcome(
            "A2", Verdict.DATA_GAP, "sector median non-positive -- ceiling undefined", detail
        )

    detail["margin"] = max(0.0, 1.0 - ratio / ceiling)
    if ratio < ceiling:
        return GateOutcome(
            "A2", Verdict.PASS, f"net debt/EBITDA {ratio:.2f} below sector ceiling {ceiling:.2f}", detail
        )
    return GateOutcome(
        "A2", Verdict.FAIL, f"net debt/EBITDA {ratio:.2f} at or above sector ceiling {ceiling:.2f}", detail
    )


def gate_a3_earnings_quality(
    window: Optional[TrailingWindow], params: Params, capability_gate: Optional[CapabilityGate]
) -> GateOutcome:
    """Cash-backed earnings, or a survivable runway for the pre-profit branch.

    This is the gate that catches GAAP profit inflated by unrealised investment
    gains rather than operations. It runs before any valuation work, never
    after.
    """
    hp = params.health
    if window is None:
        return GateOutcome("A3", Verdict.DATA_GAP, "no trailing financials", {})

    net_income = window.total("net_income")
    ocf = window.total("operating_cash_flow")
    capex = window.total("capital_expenditure")
    cash = window.latest("cash_and_equivalents")
    short_term = window.latest("short_term_investments") or 0.0

    detail = {
        "trailing_net_income": net_income,
        "trailing_operating_cash_flow": ocf,
        "required_ocf_ratio": hp.min_ocf_to_net_income,
        "basis": window.basis,
    }

    if net_income is None or ocf is None:
        return GateOutcome("A3", Verdict.DATA_GAP, "net income or OCF unavailable", detail)

    if net_income > 0:
        detail["branch"] = "profitable"
        ratio = ocf / net_income
        detail["ocf_to_net_income"] = ratio
        detail["margin"] = max(0.0, ratio / hp.min_ocf_to_net_income - 1.0)
        if ratio >= hp.min_ocf_to_net_income:
            return GateOutcome("A3", Verdict.PASS, f"OCF/NI {ratio:.2f}", detail)
        return GateOutcome(
            "A3", Verdict.FAIL,
            f"OCF/NI {ratio:.2f} below {hp.min_ocf_to_net_income:.2f} -- earnings not cash-backed",
            detail,
        )

    # Pre-profit branch.
    detail["branch"] = "pre_profit"
    if capability_gate is not None and not capability_gate.spec_growth_enabled:
        return GateOutcome(
            "A3", Verdict.DATA_GAP,
            "pre-profit branch requires cash-burn history the data source cannot supply",
            detail,
        )
    if ocf > 0:
        detail["margin"] = 1.0
        return GateOutcome("A3", Verdict.PASS, "pre-profit but operating cash flow positive", detail)

    if cash is None or capex is None:
        return GateOutcome("A3", Verdict.DATA_GAP, "cash or capex unavailable for runway", detail)

    liquid = cash + short_term
    burn = -(ocf - abs(capex))
    detail["liquid_assets"] = liquid
    detail["trailing_fcf_burn"] = burn
    if burn <= 0:
        detail["margin"] = 1.0
        return GateOutcome("A3", Verdict.PASS, "no trailing free-cash burn", detail)

    runway_months = MONTHS_PER_YEAR * liquid / burn
    detail["runway_months"] = runway_months
    detail["required_runway_months"] = hp.min_cash_runway_months
    detail["margin"] = max(0.0, runway_months / hp.min_cash_runway_months - 1.0)
    if runway_months >= hp.min_cash_runway_months:
        return GateOutcome("A3", Verdict.PASS, f"cash runway {runway_months:.0f} months", detail)
    return GateOutcome(
        "A3", Verdict.FAIL,
        f"cash runway {runway_months:.0f} months below {hp.min_cash_runway_months:.0f}", detail,
    )


def share_count_growth_pct_per_year(annuals: Sequence[Financials], years: int) -> Optional[float]:
    if len(annuals) < years + 1:
        return None
    latest = annuals[0].shares_diluted
    base = annuals[years].shares_diluted
    if latest is None or base is None or base <= 0 or latest <= 0:
        return None
    return 100.0 * ((latest / base) ** (1.0 / years) - 1.0)


def gate_a4_red_flags(
    candidate: CandidateData,
    window: Optional[TrailingWindow],
    params: Params,
    capability_gate: Optional[CapabilityGate],
) -> GateOutcome:
    """Any one red flag disqualifies."""
    hp = params.health
    flags = candidate.filing_flags
    annuals = sorted_annuals(candidate)
    dilution = share_count_growth_pct_per_year(annuals, hp.share_count_growth_window_years)

    equity = window.latest("total_equity") if window else None
    ocf = window.total("operating_cash_flow") if window else None

    detail = {
        "restatement_within_lookback": flags.restatement_within_lookback,
        "auditor_change_within_lookback": flags.auditor_change_within_lookback,
        "auditor_change_reason": flags.auditor_change_reason,
        "going_concern_language": flags.going_concern_language,
        "delayed_filing": flags.delayed_filing,
        "share_count_growth_pct_per_year": dilution,
        "max_share_count_growth_pct_per_year": hp.max_share_count_growth_pct_per_year,
        "total_equity": equity,
        "trailing_operating_cash_flow": ocf,
    }

    unknown = [
        name
        for name, value in (
            ("restatement", flags.restatement_within_lookback),
            ("auditor change", flags.auditor_change_within_lookback),
            ("going concern", flags.going_concern_language),
            ("delayed filing", flags.delayed_filing),
        )
        if value is None
    ]
    if unknown:
        return GateOutcome(
            "A4", Verdict.DATA_GAP,
            "filing red-flag status unknown for: " + ", ".join(unknown)
            + " -- absence of evidence is not a clean bill of health",
            detail,
        )

    if flags.restatement_within_lookback:
        return GateOutcome("A4", Verdict.FAIL, "restatement within lookback", detail)
    if flags.auditor_change_within_lookback and not flags.auditor_change_reason:
        return GateOutcome("A4", Verdict.FAIL, "auditor change with no stated benign reason", detail)
    if flags.going_concern_language:
        return GateOutcome("A4", Verdict.FAIL, "going-concern language present", detail)
    if flags.delayed_filing:
        return GateOutcome("A4", Verdict.FAIL, "delayed filing", detail)
    if equity is not None and ocf is not None and equity < 0 and ocf < 0:
        return GateOutcome("A4", Verdict.FAIL, "negative shareholder equity with negative OCF", detail)

    if dilution is None:
        if capability_gate is not None and not capability_gate.spec_growth_enabled:
            return GateOutcome(
                "A4", Verdict.DATA_GAP,
                "share-count history unavailable -- dilution red flag cannot be enforced",
                detail,
            )
        return GateOutcome("A4", Verdict.DATA_GAP, "share-count history insufficient", detail)
    if dilution > hp.max_share_count_growth_pct_per_year:
        return GateOutcome(
            "A4", Verdict.FAIL,
            f"share count growing {dilution:.1f}%/yr above {hp.max_share_count_growth_pct_per_year:.0f}%",
            detail,
        )

    detail["margin"] = max(0.0, 1.0 - dilution / hp.max_share_count_growth_pct_per_year)
    return GateOutcome("A4", Verdict.PASS, "no red flags", detail)


def gate_a5_data_integrity(
    candidate: CandidateData,
    window: Optional[TrailingWindow],
    params: Params,
    classification: Optional[str],
    as_of: date,
) -> GateOutcome:
    """All required inputs present and fresh. Missing or stale is never imputed."""
    hp = params.health
    is_growth = classification in (CORE_GROWTH, SPEC_GROWTH)
    max_age = hp.max_data_age_months_growth if is_growth else hp.max_data_age_months

    detail = {
        "max_data_age_months": max_age,
        "freshness_basis": "growth" if is_growth else "standard",
        "as_of": as_of.isoformat(),
    }

    if window is None or window.period_end is None:
        return GateOutcome("A5", Verdict.DATA_GAP, "no dated financial period available", detail)

    age = _months_between(window.period_end, as_of)
    detail["latest_period_end"] = window.period_end.isoformat()
    detail["data_age_months"] = age
    detail["basis"] = window.basis

    if age > max_age:
        return GateOutcome(
            "A5", Verdict.FAIL,
            f"latest financials {age:.1f} months old, limit {max_age:.0f}", detail,
        )

    if candidate.profile.price is None:
        return GateOutcome("A5", Verdict.DATA_GAP, "no price", detail)

    detail["margin"] = max(0.0, 1.0 - age / max_age)
    return GateOutcome("A5", Verdict.PASS, f"inputs present, {age:.1f} months old", detail)


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def evaluate_health(
    candidate: CandidateData,
    params: Params,
    as_of: Optional[date] = None,
    capability_gate: Optional[CapabilityGate] = None,
) -> HealthResult:
    """Run the full Module A sequence and return an auditable result."""
    as_of = as_of or candidate.as_of or date.today()
    window = build_trailing_window(candidate)

    provisional, class_detail = classify(candidate, params, capability_gate)

    gates = [
        gate_universe(candidate, params, provisional),
        gate_a1_solvency(window, params),
        gate_a2_leverage(candidate, window, params),
        gate_a3_earnings_quality(window, params, capability_gate),
        gate_a4_red_flags(candidate, window, params, capability_gate),
        gate_a5_data_integrity(candidate, window, params, provisional, as_of),
    ]

    final, final_detail = classify(candidate, params, capability_gate)
    if final != provisional:
        # Cannot happen with the current router, which does not read gate
        # results -- but if a future revision makes it path-dependent, the
        # thresholds used above would no longer match the tag assigned below.
        gates.append(
            GateOutcome(
                "A6", Verdict.DATA_GAP,
                f"classification unstable between passes: {provisional} then {final}",
                {"provisional": provisional, "final": final},
            )
        )
        return HealthResult(
            symbol=candidate.symbol,
            verdict=Verdict.DATA_GAP,
            classification=None,
            gates=gates,
            trailing_basis=window.basis if window else None,
            reasons=["classification unstable"],
            classification_detail=final_detail,
        )

    a6 = GateOutcome(
        "A6",
        Verdict.PASS if final else Verdict.FAIL,
        final or "no classification matched",
        final_detail,
    )
    gates.append(a6)

    failures = [g for g in gates if g.verdict is Verdict.FAIL]
    data_gaps = [g for g in gates if g.verdict is Verdict.DATA_GAP]

    if failures:
        verdict = Verdict.FAIL
    elif data_gaps:
        verdict = Verdict.DATA_GAP
    else:
        verdict = Verdict.PASS

    a1 = next((g for g in gates if g.gate == "A1"), None)
    return HealthResult(
        symbol=candidate.symbol,
        verdict=verdict,
        classification=final if verdict is Verdict.PASS else None,
        gates=gates,
        a1_branch=(a1.detail.get("branch") if a1 else None),
        trailing_basis=window.basis if window else None,
        reasons=[f"{g.gate}: {g.reason}" for g in failures + data_gaps],
        classification_detail=final_detail,
    )
