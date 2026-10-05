"""Fakes mimicking QuantConnect's Fundamental surface.

Modelled on the published quantconnect-stubs API, in particular
``MultiPeriodField``'s ``has_period_value`` / ``has_value`` semantics -- QC
returns 0.0 for an absent figure, and those two methods are the only thing
separating "reported zero" from "not reported". The fake reproduces that trap
faithfully, because an adapter that reads through it is the bug worth testing
for.
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, Optional


class FakeMultiPeriodField:
    def __init__(self, periods: Optional[dict[str, Any]] = None, default: Any = None):
        self._periods = dict(periods or {})
        self._default = default

    def has_period_value(self, period: str) -> bool:
        return period in self._periods

    def get_period_value(self, period: str) -> Any:
        return self._periods[period]

    @property
    def has_value(self) -> bool:
        return self._default is not None

    @property
    def value(self) -> Any:
        # QC hands back 0.0 rather than None when nothing was reported.
        return self._default if self._default is not None else 0.0


def mpf(annual=None, quarterly=None, default=None):
    periods = {}
    if annual is not None:
        periods["12M"] = annual
    if quarterly is not None:
        periods["3M"] = quarterly
    return FakeMultiPeriodField(periods, default)


def absent():
    """A field QC reports as 0.0 with no value behind it."""
    return FakeMultiPeriodField({}, None)


def make_fundamental(
    symbol: str = "TEST",
    end_time: date = date(2026, 6, 30),
    period_end: date = date(2025, 12, 31),
    market_cap: float = 6_000_000_000.0,
    price: float = 70.0,
    dollar_volume: float = 40_000_000.0,
    revenue: float = 9_000_000_000.0,
    net_income: float = 1_080_000_000.0,
    sector_code: int = 310,
    industry_group: int = 31052,
    template: str = "N",
    sic: str = "3531",
    is_reit: bool = False,
    auditor: str = "Auditor A",
    pe_ratio: float = 6.3,
    shares: float = 95_000_000.0,
    recurring_capex_present: bool = False,
    delisting_date: Any = None,
):
    income = SimpleNamespace(
        total_revenue=mpf(revenue, revenue / 4),
        operating_revenue=mpf(revenue, revenue / 4),
        gross_profit=mpf(revenue * 0.45, revenue * 0.45 / 4),
        operating_income=mpf(revenue * 0.16, revenue * 0.16 / 4),
        ebitda=mpf(revenue * 0.20, revenue * 0.20 / 4),
        depreciation_amortization_depletion=mpf(revenue * 0.05, revenue * 0.05 / 4),
        net_income=mpf(net_income, net_income / 4),
        interest_expense=mpf(revenue * 0.01),
        tax_provision=mpf(net_income * 0.26),
        pretax_income=mpf(net_income * 1.26),
        depreciation_and_amortization=mpf(revenue * 0.05),
        net_interest_income=absent(),
        total_premiums_earned=absent(),
        policyholder_benefits_gross=absent(),
        underwriting_expenses=absent(),
        net_investment_income=absent(),
        unrealized_gain_loss=absent(),
        gain_on_sale_of_ppe=mpf(revenue * 0.01),
        gain_loss_on_sale_of_business=absent(),
        normalized_ebitda=absent(),
    )
    # Balance-sheet lines are stocks: reported at every period end, and not
    # scaled down for a shorter period the way a flow is.
    def stock(value):
        return mpf(value, value)

    balance = SimpleNamespace(
        current_assets=stock(revenue * 0.45),
        current_liabilities=stock(revenue * 0.25),
        cash_and_cash_equivalents=stock(revenue * 0.20),
        available_for_sale_securities=stock(revenue * 0.05),
        total_debt=stock(revenue * 0.30),
        total_assets=stock(revenue * 1.6),
        total_liabilities_net_minority_interest=stock(revenue * 0.8),
        stockholders_equity=stock(revenue * 0.8),
        goodwill_and_other_intangible_assets=stock(revenue * 0.1),
        tangible_book_value=stock(revenue * 0.7),
    )
    cash_flow = SimpleNamespace(
        operating_cash_flow=mpf(net_income * 1.2, net_income * 1.2 / 4),
        capital_expenditure=mpf(-revenue * 0.04, -revenue * 0.04 / 4),
        free_cash_flow=mpf(net_income * 1.2 - revenue * 0.04),
        cash_dividends_paid=mpf(-revenue * 0.02),
        repurchase_of_capital_stock=mpf(-revenue * 0.01),
        depreciation_amortization_depletion=mpf(revenue * 0.05),
        maintenance_capital_expenditure=mpf(-revenue * 0.02) if recurring_capex_present else absent(),
    )
    statements = SimpleNamespace(
        income_statement=income,
        balance_sheet=balance,
        cash_flow_statement=cash_flow,
        period_ending_date=FakeMultiPeriodField(
            {"12M": datetime(period_end.year, period_end.month, period_end.day),
             "3M": datetime(period_end.year, period_end.month, period_end.day)}
        ),
        file_date=FakeMultiPeriodField({"12M": datetime(period_end.year, period_end.month, period_end.day)}),
        period_auditor=FakeMultiPeriodField({"12M": auditor}),
        auditor_report_status=FakeMultiPeriodField({"12M": "Unqualified"}),
    )
    earnings = SimpleNamespace(
        diluted_average_shares=mpf(shares, shares),
        basic_average_shares=mpf(shares),
        diluted_eps=mpf(net_income / shares),
        dividend_per_share=mpf(1.80),
        period_ending_date=FakeMultiPeriodField({"12M": datetime(period_end.year, period_end.month, period_end.day)}),
    )
    return SimpleNamespace(
        symbol=symbol,
        end_time=datetime(end_time.year, end_time.month, end_time.day),
        market_cap=market_cap,
        price=price,
        value=price,
        dollar_volume=dollar_volume,
        has_fundamental_data=True,
        financial_statements=statements,
        earning_reports=earnings,
        valuation_ratios=SimpleNamespace(
            pe_ratio=pe_ratio,
            forward_pe_ratio=pe_ratio * 0.9,
            pb_ratio=1.2,
            ev_to_revenue=1.1,
            ev_to_ebitda=6.0,
            pcf_ratio=8.0,
            peg_ratio=0.8,
            book_value_per_share=revenue * 0.8 / shares,
            tangible_book_value_per_share=revenue * 0.7 / shares,
        ),
        operation_ratios=SimpleNamespace(roe=mpf(0.18), revenue_growth=mpf(0.07)),
        company_reference=SimpleNamespace(
            standard_name=f"{symbol} Inc",
            short_name=symbol,
            country_id="USA",
            industry_template_code=template,
            is_reit=is_reit,
            auditor=auditor,
            cik="0000018230",
        ),
        security_reference=SimpleNamespace(
            exchange_id="NYS",
            currency_id="USD",
            is_primary_share=True,
            is_depositary_receipt=False,
            ipo_date=datetime(1990, 1, 1),
            delisting_date=delisting_date,
            security_type="ST00000001",
        ),
        asset_classification=SimpleNamespace(
            morningstar_sector_code=sector_code,
            morningstar_industry_group_code=industry_group,
            sic=sic,
        ),
    )
