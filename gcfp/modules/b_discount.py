"""The discount rate, fully pinned (fixes v3 flaw 4).

v3 said "discount rate" and left the reader to guess at a risk-free source, an
equity risk premium, and a beta.  Three unpinned inputs multiply into a fair
value that can be moved anywhere its author wants, which is the opposite of an
auditable system.  So::

    cost_of_equity = risk_free_rate + (beta x equity_risk_premium)
    risk_free_rate = current 10-year US Treasury yield (^TNX), live, dated
    equity_risk_premium = 5.0% default; sweep range 4.5-5.5%
    beta = 5-year monthly beta vs. S&P 500, from the data provider
           if unavailable: GICS sub-industry median beta, flag "BETA: PROXY"
           if that is also unavailable: fail A5 — do not assume 1.0
    WACC = (E/V x cost_of_equity) + (D/V x after-tax cost of debt)
           cost of debt = interest expense / average total debt
           if not computable: use cost_of_equity, flag it
    absolute floor = 9.0%, applied after all of the above

Assuming beta = 1.0 is the specific temptation this refuses.  A missing beta is
a data gap, and A5 already says what to do with one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from ..classification import Classification
from ..config import Config
from ..data.adapter import DataUnavailable
from ..types import CompanyData, MarketData, PeriodFinancials


@dataclass(frozen=True)
class DiscountRate:
    """A discount rate and every input that produced it."""

    rate: float
    cost_of_equity: float
    risk_free_rate: float
    beta: float
    equity_risk_premium: float
    beta_is_proxy: bool
    cost_of_debt: float | None
    after_tax_cost_of_debt: float | None
    equity_weight: float
    debt_weight: float
    floor_applied: float
    floor_binding: bool
    method: str
    flags: tuple[str, ...] = ()

    def as_log_line(self) -> str:
        bits = [
            f"discount_rate={self.rate:.2%}",
            f"method={self.method}",
            f"rf={self.risk_free_rate:.2%}",
            f"beta={self.beta:.3f}" + (" (PROXY)" if self.beta_is_proxy else ""),
            f"erp={self.equity_risk_premium:.2%}",
            f"cost_of_equity={self.cost_of_equity:.2%}",
        ]
        if self.after_tax_cost_of_debt is not None:
            bits.append(f"after_tax_kd={self.after_tax_cost_of_debt:.2%}")
            bits.append(f"E/V={self.equity_weight:.2f} D/V={self.debt_weight:.2f}")
        bits.append(
            f"floor={self.floor_applied:.2%}"
            + (" BINDING" if self.floor_binding else "")
        )
        if self.flags:
            bits.append("flags=" + ",".join(self.flags))
        return " · ".join(bits)


def classification_floor(classification: Classification, config: Config) -> float:
    """The floor that applies to this path.

    B2's floors are higher than B1's and are never lowered to make a name pass.
    """
    dr = config.discount_rate
    if classification is Classification.SPEC_GROWTH:
        return dr.spec_growth_floor
    if classification is Classification.CORE_GROWTH:
        return dr.core_growth_floor
    return dr.absolute_floor


def _average_total_debt(rows: Sequence[PeriodFinancials]) -> float | None:
    values = [r.total_debt for r in rows if r.total_debt is not None]
    if len(values) < 2:
        return None
    return sum(values[:2]) / 2.0


def _effective_tax_rate(row: PeriodFinancials | None, default: float) -> float:
    if row is None or row.tax_expense is None or not row.pretax_income:
        return default
    if row.pretax_income <= 0:
        return default
    rate = row.tax_expense / row.pretax_income
    # Guard against one-off credits and settlements producing a nonsense rate.
    return rate if 0.0 <= rate <= 0.60 else default


def build_discount_rate(
    data: CompanyData,
    market: MarketData,
    config: Config,
    classification: Classification,
    *,
    equity_risk_premium: float | None = None,
) -> DiscountRate:
    """Build the rate, or raise :class:`DataUnavailable` if beta cannot be had.

    ``equity_risk_premium`` overrides the default so the validation protocol's
    4.5-5.5% sweep can be run without touching config.
    """
    dr_cfg = config.discount_rate
    erp = equity_risk_premium if equity_risk_premium is not None else dr_cfg.equity_risk_premium
    flags: list[str] = []

    rf = market.risk_free_rate
    if rf is None:
        raise DataUnavailable(
            "risk_free_rate",
            "10-year Treasury yield (^TNX) not available; B1/B2 cannot be built",
        )

    profile = data.profile
    beta = profile.beta
    beta_is_proxy = profile.beta_is_proxy
    if beta is None:
        group = profile.gics_sub_industry_code or profile.sub_industry or profile.industry
        beta = market.group_beta_median.get(group) if group else None
        if beta is not None:
            beta_is_proxy = True
            flags.append("BETA: PROXY")
    if beta is None:
        # Never assume 1.0.  This is an A5 data-integrity failure.
        raise DataUnavailable(
            "beta",
            "no name-level beta and no group median beta; A5 data-integrity "
            "failure — 1.0 is not assumed",
        )

    cost_of_equity = rf + beta * erp

    annual = data.trailing_years(2)
    latest = data.latest_annual
    avg_debt = _average_total_debt(annual)
    interest = latest.interest_expense if latest else None
    cost_of_debt = None
    after_tax_kd = None
    equity_weight, debt_weight = 1.0, 0.0
    method = "cost_of_equity"

    market_cap = profile.market_cap
    total_debt = latest.total_debt if latest else None

    if (
        interest is not None
        and avg_debt
        and avg_debt > 0
        and market_cap
        and total_debt is not None
        and (market_cap + total_debt) > 0
    ):
        cost_of_debt = abs(interest) / avg_debt
        tax_rate = _effective_tax_rate(latest, dr_cfg.default_tax_rate)
        after_tax_kd = cost_of_debt * (1.0 - tax_rate)
        enterprise = market_cap + total_debt
        equity_weight = market_cap / enterprise
        debt_weight = total_debt / enterprise
        rate = equity_weight * cost_of_equity + debt_weight * after_tax_kd
        method = "wacc"
    else:
        rate = cost_of_equity
        flags.append("COST OF DEBT NOT COMPUTABLE — using cost of equity")

    floor = classification_floor(classification, config)
    floor_binding = rate < floor
    if floor_binding:
        rate = floor

    return DiscountRate(
        rate=rate,
        cost_of_equity=cost_of_equity,
        risk_free_rate=rf,
        beta=beta,
        equity_risk_premium=erp,
        beta_is_proxy=beta_is_proxy,
        cost_of_debt=cost_of_debt,
        after_tax_cost_of_debt=after_tax_kd,
        equity_weight=equity_weight,
        debt_weight=debt_weight,
        floor_applied=floor,
        floor_binding=floor_binding,
        method=method,
        flags=tuple(flags),
    )


def erp_sweep(
    data: CompanyData,
    market: MarketData,
    config: Config,
    classification: Classification,
) -> list[DiscountRate]:
    """The 4.5-5.5% ERP sweep the validation protocol requires."""
    dr = config.discount_rate
    return [
        build_discount_rate(
            data, market, config, classification, equity_risk_premium=erp
        )
        for erp in (dr.erp_sweep_low, dr.equity_risk_premium, dr.erp_sweep_high)
    ]


__all__ = [
    "DiscountRate",
    "build_discount_rate",
    "classification_floor",
    "erp_sweep",
]
