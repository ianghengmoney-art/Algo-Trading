"""Module B — sector-adaptive fair value.

Prime Directive 3: the method matches the business type.  A single-stage DCF on
a pre-profit company, or P/E on negative earnings, is a first-order error — the
code refuses, never approximates.  That refusal is literal here: each path
raises :class:`MethodRefused` rather than returning a degraded number, and the
dispatcher will not run a DCF on a bank or a P/E on a REIT even if asked.

Output is a point-estimate fair value (probability-weighted for B2), labelled
with method and classification, in the listing currency — Module K handles
translation, and K2 is explicit that inputs are never translated before
valuing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from ..classification import Classification
from ..config import Config
from ..data.adapter import DataUnavailable
from ..ledger import AuditLedger
from ..types import CompanyData, MarketData, PeriodFinancials, TamSource
from .b_discount import DiscountRate, build_discount_rate


class MethodRefused(Exception):
    """The requested method does not fit this business.

    Raised instead of returning an approximation.  A P/E on negative earnings
    is not a bad estimate, it is a meaningless one, and the difference matters:
    a bad estimate invites a judgement call, a refusal does not.
    """


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    weight: float
    equity_value: float
    per_share: float
    growth_rate: float
    terminal_value_share: float


@dataclass
class FairValue:
    """Module B's output for one company."""

    symbol: str
    classification: Classification
    method: str
    #: Point estimate per share, in the listing currency.
    fair_value_per_share: float
    currency: str
    discount_rate: DiscountRate | None = None
    #: B1: terminal value as a share of enterprise value.  Flagged above 75%.
    terminal_value_share: float | None = None
    #: B1: the verdict re-run at half the year-1 growth rate.
    half_growth_fair_value: float | None = None
    scenarios: list[ScenarioResult] = field(default_factory=list)
    cross_check: dict[str, float] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    diagnostics: dict[str, object] = field(default_factory=dict)

    def discount_to(self, price: float) -> float:
        """Fractional discount of ``price`` to fair value.

        Positive means the price sits below fair value.  E's gate X and D2's
        excess are both computed off this one number.
        """
        if self.fair_value_per_share <= 0:
            raise MethodRefused(
                f"{self.symbol}: fair value is non-positive; no discount is meaningful"
            )
        return 1.0 - (price / self.fair_value_per_share)

    def as_log_line(self) -> str:
        bits = [
            f"fair_value={self.fair_value_per_share:,.2f} {self.currency}",
            f"method={self.method}",
            f"classification={self.classification.value}",
        ]
        if self.terminal_value_share is not None:
            bits.append(f"tv_share={self.terminal_value_share:.1%}")
        if self.half_growth_fair_value is not None:
            bits.append(f"half_growth_fv={self.half_growth_fair_value:,.2f}")
        for k, v in self.cross_check.items():
            bits.append(f"{k}={v:,.3g}")
        if self.flags:
            bits.append("flags=" + "; ".join(self.flags))
        return " · ".join(bits)


# -- shared DCF machinery -------------------------------------------------


def _fcf(row: PeriodFinancials) -> float | None:
    """Free cash flow, derived when not reported.

    Vendors report capex as a negative number; adding is correct and
    subtracting silently doubles it.  A missing leg yields ``None``.
    """
    if row.free_cash_flow is not None:
        return row.free_cash_flow
    if row.operating_cash_flow is None or row.capital_expenditure is None:
        return None
    return row.operating_cash_flow + row.capital_expenditure


def _trailing_growth(rows: Sequence[PeriodFinancials], years: int) -> float | None:
    """Revenue CAGR over ``years``, newest-first rows."""
    if len(rows) < years + 1:
        return None
    newest, oldest = rows[0].revenue, rows[years].revenue
    if newest is None or not oldest or oldest <= 0 or newest <= 0:
        return None
    return (newest / oldest) ** (1.0 / years) - 1.0


def _linear_fade(start: float, end: float, steps: int) -> list[float]:
    """``steps`` growth rates fading linearly from ``start`` to ``end``."""
    if steps <= 0:
        return []
    if steps == 1:
        return [end]
    return [start + (end - start) * (i / (steps - 1)) for i in range(steps)]


