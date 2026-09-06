"""Shared fixtures.

Values are chosen to sit just either side of the thresholds under test, so a
test that passes proves the gate discriminates rather than that the number
happened to be large enough.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from gcfp.config import DEFAULT_CONFIG, Config
from gcfp.data.fixtures import (
    FixtureAdapter,
    FixtureCompany,
    make_annuals,
    make_multiple_series,
    make_prices,
    make_quarters,
)
from gcfp.types import CompanyProfile, MarketData, ReportingFrequency, TaxonomyLevel


@pytest.fixture(scope="session")
def config() -> Config:
    """Immutable, so one instance serves the whole run.

    Tests that need a variant use ``dataclasses.replace`` rather than mutating
    this — Config is frozen, so an accidental mutation would fail loudly."""
    return DEFAULT_CONFIG


@pytest.fixture(scope="session")
def market() -> MarketData:
    return MarketData(
        risk_free_rate=0.042,
        risk_free_rate_date=date.today(),
        fx_rates={"USDSGD": 1.29},
        group_net_debt_ebitda_median={"Machinery": 1.8},
        group_member_counts={"Machinery": 22},
        group_gross_margin_stdev_median={"Machinery": 0.020},
        sector_cap_rates={"Real Estate": 0.058},
    )


def stable_profile(**kw) -> CompanyProfile:
    defaults = dict(
        symbol="STABLECO",
        name="Stable Industrial",
        market_cap=80e9,
        adv_3m_usd=300e6,
        beta=1.05,
        sector="Industrials",
        industry="Machinery",
        taxonomy_level=TaxonomyLevel.INDUSTRY,
        exchange="NYSE",
        reporting_frequency=ReportingFrequency.QUARTERLY,
        last_report_date=date.today() - timedelta(days=30),
    )
    defaults.update(kw)
    return CompanyProfile(**defaults)


BALANCE = dict(
    total_current_assets=20e9,
    total_current_liabilities=14e9,
    total_debt=12e9,
    cash_and_equivalents=4e9,
    total_equity=18e9,
)


def build_company(
    *,
    profile: CompanyProfile | None = None,
    price: float = 150.0,
    annual_kw: dict | None = None,
    quarterly_kw: dict | None = None,
    multiple: str = "trailing_pe",
    multiple_values: list[float] | None = None,
    corporate_actions: list | None = None,
    **company_kw,
):
    """A company that clears Module A comfortably, with knobs for each gate."""
    profile = profile or stable_profile()
    annual_defaults = dict(
        revenue=40e9,
        net_income=5e9,
        operating_cash_flow=7e9,
        free_cash_flow=5e9,
        revenue_growth=0.05,
        interest_expense=500e6,
        tax_expense=1.2e9,
        pretax_income=6.2e9,
        shares_diluted=500e6,
        operating_income=6.5e9,
        invested_capital=26e9,
        capital_expenditure=-2e9,
        **BALANCE,
    )
    annual_defaults.update(annual_kw or {})

    quarterly_defaults = dict(
        revenue=10e9,
        net_income=1.25e9,
        operating_cash_flow=1.75e9,
        shares=500e6,
        share_growth=-0.025,
        ebitda=2.0e9,
        gross_profit=3.1e9,
        **BALANCE,
    )
    quarterly_defaults.update(quarterly_kw or {})

    values = multiple_values or [
        20, 21, 19, 22, 18, 20, 21, 19, 20, 22, 18, 21, 19, 20, 21,
        20, 19, 22, 20, 18, 21, 19, 20, 20, 21, 19, 20, 22, 19, 21,
    ]
    # Period counts are knobs too: a filer with no quarterly reports at all
    # (a 20-F foreign private issuer, say) is a case the gates must survive.
    n_annual = annual_defaults.pop("count", 8)
    n_quarters = quarterly_defaults.pop("count", 12)
    fixture = FixtureCompany(
        profile=profile,
        annual=make_annuals(n_annual, **annual_defaults),
        quarterly=make_quarters(n_quarters, **quarterly_defaults),
        prices=make_prices(500, start_price=price, daily_drift=0.0004),
        multiples={multiple: make_multiple_series(multiple, values)},
        corporate_actions=corporate_actions or [],
        **company_kw,
    )
    adapter = FixtureAdapter().add(fixture)
    return adapter.load_company(profile.symbol, multiple=multiple)


@pytest.fixture
def healthy_company():
    return build_company()
