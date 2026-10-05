"""Module B -- sector-adaptive fair value.

One method per classification. The code selects; it never blends and never
approximates. Prime Directive 3 makes a single-stage DCF on a pre-profit
company, or a P/E on negative earnings, a first-order modelling error, so the
banned combinations raise ``MethodMismatch`` rather than returning a number
somebody might read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
from ..modules.a_health import TrailingWindow, sorted_annuals
from ..types import CandidateData, Financials


class MethodMismatch(Exception):
    """Raised when a valuation method is applied to the wrong business type."""


class ValuationImpossible(Exception):
    """Raised when the inputs a method requires are absent.

    Distinct from ``MethodMismatch``: the method was right, the data was not
    there. Callers surface it as a data gap instead of a fair value.
    """


@dataclass
class Scenario:
    name: str
    weight: float
    growth_multiplier: float
    terminal_fcf_margin_pct: Optional[float]
    description: str


@dataclass
class ValuationResult:
    symbol: str
    classification: str
    method: str
    fair_value_per_share: Optional[float]
    diagnostics: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    scenario_values: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        """Fair value is never reported as a universal number."""
        return f"{self.method} [{self.classification}]"


# ---------------------------------------------------------------------------
# shared mechanics
# ---------------------------------------------------------------------------


def capm_discount_rate_pct(beta: Optional[float], params: Params, floor_pct: float) -> float:
    """CAPM on the company's own beta, floored.

    The floor is the operative number more often than the CAPM output, and that
    is deliberate: a low-beta business is not a low-risk one at an arbitrary
    price, and the floor stops the discount rate from being the free parameter
    that makes a name pass.
    """
    vp = params.valuation
    if beta is None:
        return floor_pct
    modelled = vp.risk_free_rate_pct + beta * vp.equity_risk_premium_pct
    return max(floor_pct, modelled)


def _present_value(cash_flows: Sequence[float], discount_rate: float) -> float:
    return sum(cf / (1.0 + discount_rate) ** (i + 1) for i, cf in enumerate(cash_flows))


def _linear_fade(start_pct: float, end_pct: float, years: int) -> list[float]:
    """Growth path fading linearly from ``start_pct`` to ``end_pct``."""
    if years <= 0:
        return []
    step = (end_pct - start_pct) / years
    return [start_pct + step * (i + 1) for i in range(years)]


def revenue_cagr_pct(annuals: Sequence[Financials], years: int) -> Optional[float]:
    if len(annuals) < years + 1:
        return None
    latest, base = annuals[0].revenue, annuals[years].revenue
    if latest is None or base is None or base <= 0 or latest <= 0:
        return None
    return 100.0 * ((latest / base) ** (1.0 / years) - 1.0)


def average_growth_pct(values: Sequence[Optional[float]]) -> Optional[float]:
    """Mean year-on-year growth across a series, oldest to newest."""
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return None
    rates = []
    for prior, latest in zip(clean, clean[1:]):
        if prior is None or prior <= 0:
            continue
        rates.append(100.0 * (latest / prior - 1.0))
    if not rates:
        return None
    return sum(rates) / len(rates)


def _equity_value_per_share(
    enterprise_value: float, net_debt: Optional[float], shares: Optional[float]
) -> Optional[float]:
    if shares is None or shares <= 0:
        return None
    equity = enterprise_value - (net_debt or 0.0)
    return equity / shares


# ---------------------------------------------------------------------------
# B1 -- CORE-STABLE two-stage DCF
# ---------------------------------------------------------------------------


def value_core_stable(
    candidate: CandidateData,
    window: TrailingWindow,
    params: Params,
    classification: str = CORE_STABLE,
) -> ValuationResult:
    """Two-stage DCF with a linear fade to terminal.

    Stage one runs at the trailing five-year average growth rate, capped there
    rather than at anything more optimistic. Stage two fades that rate linearly
    into the terminal rate, which is hard-capped at 2.5% and never above.
    """
    if classification != CORE_STABLE:
        raise MethodMismatch(
            f"two-stage FCF DCF is the CORE-STABLE method; {classification} must use its own path"
        )

    vp = params.valuation
    annuals = sorted_annuals(candidate)
    base_fcf = window.total("free_cash_flow")
    if base_fcf is None:
        ocf = window.total("operating_cash_flow")
        capex = window.total("capital_expenditure")
        if ocf is None or capex is None:
            raise ValuationImpossible(f"{candidate.symbol}: trailing free cash flow not computable")
        base_fcf = ocf - abs(capex)

    if base_fcf <= 0:
        raise ValuationImpossible(
            f"{candidate.symbol}: CORE-STABLE DCF requires positive trailing FCF, got {base_fcf:,.0f}"
        )

    fcf_series = [p.computed_free_cash_flow for p in reversed(annuals[: vp.b1_stage1_years + 1])]
    growth_pct = average_growth_pct(fcf_series)
    if growth_pct is None:
        growth_pct = revenue_cagr_pct(annuals, min(vp.b1_stage1_years, max(1, len(annuals) - 1)))
    if growth_pct is None:
        raise ValuationImpossible(f"{candidate.symbol}: no growth history for the DCF")

    terminal_pct = min(vp.b1_max_terminal_growth_pct, max(0.0, growth_pct))
    stage1_pct = max(terminal_pct, growth_pct)

    discount_pct = capm_discount_rate_pct(candidate.profile.beta, params, vp.b1_min_discount_rate_pct)
    r = discount_pct / 100.0
    gt = terminal_pct / 100.0
    if r <= gt:
        raise ValuationImpossible(
            f"{candidate.symbol}: discount rate {discount_pct:.1f}% not above terminal growth "
            f"{terminal_pct:.1f}% -- perpetuity undefined"
        )

    def run(stage1_growth_pct: float) -> tuple[float, float, list[float]]:
        path = [stage1_growth_pct] * vp.b1_stage1_years
        path += _linear_fade(stage1_growth_pct, terminal_pct, vp.b1_fade_years)
        flows: list[float] = []
        cash = base_fcf
        for g in path:
            cash = cash * (1.0 + g / 100.0)
            flows.append(cash)
        pv_explicit = _present_value(flows, r)
        terminal_value = flows[-1] * (1.0 + gt) / (r - gt)
        pv_terminal = terminal_value / (1.0 + r) ** len(flows)
        return pv_explicit, pv_terminal, flows

    pv_explicit, pv_terminal, flows = run(stage1_pct)
    enterprise_value = pv_explicit + pv_terminal

    net_debt = window.periods[0].net_debt if window.periods else None
    shares = window.latest("shares_diluted") or candidate.profile.shares_outstanding
    fair_value = _equity_value_per_share(enterprise_value, net_debt, shares)

    tv_share_pct = 100.0 * pv_terminal / enterprise_value if enterprise_value else None

    # Mandatory sensitivity: does the verdict survive half the year-one rate?
    half_pv_explicit, half_pv_terminal, _ = run(stage1_pct * vp.b1_sensitivity_growth_multiplier)
    half_ev = half_pv_explicit + half_pv_terminal
    half_fair_value = _equity_value_per_share(half_ev, net_debt, shares)

    warnings: list[str] = []
    if tv_share_pct is not None and tv_share_pct > vp.b1_terminal_value_share_flag_pct:
        warnings.append(
            f"TERMINAL VALUE {tv_share_pct:.0f}% OF EV -- above the "
            f"{vp.b1_terminal_value_share_flag_pct:.0f}% flag. This model is valuing a perpetuity "
            "assumption more than a business."
        )

    price = candidate.profile.price
    survives = None
    if price is not None and fair_value is not None and half_fair_value is not None:
        survives = half_fair_value > price
        if fair_value > price and not survives:
            warnings.append(
                "HALF-GROWTH RE-RUN BREAKS THE VERDICT -- undervaluation depends on the "
                "full growth assumption holding."
            )

    return ValuationResult(
        symbol=candidate.symbol,
        classification=classification,
        method="two-stage DCF (B1)",
        fair_value_per_share=fair_value,
        diagnostics={
            "base_trailing_fcf": base_fcf,
            "stage1_growth_pct": stage1_pct,
            "terminal_growth_pct": terminal_pct,
            "discount_rate_pct": discount_pct,
            "beta": candidate.profile.beta,
            "explicit_years": len(flows),
            "pv_explicit": pv_explicit,
            "pv_terminal": pv_terminal,
            "enterprise_value": enterprise_value,
            "terminal_value_share_pct": tv_share_pct,
            "net_debt": net_debt,
            "shares_diluted": shares,
            "half_growth_fair_value_per_share": half_fair_value,
            "half_growth_verdict_survives": survives,
        },
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# B2 -- CORE-GROWTH / SPEC-GROWTH three-phase scenario DCF
# ---------------------------------------------------------------------------


def default_scenarios(params: Params) -> list[Scenario]:
    """Bear / base / bull at 30 / 50 / 20.

    The bear case models the growth thesis *failing* -- growth collapsing
    toward nothing and the mature margin landing well below plan -- not merely
    growing a little more slowly.
    """
    weights = params.valuation.b2_scenario_weights
    return [
        Scenario("bear", weights["bear"], 0.20, 5.0, "thesis fails: growth collapses, margin never arrives"),
        Scenario("base", weights["base"], 1.00, 15.0, "plan roughly holds"),
        Scenario("bull", weights["bull"], 1.25, 22.0, "plan beaten on both growth and margin"),
    ]


def value_growth(
    candidate: CandidateData,
    window: TrailingWindow,
    params: Params,
    classification: str,
    tam_usd: Optional[float] = None,
    scenarios: Optional[Sequence[Scenario]] = None,
) -> ValuationResult:
    """Three-phase, scenario-weighted, revenue-driven DCF.

    Revenue-driven rather than FCF-driven because the SPEC-GROWTH path starts
    from negative free cash flow, where compounding a negative base is
    meaningless. Revenue is projected, and a maturity FCF margin is ramped in
    over the fade so that the terminal value rests on a stated margin
    assumption a reader can argue with.
    """
    if classification not in (CORE_GROWTH, SPEC_GROWTH):
        raise MethodMismatch(
            f"scenario DCF is the GROWTH method; {classification} must use its own path"
        )

    vp = params.valuation
    annuals = sorted_annuals(candidate)
    revenue = window.total("revenue")
    if revenue is None or revenue <= 0:
        raise ValuationImpossible(f"{candidate.symbol}: trailing revenue unavailable")

    cagr_2y = revenue_cagr_pct(annuals, 2)
    if cagr_2y is None:
        raise ValuationImpossible(f"{candidate.symbol}: 2-year revenue CAGR unavailable")

    # Phase 1 runs at the company's own trailing rate, and the 1.5x multiple is
    # a ceiling on any forward estimate -- never the assumption itself. Taking
    # the cap as the base would make the most optimistic permitted number the
    # default one.
    growth_cap_pct = cagr_2y * vp.b2_phase1_growth_cap_multiple
    forward_growth_pct = None
    est = candidate.estimates
    if est.forward_revenue is not None and revenue > 0:
        forward_growth_pct = 100.0 * (est.forward_revenue / revenue - 1.0)
    base_growth_pct = cagr_2y if forward_growth_pct is None else min(forward_growth_pct, growth_cap_pct)
    base_growth_pct = max(0.0, min(base_growth_pct, growth_cap_pct))

    floor = (
        vp.b2_min_discount_rate_spec_growth_pct
        if classification == SPEC_GROWTH
        else vp.b2_min_discount_rate_core_growth_pct
    )
    discount_pct = capm_discount_rate_pct(candidate.profile.beta, params, floor)
    r = discount_pct / 100.0
    gt = vp.b2_max_terminal_growth_pct / 100.0
    if r <= gt:
        raise ValuationImpossible(f"{candidate.symbol}: discount rate not above terminal growth")

    net_debt = window.periods[0].net_debt if window.periods else None
    shares = window.latest("shares_diluted") or candidate.profile.shares_outstanding
    scenario_list = list(scenarios or default_scenarios(params))

    scenario_values: dict = {}
    scenario_detail: dict = {}
    weighted = 0.0
    weight_total = 0.0

    for scenario in scenario_list:
        g1 = base_growth_pct * scenario.growth_multiplier
        path = [g1] * vp.b2_phase1_years
        path += _linear_fade(g1, vp.b2_max_terminal_growth_pct, vp.b2_phase2_years)

        margins = _linear_fade(0.0, scenario.terminal_fcf_margin_pct or 0.0, len(path))
        flows: list[float] = []
        projected_revenue = revenue
        for growth_pct, margin_pct in zip(path, margins):
            projected_revenue *= 1.0 + growth_pct / 100.0
            flows.append(projected_revenue * margin_pct / 100.0)

        pv_explicit = _present_value(flows, r)
        terminal_value = flows[-1] * (1.0 + gt) / (r - gt)
        pv_terminal = terminal_value / (1.0 + r) ** len(flows)
        enterprise_value = pv_explicit + pv_terminal
        per_share = _equity_value_per_share(enterprise_value, net_debt, shares)
        scenario_values[scenario.name] = per_share
        scenario_detail[scenario.name] = {
            "weight": scenario.weight,
            "phase1_growth_pct": g1,
            "terminal_fcf_margin_pct": scenario.terminal_fcf_margin_pct,
            "terminal_revenue": projected_revenue,
            "enterprise_value": enterprise_value,
            "terminal_value_share_pct": 100.0 * pv_terminal / enterprise_value if enterprise_value else None,
            "description": scenario.description,
        }
        if per_share is not None:
            weighted += scenario.weight * per_share
            weight_total += scenario.weight

    fair_value = weighted / weight_total if weight_total else None

    warnings: list[str] = []

    # TAM ceiling test.
    base_terminal_revenue = scenario_detail.get("base", {}).get("terminal_revenue")
    implied_share_pct = None
    if tam_usd and base_terminal_revenue:
        implied_share_pct = 100.0 * base_terminal_revenue / tam_usd
        if implied_share_pct > vp.b2_tam_ceiling_share_pct:
            warnings.append(
                f"TAM CEILING BREACHED -- base case implies {implied_share_pct:.0f}% share of the "
                f"stated addressable market by year {vp.b2_tam_horizon_years}, above the "
                f"{vp.b2_tam_ceiling_share_pct:.0f}% ceiling."
            )
    else:
        warnings.append(
            "TAM CEILING TEST NOT RUN -- no realistically-defined addressable market supplied. "
            "The growth path is unbounded by any share constraint."
        )

    # EV/Revenue cross-check, growth-adjusted.
    market_cap = candidate.profile.market_cap
    ev_to_revenue = None
    ev_to_revenue_per_growth = None
    if market_cap is not None:
        current_ev = market_cap + (net_debt or 0.0)
        ev_to_revenue = current_ev / revenue
        if cagr_2y and cagr_2y > 0:
            ev_to_revenue_per_growth = ev_to_revenue / cagr_2y

    bear_value = scenario_values.get("bear")
    price = candidate.profile.price
    if bear_value is not None and price is not None and bear_value < 0.5 * price:
        warnings.append(
            f"BEAR CASE {bear_value:,.2f} IS BELOW HALF THE PRICE -- a thesis failure costs "
            "more than half the position."
        )

    return ValuationResult(
        symbol=candidate.symbol,
        classification=classification,
        method="three-phase scenario DCF (B2)",
        fair_value_per_share=fair_value,
        diagnostics={
            "trailing_revenue": revenue,
            "revenue_2y_cagr_pct": cagr_2y,
            "phase1_growth_pct": base_growth_pct,
            "phase1_growth_cap_pct": growth_cap_pct,
            "forward_revenue_growth_pct": forward_growth_pct,
            "discount_rate_pct": discount_pct,
            "discount_rate_floor_pct": floor,
            "terminal_growth_pct": vp.b2_max_terminal_growth_pct,
            "net_debt": net_debt,
            "shares_diluted": shares,
            "scenarios": scenario_detail,
            "tam_usd": tam_usd,
            "implied_year10_market_share_pct": implied_share_pct,
            "ev_to_revenue": ev_to_revenue,
            "ev_to_revenue_per_growth_point": ev_to_revenue_per_growth,
            "probability_weighted": True,
        },
        warnings=warnings,
        scenario_values=scenario_values,
    )


# ---------------------------------------------------------------------------
# B3 -- FINANCIAL-BANK
# ---------------------------------------------------------------------------


def value_bank(
    candidate: CandidateData,
    window: TrailingWindow,
    params: Params,
    peer_median_p_tbv: Optional[float],
    classification: str = FINANCIAL_BANK,
) -> ValuationResult:
    """P/B and P/TBV against sector, plus ROE sustainability.

    Standard DCF is banned here: a bank's revenue and cost structure does not
    map onto a normal cash-flow build, and forcing one produces a number with
    the shape of a valuation and none of the meaning.
    """
    if classification != FINANCIAL_BANK:
        raise MethodMismatch(f"bank relative valuation applied to {classification}")
    if peer_median_p_tbv is None or peer_median_p_tbv <= 0:
        raise ValuationImpossible(f"{candidate.symbol}: no peer median P/TBV available")

    equity = window.latest("total_equity")
    tangible = window.periods[0].tangible_book_value if window.periods else None
    shares = window.latest("shares_diluted") or candidate.profile.shares_outstanding
    net_income = window.total("net_income")

    if tangible is None or shares is None or shares <= 0:
        raise ValuationImpossible(f"{candidate.symbol}: tangible book value per share not computable")

    tbv_per_share = tangible / shares
    fair_value = peer_median_p_tbv * tbv_per_share

    current_roe_pct = None
    if net_income is not None and equity not in (None, 0):
        current_roe_pct = 100.0 * net_income / equity

    annuals = sorted_annuals(candidate)
    historical_roes = [
        100.0 * p.net_income / p.total_equity
        for p in annuals[:10]
        if p.net_income is not None and p.total_equity not in (None, 0)
    ]
    long_run_roe_pct = sum(historical_roes) / len(historical_roes) if historical_roes else None

    warnings: list[str] = []
    roe_gap_pp = None
    if current_roe_pct is not None and long_run_roe_pct is not None:
        roe_gap_pp = current_roe_pct - long_run_roe_pct
        if abs(roe_gap_pp) >= 3.0:
            warnings.append(
                f"ROE {current_roe_pct:.1f}% is {roe_gap_pp:+.1f}pp against its own "
                f"{len(historical_roes)}-year average of {long_run_roe_pct:.1f}%. State the reason "
                "for the gap before treating the current level as sustainable."
            )
    if len(historical_roes) < 10:
        warnings.append(
            f"ROE sustainability read uses {len(historical_roes)} years, not the full 10."
        )

    price = candidate.profile.price
    return ValuationResult(
        symbol=candidate.symbol,
        classification=classification,
        method="P/TBV and P/B relative to sector (B3)",
        fair_value_per_share=fair_value,
        diagnostics={
            "tangible_book_value_per_share": tbv_per_share,
            "book_value_per_share": (equity / shares) if equity is not None else None,
            "peer_median_p_tbv": peer_median_p_tbv,
            "current_p_tbv": (price / tbv_per_share) if price else None,
            "current_roe_pct": current_roe_pct,
            "long_run_roe_pct": long_run_roe_pct,
            "roe_gap_pp": roe_gap_pp,
            "roe_years_available": len(historical_roes),
            "dcf_banned": True,
        },
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# B4 -- REIT
# ---------------------------------------------------------------------------


def compute_ffo(period: Financials) -> Optional[float]:
    if period.ffo is not None:
        return period.ffo
    if period.net_income is None:
        return None
    depreciation = period.real_estate_depreciation
    if depreciation is None:
        depreciation = period.depreciation_amortisation
    if depreciation is None:
        return None
    return period.net_income + depreciation - (period.gains_on_property_sales or 0.0)


def compute_affo(period: Financials) -> Optional[float]:
    if period.affo is not None:
        return period.affo
    ffo = compute_ffo(period)
    if ffo is None:
        return None
    if period.recurring_capex is None:
        return None
    return ffo - abs(period.recurring_capex) - (period.straight_line_rent_adjustment or 0.0)


def value_reit(
    candidate: CandidateData,
    window: TrailingWindow,
    params: Params,
    peer_median_p_affo: Optional[float],
    sector_cap_rate_pct: Optional[float] = None,
    classification: str = REIT,
) -> ValuationResult:
    """P/AFFO primary, P/FFO secondary. P/E is banned.

    Depreciation distorts REIT earnings badly enough to make the ratio
    meaningless, so the earnings multiple is not offered even as a fallback.
    """
    if classification != REIT:
        raise MethodMismatch(f"REIT valuation applied to {classification}")

    shares = window.latest("shares_diluted") or candidate.profile.shares_outstanding
    if shares is None or shares <= 0:
        raise ValuationImpossible(f"{candidate.symbol}: share count unavailable")

    ffo_total = None
    affo_total = None
    ffo_parts = [compute_ffo(p) for p in window.periods]
    affo_parts = [compute_affo(p) for p in window.periods]
    if all(v is not None for v in ffo_parts) and ffo_parts:
        ffo_total = sum(ffo_parts)
    if all(v is not None for v in affo_parts) and affo_parts:
        affo_total = sum(affo_parts)

    warnings: list[str] = []
    if affo_total is None:
        warnings.append(
            "AFFO NOT COMPUTABLE -- recurring maintenance capex and straight-line rent "
            "adjustments were not supplied. Falling back to the P/FFO secondary read; the "
            "primary B4 multiple is unavailable."
        )
    if peer_median_p_affo is None or peer_median_p_affo <= 0:
        raise ValuationImpossible(f"{candidate.symbol}: no peer median P/AFFO or P/FFO available")

    basis_total = affo_total if affo_total is not None else ffo_total
    if basis_total is None:
        raise ValuationImpossible(f"{candidate.symbol}: neither AFFO nor FFO computable")
    basis_name = "AFFO" if affo_total is not None else "FFO"

    per_share = basis_total / shares
    fair_value = peer_median_p_affo * per_share

    price = candidate.profile.price
    implied_cap_rate_pct = None
    net_debt = window.periods[0].net_debt if window.periods else None
    market_cap = candidate.profile.market_cap
    noi_proxy = window.total("operating_income")
    if market_cap is not None and noi_proxy:
        enterprise_value = market_cap + (net_debt or 0.0)
        if enterprise_value > 0:
            implied_cap_rate_pct = 100.0 * noi_proxy / enterprise_value
    if implied_cap_rate_pct is not None and sector_cap_rate_pct:
        spread = implied_cap_rate_pct - sector_cap_rate_pct
        if abs(spread) >= 1.0:
            warnings.append(
                f"IMPLIED CAP RATE {implied_cap_rate_pct:.1f}% versus sector environment "
                f"{sector_cap_rate_pct:.1f}% ({spread:+.1f}pp)."
            )
    elif sector_cap_rate_pct is None:
        warnings.append("No sector cap-rate environment supplied -- cap-rate cross-check not run.")

    return ValuationResult(
        symbol=candidate.symbol,
        classification=classification,
        method=f"P/{basis_name} relative to peers (B4)",
        fair_value_per_share=fair_value,
        diagnostics={
            "basis": basis_name,
            "ffo_trailing": ffo_total,
            "affo_trailing": affo_total,
            f"{basis_name.lower()}_per_share": per_share,
            "peer_median_multiple": peer_median_p_affo,
            f"current_p_{basis_name.lower()}": (price / per_share) if price and per_share else None,
            "implied_cap_rate_pct": implied_cap_rate_pct,
            "sector_cap_rate_pct": sector_cap_rate_pct,
            "pe_banned": True,
        },
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# B5 -- INSURER
# ---------------------------------------------------------------------------


def combined_ratio_pct(period: Financials) -> Optional[float]:
    if period.earned_premium in (None, 0):
        return None
    losses = period.losses_and_lae_incurred
    expense = period.underwriting_expense
    if losses is None or expense is None:
        return None
    return 100.0 * (losses + expense) / period.earned_premium


def value_insurer(
    candidate: CandidateData,
    window: TrailingWindow,
    params: Params,
    peer_median_p_b: Optional[float],
    classification: str = INSURER,
) -> ValuationResult:
    """Combined ratio trend plus P/B against sector.

    Operating ROE *excluding unrealised investment gains* is the primary
    quality read. An insurer whose profit is investment-gain-driven has already
    failed A3 before reaching here.
    """
    if classification != INSURER:
        raise MethodMismatch(f"insurer valuation applied to {classification}")
    if peer_median_p_b is None or peer_median_p_b <= 0:
        raise ValuationImpossible(f"{candidate.symbol}: no peer median P/B available")

    equity = window.latest("total_equity")
    shares = window.latest("shares_diluted") or candidate.profile.shares_outstanding
    if equity is None or shares is None or shares <= 0:
        raise ValuationImpossible(f"{candidate.symbol}: book value per share not computable")

    book_per_share = equity / shares
    fair_value = peer_median_p_b * book_per_share

    annuals = sorted_annuals(candidate)
    ratios = [(p.period_end, combined_ratio_pct(p)) for p in annuals[:5]]
    ratios = [(d, v) for d, v in ratios if v is not None]
    current_cr = ratios[0][1] if ratios else None

    net_income = window.total("net_income")
    unrealised = window.total("unrealised_investment_gains")
    operating_roe_pct = None
    if net_income is not None and equity:
        operating_income_ex_gains = net_income - (unrealised or 0.0)
        operating_roe_pct = 100.0 * operating_income_ex_gains / equity

    warnings: list[str] = []
    if current_cr is None:
        warnings.append(
            "COMBINED RATIO NOT COMPUTABLE -- losses and LAE or underwriting expense were not "
            "supplied. The B5 primary underwriting read is unavailable."
        )
    elif current_cr >= 100.0:
        warnings.append(
            f"COMBINED RATIO {current_cr:.1f} -- underwriting loss; profit is coming from the "
            "investment portfolio, not the business."
        )
    if unrealised is None:
        warnings.append(
            "Unrealised investment gains not separable -- operating ROE could not be cleaned."
        )

    trend = None
    if len(ratios) >= 2:
        trend = ratios[0][1] - ratios[-1][1]

    price = candidate.profile.price
    return ValuationResult(
        symbol=candidate.symbol,
        classification=classification,
        method="combined ratio and P/B relative to sector (B5)",
        fair_value_per_share=fair_value,
        diagnostics={
            "book_value_per_share": book_per_share,
            "peer_median_p_b": peer_median_p_b,
            "current_p_b": (price / book_per_share) if price else None,
            "combined_ratio_pct": current_cr,
            "combined_ratio_history": [(d.isoformat(), v) for d, v in ratios],
            "combined_ratio_trend_pp": trend,
            "operating_roe_ex_unrealised_pct": operating_roe_pct,
            "underwriting_profitable": (current_cr < 100.0) if current_cr is not None else None,
        },
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# router
# ---------------------------------------------------------------------------


def value_candidate(
    candidate: CandidateData,
    window: TrailingWindow,
    params: Params,
    classification: str,
    peer_median_multiple: Optional[float] = None,
    tam_usd: Optional[float] = None,
    sector_cap_rate_pct: Optional[float] = None,
) -> ValuationResult:
    """Select the one method that fits this classification."""
    if classification == CORE_STABLE:
        return value_core_stable(candidate, window, params)
    if classification in (CORE_GROWTH, SPEC_GROWTH):
        return value_growth(candidate, window, params, classification, tam_usd=tam_usd)
    if classification == FINANCIAL_BANK:
        return value_bank(candidate, window, params, peer_median_multiple)
    if classification == REIT:
        return value_reit(candidate, window, params, peer_median_multiple, sector_cap_rate_pct)
    if classification == INSURER:
        return value_insurer(candidate, window, params, peer_median_multiple)
    raise MethodMismatch(f"no Module B method for classification {classification!r}")