def _discounted_dcf(
    base_cash_flow: float,
    growth_path: Sequence[float],
    terminal_growth: float,
    discount_rate: float,
) -> tuple[float, float]:
    """Return (enterprise value, terminal value share).

    Raises when the terminal growth is not strictly below the discount rate —
    the Gordon denominator goes to zero or negative and produces a number that
    looks like a valuation but is an artefact.
    """
    if discount_rate <= terminal_growth:
        raise MethodRefused(
            f"discount rate {discount_rate:.2%} is not above terminal growth "
            f"{terminal_growth:.2%}; the perpetuity is undefined"
        )
    cash_flow = base_cash_flow
    pv_explicit = 0.0
    for year, growth in enumerate(growth_path, start=1):
        cash_flow *= 1.0 + growth
        pv_explicit += cash_flow / (1.0 + discount_rate) ** year

    terminal_cf = cash_flow * (1.0 + terminal_growth)
    terminal_value = terminal_cf / (discount_rate - terminal_growth)
    pv_terminal = terminal_value / (1.0 + discount_rate) ** len(growth_path)

    enterprise = pv_explicit + pv_terminal
    share = pv_terminal / enterprise if enterprise > 0 else float("nan")
    return enterprise, share


def _equity_per_share(
    enterprise_value: float, data: CompanyData
) -> tuple[float, float]:
    """Bridge enterprise value to per-share equity value."""
    latest = data.latest_quarter or data.latest_annual
    if latest is None:
        raise DataUnavailable("financials", "no period available for the equity bridge")
    net_debt = latest.net_debt
    if net_debt is None:
        raise DataUnavailable(
            "net_debt", "total debt or cash missing; equity bridge not computable"
        )
    shares = latest.shares_diluted or latest.shares_outstanding
    if not shares or shares <= 0:
        raise DataUnavailable("shares_diluted", "share count missing or non-positive")
    equity = enterprise_value - net_debt
    return equity, equity / shares


# -- B1 -------------------------------------------------------------------


def value_b1_core_stable(
    data: CompanyData,
    market: MarketData,
    config: Config,
    ledger: AuditLedger | None = None,
) -> FairValue:
    """Two-stage DCF for CORE-STABLE.

    Growth capped at the trailing 5-year average, fading linearly to terminal.
    Terminal growth <= 2.5%, never above.
    """
    cfg = config.valuation
    annual = data.trailing_years(cfg.b1_stage_one_years + 1)
    base = _fcf(annual[0]) if annual else None
    if base is None:
        raise DataUnavailable("free_cash_flow", "B1 needs a base-year free cash flow")
    if base <= 0:
        raise MethodRefused(
            f"{data.profile.symbol}: base free cash flow is {base:,.0f}; a "
            "two-stage DCF on negative cash flow is a first-order error"
        )

    trailing = _trailing_growth(annual, cfg.b1_stage_one_years)
    if trailing is None:
        raise DataUnavailable(
            "revenue_history",
            f"B1 needs {cfg.b1_stage_one_years} years of revenue to cap growth",
        )

    terminal = cfg.terminal_growth_cap
    # Growth is capped at the trailing 5-year average and never allowed below
    # terminal in stage one — a company shrinking toward perpetuity is not a
    # CORE-STABLE name and A6 should not have routed it here, but the floor
    # keeps the path arithmetically sound either way.
    start_growth = max(trailing, terminal)

    discount = build_discount_rate(
        data, market, config, Classification.CORE_STABLE
    )
    stage_one = [start_growth] * cfg.b1_stage_one_years
    stage_two = _linear_fade(
        start_growth, terminal, cfg.b1_projection_years - cfg.b1_stage_one_years
    )
    path = stage_one + stage_two

    enterprise, tv_share = _discounted_dcf(base, path, terminal, discount.rate)
    _, per_share = _equity_per_share(enterprise, data)

    flags: list[str] = list(discount.flags)
    if tv_share > cfg.terminal_value_share_flag:
        flags.append(
            f"TERMINAL VALUE {tv_share:.0%} OF EV — the model is valuing a "
            "perpetuity assumption, not a business"
        )

    # Mandatory diagnostic: re-run at half the year-1 growth rate and show
    # whether the verdict survives.
    half_path = [start_growth / 2.0] * cfg.b1_stage_one_years + _linear_fade(
        start_growth / 2.0, terminal, cfg.b1_projection_years - cfg.b1_stage_one_years
    )
    half_ev, _ = _discounted_dcf(base, half_path, terminal, discount.rate)
    _, half_per_share = _equity_per_share(half_ev, data)

    result = FairValue(
        symbol=data.profile.symbol,
        classification=Classification.CORE_STABLE,
        method="B1 two-stage DCF",
        fair_value_per_share=per_share,
        currency=data.profile.reporting_currency,
        discount_rate=discount,
        terminal_value_share=tv_share,
        half_growth_fair_value=half_per_share,
        flags=flags,
        diagnostics={
            "base_fcf": base,
            "trailing_5y_growth": trailing,
            "start_growth": start_growth,
            "terminal_growth": terminal,
            "enterprise_value": enterprise,
        },
    )
    if ledger is not None:
        ledger.note(f"B1 · {result.as_log_line()}")
        ledger.note(f"B1 discount · {discount.as_log_line()}")
    return result


# -- B2 -------------------------------------------------------------------


@dataclass(frozen=True)
class TamAssessment:
    """B2.1's verdict, with its sourcing trail."""

    implied_share_year_10: float | None
    tam: float | None
    sources: tuple[TamSource, ...]
    flags: tuple[str, ...]
    is_proxy: bool
    computable: bool

    def as_log_lines(self) -> list[str]:
        lines = [s.as_log_line() for s in self.sources]
        if self.implied_share_year_10 is not None:
            lines.append(
                f"TAM ceiling · implied year-10 share="
                f"{self.implied_share_year_10:.1%} tam={self.tam:,.0f}"
            )
        lines.extend(f"TAM flag · {f}" for f in self.flags)
        return lines


def assess_tam(
    data: CompanyData,
    config: Config,
    sources: Sequence[TamSource],
    year_10_revenue: float,
    *,
    industry_revenue: float | None = None,
    industry_cagr: float | None = None,
) -> TamAssessment:
    """B2.1 — the TAM ceiling test, with sourcing discipline.

    TAM estimation is at least as gameable as peer selection, and v3 gave it
    none of C2's discipline.  So: cited sources only, two of them, the lower of
    two that disagree by more than 50%, and a bounded proxy when no credible
    third-party figure exists — never an uncited number.
    """
    cfg = config.valuation
    flags: list[str] = []
    is_proxy = False
    tam: float | None = None
    used: tuple[TamSource, ...] = tuple(sources)

    cited = [s for s in sources if s.name and s.figure > 0]
    if len(cited) >= cfg.tam_min_sources:
        figures = sorted(s.figure for s in cited)
        low, high = figures[0], figures[-1]
        if low > 0 and (high - low) / low > cfg.tam_source_disagreement_flag:
            flags.append("TAM DISPUTED")
            tam = low
        else:
            tam = sum(figures) / len(figures)
    elif cited:
        flags.append(
            f"TAM UNDER-SOURCED — {len(cited)} source(s), {cfg.tam_min_sources} required"
        )
        tam = min(s.figure for s in cited)
    else:
        # Bounded fallback proxy.
        if industry_revenue is not None and industry_cagr is not None:
            tam = industry_revenue * (1.0 + industry_cagr) ** cfg.tam_proxy_years
            is_proxy = True
            used = (
                TamSource(
                    name="GICS industry aggregate proxy",
                    published=date.today(),
                    figure=tam,
                    method="proxy",
                ),
            )
            flags.append(
                "TAM: PROXY — treat the ceiling test as indicative only"
            )
        else:
            flags.append(
                "TAM NOT COMPUTABLE — no cited source and no industry aggregate; "
                "the ceiling test did not run"
            )
            return TamAssessment(None, None, used, tuple(flags), False, False)

    share = year_10_revenue / tam if tam and tam > 0 else None
    if share is not None and share > cfg.tam_share_ceiling:
        flags.append(
            f"TAM CEILING BREACHED — implied year-10 share {share:.1%} exceeds "
            f"{cfg.tam_share_ceiling:.0%}"
        )
    return TamAssessment(share, tam, used, tuple(flags), is_proxy, True)


def value_b2_growth(
    data: CompanyData,
    market: MarketData,
    config: Config,
    classification: Classification,
    ledger: AuditLedger | None = None,
    *,
    tam_sources: Sequence[TamSource] = (),
    industry_revenue: float | None = None,
    industry_cagr: float | None = None,
) -> FairValue:
    """Three-phase scenario-weighted DCF for CORE-GROWTH and SPEC-GROWTH.

    The bear case must model the growth thesis *failing*, not merely slowing —
    so it is built as a sharp fade to terminal within phase 1 rather than as a
    haircut on the base rate.
    """
    if classification not in (Classification.CORE_GROWTH, Classification.SPEC_GROWTH):
        raise MethodRefused(f"B2 does not apply to {classification.value}")

    cfg = config.valuation
    annual = data.trailing_years(3)
    cagr_2y = _trailing_growth(annual, 2)
    if cagr_2y is None:
        raise DataUnavailable(
            "revenue_history", "B2 needs a trailing 2-year revenue CAGR"
        )

    latest = data.latest_annual
    if latest is None or not latest.revenue:
        raise DataUnavailable("revenue", "B2 needs a base-year revenue")
    base_revenue = latest.revenue

    base_fcf = _fcf(latest)
    # SPEC-GROWTH is FCF-negative by definition, so the DCF is built on a
    # margin the company is assumed to reach, not on today's cash flow.
    terminal_margin = _terminal_fcf_margin(data, classification)
    if base_fcf is None and terminal_margin is None:
        raise DataUnavailable(
            "free_cash_flow",
            "B2 needs either a base free cash flow or a terminal margin assumption",
        )

    discount = build_discount_rate(data, market, config, classification)
    terminal = cfg.terminal_growth_cap
    # Phase 1 growth is capped at 1.5x the trailing 2-year CAGR, and floored at
    # terminal so a decelerating name cannot produce a negative-growth path that
    # the scenario weights would then treat as a base case.
    capped = max(cagr_2y * cfg.b2_growth_cap_multiple, terminal)

    scenario_paths = {
        # Bear: the thesis fails.  Growth collapses to terminal inside phase 1
        # and never recovers.
        "bear": _linear_fade(max(capped * 0.25, terminal), terminal, cfg.b2_phase2_end_year)
        + [terminal] * (cfg.b1_projection_years - cfg.b2_phase2_end_year),
        "base": [capped] * cfg.b2_phase1_years
        + _linear_fade(capped, terminal, cfg.b2_phase2_end_year - cfg.b2_phase1_years)
        + [terminal] * (cfg.b1_projection_years - cfg.b2_phase2_end_year),
        "bull": [capped * 1.15] * cfg.b2_phase1_years
        + _linear_fade(
            capped * 1.15, terminal, cfg.b2_phase2_end_year - cfg.b2_phase1_years
        )
        + [terminal] * (cfg.b1_projection_years - cfg.b2_phase2_end_year),
    }

    scenarios: list[ScenarioResult] = []
    weighted_per_share = 0.0
    year_10_revenue_base = base_revenue

    for name, path in scenario_paths.items():
        weight = cfg.scenario_weights[name]
        revenue = base_revenue
        for growth in path:
            revenue *= 1.0 + growth
        if name == "base":
            year_10_revenue_base = revenue

        if terminal_margin is not None:
            # Value the terminal cash flow the revenue path implies.
            start_cf = base_revenue * terminal_margin
        else:
            start_cf = base_fcf  # type: ignore[assignment]
        if start_cf is None or start_cf <= 0:
            raise MethodRefused(
                f"{data.profile.symbol}: no positive base cash flow and no "
                "terminal margin; B2 refuses rather than approximating"
            )

        enterprise, tv_share = _discounted_dcf(
            start_cf, path, terminal, discount.rate
        )
        equity, per_share = _equity_per_share(enterprise, data)
        scenarios.append(
            ScenarioResult(
                name=name,
                weight=weight,
                equity_value=equity,
                per_share=per_share,
                growth_rate=path[0],
                terminal_value_share=tv_share,
            )
        )
        weighted_per_share += weight * per_share

    tam = assess_tam(
        data,
        config,
        tam_sources,
        year_10_revenue_base,
        industry_revenue=industry_revenue,
        industry_cagr=industry_cagr,
    )

    flags = list(discount.flags) + list(tam.flags)

    cross_check: dict[str, float] = {}
    if data.profile.market_cap and base_revenue:
        latest_q = data.latest_quarter or latest
        net_debt = latest_q.net_debt if latest_q else None
        ev = data.profile.market_cap + (net_debt or 0.0)
        ev_rev = ev / base_revenue
        cross_check["ev_revenue"] = ev_rev
        if cagr_2y > 0:
            # Growth-adjusted: EV/Revenue per point of growth.
            cross_check["ev_revenue_growth_adjusted"] = ev_rev / (cagr_2y * 100.0)

    result = FairValue(
        symbol=data.profile.symbol,
        classification=classification,
        method="B2 three-phase scenario-weighted DCF",
        fair_value_per_share=weighted_per_share,
        currency=data.profile.reporting_currency,
        discount_rate=discount,
        scenarios=scenarios,
        cross_check=cross_check,
        flags=flags,
        diagnostics={
            "trailing_2y_cagr": cagr_2y,
            "capped_phase1_growth": capped,
            "terminal_fcf_margin": terminal_margin,
            "year_10_revenue_base_case": year_10_revenue_base,
            "tam": tam,
        },
    )
    if ledger is not None:
        ledger.note(f"B2 · {result.as_log_line()}")
        ledger.note(f"B2 discount · {discount.as_log_line()}")
        for line in tam.as_log_lines():
            ledger.note(f"B2.1 · {line}")
        for s in scenarios:
            ledger.note(
                f"B2 scenario {s.name} w={s.weight:.0%} "
                f"per_share={s.per_share:,.2f} g1={s.growth_rate:.1%} "
                f"tv_share={s.terminal_value_share:.0%}"
            )
    return result


def _terminal_fcf_margin(
    data: CompanyData, classification: Classification
) -> float | None:
    """The mature FCF margin a growth name is assumed to reach.

    Uses the best margin actually achieved in the available history rather than
    an assumed industry number, so the assumption is at least anchored to
    something the company has done.  Returns ``None`` for a company already
    generating cash, where today's FCF is the better base.
    """
    rows = data.trailing_years(5)
    if not rows:
        return None
    margins = []
    for row in rows:
        fcf = _fcf(row)
        if fcf is None or not row.revenue or row.revenue <= 0:
            continue
        margins.append(fcf / row.revenue)
    if not margins:
        return None
    best = max(margins)
    if best > 0 and classification is Classification.CORE_GROWTH:
        return None  # already cash-generative; use reported FCF
    if best <= 0:
        # Never achieved a positive margin.  Refuse to invent one.
        return None
    return best


# -- B3 / B4 / B5 ---------------------------------------------------------


def value_b3_bank(
    data: CompanyData,
    market: MarketData,
    config: Config,
    ledger: AuditLedger | None = None,
    *,
    sector_price_to_book: float | None = None,
) -> FairValue:
    """P/B and P/TBV against sector, plus ROE sustainability.

    Standard DCF is banned: a bank's revenue and cost structure does not map
    onto a normal cash-flow build.
    """
    latest = data.latest_quarter or data.latest_annual
    if latest is None:
        raise DataUnavailable("financials", "B3 needs a balance sheet")
    shares = latest.shares_diluted or latest.shares_outstanding
    if not shares:
        raise DataUnavailable("shares_diluted", "B3 needs a share count")
    if latest.total_equity is None:
        raise DataUnavailable("total_equity", "B3 needs shareholders' equity")
    if latest.total_equity <= 0:
        raise MethodRefused(
            f"{data.profile.symbol}: negative book value; P/B is meaningless"
        )
    if sector_price_to_book is None:
        raise DataUnavailable(
            "sector_price_to_book", "B3 values against the sector multiple"
        )

    book_per_share = latest.total_equity / shares
    tbv = latest.tangible_book_value
    tbv_per_share = tbv / shares if tbv is not None else None

    fair_value = book_per_share * sector_price_to_book

    flags: list[str] = []
    cross_check: dict[str, float] = {"book_value_per_share": book_per_share}
    if tbv_per_share is not None:
        cross_check["tangible_book_per_share"] = tbv_per_share
        cross_check["p_tbv_implied"] = fair_value / tbv_per_share
    else:
        flags.append("TANGIBLE BOOK UNAVAILABLE — P/TBV cross-check did not run")

    roe, roe_10y = _roe_and_history(data)
    if roe is not None:
        cross_check["roe_current"] = roe
    if roe is not None and roe_10y is not None:
        cross_check["roe_10y_average"] = roe_10y
        gap = roe - roe_10y
        cross_check["roe_gap"] = gap
        if abs(gap) > 0.02:
            flags.append(
                f"ROE SUSTAINABILITY — current {roe:.1%} vs 10-year average "
                f"{roe_10y:.1%}; the reason for the gap must be stated"
            )
    else:
        flags.append(
            "ROE SUSTAINABILITY NOT ASSESSED — 10-year ROE history unavailable"
        )

    result = FairValue(
        symbol=data.profile.symbol,
        classification=Classification.FINANCIAL_BANK,
        method="B3 P/B and P/TBV vs sector",
        fair_value_per_share=fair_value,
        currency=data.profile.reporting_currency,
        cross_check=cross_check,
        flags=flags,
        diagnostics={"sector_price_to_book": sector_price_to_book},
    )
    if ledger is not None:
        ledger.note(f"B3 · {result.as_log_line()}")
    return result


def _roe_and_history(data: CompanyData) -> tuple[float | None, float | None]:
    rows = data.trailing_years(10)
    if not rows:
        return None, None
    def _roe(row: PeriodFinancials) -> float | None:
        if row.net_income is None or not row.total_equity or row.total_equity <= 0:
            return None
        return row.net_income / row.total_equity

    current = _roe(rows[0])
    history = [r for r in (_roe(row) for row in rows) if r is not None]
    if len(history) < 5:
        return current, None
    return current, sum(history) / len(history)


def value_b4_reit(
    data: CompanyData,
    market: MarketData,
    config: Config,
    ledger: AuditLedger | None = None,
    *,
    peer_price_to_affo: float | None = None,
) -> FairValue:
    """P/AFFO (primary) and P/FFO against peers.

    P/E is banned: depreciation distorts REIT earnings enough to make it
    meaningless.
    """
    latest = data.latest_annual or data.latest_quarter
    if latest is None:
        raise DataUnavailable("financials", "B4 needs a reporting period")
    shares = latest.shares_diluted or latest.shares_outstanding
    if not shares:
        raise DataUnavailable("shares_diluted", "B4 needs a share count")

    affo = latest.adjusted_funds_from_operations
    ffo = latest.funds_from_operations
    if affo is None and ffo is None:
        raise DataUnavailable(
            "adjusted_funds_from_operations",
            "B4 needs AFFO or FFO; a REIT cannot be valued on earnings and "
            "P/E is banned on this path",
        )
    if peer_price_to_affo is None:
        raise DataUnavailable(
            "peer_price_to_affo", "B4 values against the peer AFFO multiple"
        )

    flags: list[str] = []
    primary = affo
    basis = "AFFO"
    if primary is None:
        primary = ffo
        basis = "FFO"
        flags.append("AFFO UNAVAILABLE — valued on FFO, the secondary measure")

    per_share = primary / shares
    if per_share <= 0:
        raise MethodRefused(
            f"{data.profile.symbol}: {basis} per share is non-positive"
        )
    fair_value = per_share * peer_price_to_affo

    cross_check: dict[str, float] = {f"{basis.lower()}_per_share": per_share}
    if ffo is not None and affo is not None:
        cross_check["ffo_per_share"] = ffo / shares

    # Implied cap rate against the prevailing sector environment.
    sector_cap = market.sector_cap_rates.get(data.profile.sector or "")
    implied_cap = _implied_cap_rate(data, latest)
    if implied_cap is not None:
        cross_check["implied_cap_rate"] = implied_cap
        if sector_cap is not None:
            cross_check["sector_cap_rate"] = sector_cap
            if abs(implied_cap - sector_cap) > 0.01:
                flags.append(
                    f"CAP RATE GAP — implied {implied_cap:.2%} vs sector "
                    f"{sector_cap:.2%}"
                )
        else:
            flags.append("SECTOR CAP RATE UNAVAILABLE — cross-check did not run")
    else:
        flags.append("IMPLIED CAP RATE NOT COMPUTABLE")

    result = FairValue(
        symbol=data.profile.symbol,
        classification=Classification.REIT,
        method=f"B4 P/{basis} vs peers",
        fair_value_per_share=fair_value,
        currency=data.profile.reporting_currency,
        cross_check=cross_check,
        flags=flags,
        diagnostics={"peer_price_to_affo": peer_price_to_affo, "basis": basis},
    )
    if ledger is not None:
        ledger.note(f"B4 · {result.as_log_line()}")
    return result


def _implied_cap_rate(
    data: CompanyData, latest: PeriodFinancials
) -> float | None:
    """Operating income over enterprise value, as a standing-in NOI yield."""
    if latest.operating_income is None or not data.profile.market_cap:
        return None
    net_debt = latest.net_debt
    if net_debt is None:
        return None
    ev = data.profile.market_cap + net_debt
    if ev <= 0:
        return None
    return latest.operating_income / ev


def value_b5_insurer(
    data: CompanyData,
    market: MarketData,
    config: Config,
    ledger: AuditLedger | None = None,
    *,
    sector_price_to_book: float | None = None,
) -> FairValue:
    """Combined ratio trend plus P/B against sector.

    Operating ROE excluding unrealised investment gains is the primary quality
    read — the same distortion A3 exists to catch.
    """
    latest = data.latest_quarter or data.latest_annual
    if latest is None:
        raise DataUnavailable("financials", "B5 needs a reporting period")
    shares = latest.shares_diluted or latest.shares_outstanding
    if not shares:
        raise DataUnavailable("shares_diluted", "B5 needs a share count")
    if latest.total_equity is None or latest.total_equity <= 0:
        raise MethodRefused(
            f"{data.profile.symbol}: non-positive book value; P/B is meaningless"
        )
    if sector_price_to_book is None:
        raise DataUnavailable(
            "sector_price_to_book", "B5 values against the sector multiple"
        )

    book_per_share = latest.total_equity / shares
    fair_value = book_per_share * sector_price_to_book

    flags: list[str] = []
    cross_check: dict[str, float] = {"book_value_per_share": book_per_share}

    ratios = [
        r.combined_ratio
        for r in data.trailing_years(5)
        if r.combined_ratio is not None
    ]
    if ratios:
        cross_check["combined_ratio_latest"] = ratios[0]
        cross_check["combined_ratio_5y_mean"] = sum(ratios) / len(ratios)
        if ratios[0] >= 100.0:
            flags.append(
                f"COMBINED RATIO {ratios[0]:.1f} — underwriting loss, not profit"
            )
        if len(ratios) >= 3 and ratios[0] > ratios[-1]:
            flags.append("COMBINED RATIO DETERIORATING over the available window")
    else:
        flags.append(
            "COMBINED RATIO UNAVAILABLE — the primary underwriting read did not run"
        )

    result = FairValue(
        symbol=data.profile.symbol,
        classification=Classification.INSURER,
        method="B5 combined ratio + P/B vs sector",
        fair_value_per_share=fair_value,
        currency=data.profile.reporting_currency,
        cross_check=cross_check,
        flags=flags,
        diagnostics={"sector_price_to_book": sector_price_to_book},
    )
    if ledger is not None:
        ledger.note(f"B5 · {result.as_log_line()}")
    return result


# -- dispatcher -----------------------------------------------------------

#: Which method each classification is allowed to use.  The dispatcher will
#: not route around this table.
METHOD_FOR: dict[Classification, str] = {
    Classification.CORE_STABLE: "B1",
    Classification.CORE_GROWTH: "B2",
    Classification.SPEC_GROWTH: "B2",
    Classification.FINANCIAL_BANK: "B3",
    Classification.REIT: "B4",
    Classification.INSURER: "B5",
}


def value(
    data: CompanyData,
    market: MarketData,
    config: Config,
    classification: Classification,
    ledger: AuditLedger | None = None,
    **kwargs,
) -> FairValue:
    """Route to the one method that fits this classification."""
    method = METHOD_FOR[classification]
    if method == "B1":
        return value_b1_core_stable(data, market, config, ledger)
    if method == "B2":
        return value_b2_growth(
            data, market, config, classification, ledger,
            tam_sources=kwargs.get("tam_sources", ()),
            industry_revenue=kwargs.get("industry_revenue"),
            industry_cagr=kwargs.get("industry_cagr"),
        )
    if method == "B3":
        return value_b3_bank(
            data, market, config, ledger,
            sector_price_to_book=kwargs.get("sector_price_to_book"),
        )
    if method == "B4":
        return value_b4_reit(
            data, market, config, ledger,
            peer_price_to_affo=kwargs.get("peer_price_to_affo"),
        )
    if method == "B5":
        return value_b5_insurer(
            data, market, config, ledger,
            sector_price_to_book=kwargs.get("sector_price_to_book"),
        )
    raise MethodRefused(f"no method registered for {classification.value}")


__all__ = [
    "FairValue",
    "ScenarioResult",
    "TamAssessment",
    "MethodRefused",
    "METHOD_FOR",
    "value",
    "value_b1_core_stable",
    "value_b2_growth",
    "value_b3_bank",
    "value_b4_reit",
    "value_b5_insurer",
    "assess_tam",
]
