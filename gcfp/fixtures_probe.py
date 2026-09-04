"""A fixture set shaped like a real retail data source.

The §18 probe is only useful if it has been run against something before it is
trusted against live data.  This builds the ten targets §18 names, with the
coverage profile a typical retail equity API actually offers — which is not
uniform, and that non-uniformity is the whole finding:

* fundamentals and prices are broadly available for large US names;
* REIT AFFO, insurer combined ratios, and bank tangible book are the fields
  free and low tier sources most often omit, and they are exactly what B3/B4/B5
  cannot run without;
* a delisted name is usually absent entirely, which is survivorship bias
  arriving as a 404 rather than as a caveat;
* GICS codes are behind a separate, expensive licence, so vendors ship their
  own taxonomy.

Numbers here are illustrative and are not claimed to be any company's real
financials.  The point is the *shape* of the coverage, not the values.
"""

from __future__ import annotations

from datetime import date, timedelta

from .data.fixtures import (
    FixtureAdapter,
    FixtureCompany,
    make_annuals,
    make_multiple_series,
    make_prices,
    make_quarters,
)
from .types import (
    CompanyProfile,
    CorporateAction,
    CorporateActionType,
    MarketData,
    ReportingFrequency,
    TaxonomyLevel,
)

_TODAY = date.today()


def _profile(symbol: str, name: str, **kw) -> CompanyProfile:
    """A profile shaped like a vendor's: an industry string, never a GICS code."""
    defaults = dict(
        listing_currency="USD",
        reporting_currency="USD",
        exchange="NYSE",
        country="US",
        taxonomy_level=TaxonomyLevel.INDUSTRY,
        taxonomy_is_gics=False,
        reporting_frequency=ReportingFrequency.QUARTERLY,
        last_report_date=_TODAY - timedelta(days=35),
    )
    defaults.update(kw)
    return CompanyProfile(symbol=symbol, name=name, **defaults)


def _peer(symbol: str, industry: str, sector: str, market_cap: float,
          growth: float, pe: float, exchange: str = "NYSE") -> FixtureCompany:
    """A peer with just enough substance for C2 to screen it honestly.

    Market cap, revenue growth, and a computable trailing multiple are exactly
    the three things C2's screen reads; a peer fixture that omitted any of them
    would be rejected for a reason that says more about the fixture than about
    the data source.
    """
    shares = 500e6
    net_income = market_cap / pe
    revenue = net_income * 8.0
    price = market_cap / shares
    return FixtureCompany(
        profile=_profile(symbol, f"{symbol} (peer)", market_cap=market_cap,
                         adv_3m_usd=200e6, beta=1.1, sector=sector,
                         industry=industry, exchange=exchange),
        annual=make_annuals(3, revenue=revenue, net_income=net_income,
                            operating_cash_flow=net_income * 1.3,
                            free_cash_flow=net_income * 0.9,
                            revenue_growth=growth, shares_diluted=shares,
                            total_debt=market_cap * 0.15,
                            cash_and_equivalents=market_cap * 0.05,
                            total_equity=market_cap * 0.3),
        quarterly=make_quarters(4, revenue=revenue / 4, net_income=net_income / 4,
                                operating_cash_flow=net_income * 0.33, shares=shares,
                                total_debt=market_cap * 0.15,
                                cash_and_equivalents=market_cap * 0.05,
                                total_equity=market_cap * 0.3),
        prices=make_prices(10, start_price=price),
    )


#: Peer groups for each probe target, sized and grown to sit inside C2's
#: screen — except TPL's, which deliberately do not, because a genuine
#: near-monopoly is what C5 exists for.
_PEER_GROUPS: dict[str, list[tuple[str, float, float, float]]] = {
    # symbol: (peer, market_cap, revenue_growth, trailing_pe)
    "CAT": [("DE", 150e9, 0.04, 18.0), ("AGCO", 30e9, 0.06, 16.0),
            ("PCAR", 65e9, 0.05, 15.0), ("PH", 120e9, 0.07, 22.0),
            ("ETN", 154e9, 0.08, 24.0), ("RTX", 200e9, 0.06, 21.0)],
    "NVDA": [("AMD", 300e9, 0.50, 40.0), ("AVGO", 700e9, 0.45, 35.0),
             ("QCOM", 400e9, 0.48, 30.0), ("TXN", 320e9, 0.52, 33.0),
             ("INTC", 280e9, 0.47, 28.0)],
    "JPM": [("BAC", 400e9, 0.05, 12.0), ("WFC", 300e9, 0.07, 11.0),
            ("C", 200e9, 0.06, 10.0), ("GS", 250e9, 0.08, 14.0),
            ("MS", 260e9, 0.05, 15.0)],
    "O": [("NNN", 20e9, 0.08, 17.0), ("WPC", 30e9, 0.10, 15.0),
          ("ADC", 25e9, 0.09, 18.0), ("SRC", 18e9, 0.07, 14.0),
          ("STAG", 22e9, 0.11, 16.0)],
    "PGR": [("ALL", 80e9, 0.12, 13.0), ("TRV", 90e9, 0.13, 14.0),
            ("CB", 120e9, 0.15, 12.0), ("HIG", 70e9, 0.16, 11.0),
            ("CINF", 60e9, 0.14, 15.0)],
    "TSM": [("INTC", 280e9, 0.20, 28.0), ("UMC", 250e9, 0.24, 18.0),
            ("ASML", 400e9, 0.19, 35.0), ("GFS", 300e9, 0.25, 22.0)],
    "GE": [("RTX", 200e9, 0.07, 21.0), ("HON", 250e9, 0.06, 23.0),
           ("LMT", 300e9, 0.09, 18.0), ("NOC", 220e9, 0.08, 19.0),
           ("BA", 400e9, 0.10, 30.0)],
    # TPL's two named peers are an order of magnitude smaller and grow at a
    # completely different rate: no genuine peer set exists, which is the
    # condition C5 was added to handle.
    "TPL": [("LB", 2e9, 0.04, 12.0), ("DMLP", 1.2e9, 0.02, 10.0)],
}

_PEER_TAXONOMY: dict[str, tuple[str, str]] = {
    "CAT": ("Agricultural - Machinery", "Industrials"),
    "NVDA": ("Semiconductors", "Technology"),
    "JPM": ("Banks - Diversified", "Financial Services"),
    "O": ("REIT - Retail", "Real Estate"),
    "PGR": ("Insurance - Property & Casualty", "Financial Services"),
    "TSM": ("Semiconductors", "Technology"),
    "GE": ("Aerospace & Defense", "Industrials"),
    "TPL": ("Oil & Gas Midstream", "Energy"),
}


def _add_peers(adapter: FixtureAdapter) -> None:
    """Register every named peer so C2 can screen a real set."""
    seen: set[str] = set()
    for subject, peers in _PEER_GROUPS.items():
        industry, sector = _PEER_TAXONOMY[subject]
        for symbol, market_cap, growth, pe in peers:
            if symbol in seen or symbol in adapter.companies:
                continue
            seen.add(symbol)
            adapter.add(_peer(symbol, industry, sector, market_cap, growth, pe))


def build_probe_fixture() -> FixtureAdapter:
    """The ten §18 targets with realistic coverage gaps."""
    adapter = FixtureAdapter(name="fixture (retail-source shaped)")

    common_q = dict(
        total_current_assets=20e9,
        total_current_liabilities=14e9,
        total_debt=12e9,
        cash_and_equivalents=4e9,
        total_equity=18e9,
        ebitda=2.0e9,
        gross_profit=3.1e9,
    )
    common_a = dict(
        total_debt=12e9,
        cash_and_equivalents=4e9,
        total_equity=18e9,
        interest_expense=500e6,
        tax_expense=1.2e9,
        pretax_income=6.2e9,
        shares_diluted=500e6,
        operating_income=6.5e9,
        invested_capital=26e9,
        capital_expenditure=-2e9,
    )

    # 1. Mature industrial — CORE-STABLE. Full coverage.
    adapter.add(
        FixtureCompany(
            profile=_profile("CAT", "Mature Industrial", market_cap=80e9,
                             adv_3m_usd=300e6, beta=1.05, sector="Industrials",
                             industry="Agricultural - Machinery"),
            annual=make_annuals(8, revenue=40e9, net_income=5e9,
                                operating_cash_flow=7e9, free_cash_flow=5e9,
                                revenue_growth=0.05, **common_a),
            quarterly=make_quarters(12, revenue=10e9, net_income=1.25e9,
                                    operating_cash_flow=1.75e9, shares=500e6,
                                    share_growth=-0.025, **common_q),
            prices=make_prices(500, start_price=150.0, daily_drift=0.0004),
            multiples={"trailing_pe": make_multiple_series(
                "trailing_pe",
                [20, 21, 19, 22, 18, 20, 21, 19, 20, 22, 18, 21, 19, 20,
                 21, 20, 19, 22, 20, 18, 21, 19, 20, 20, 21, 19, 20, 22, 19, 21])},
            peers=["DE", "AGCO", "PCAR", "PH", "ETN", "RTX"],
            forward_eps_growth=0.09,
            dividend_yield=0.021,
        )
    )

    # 2. Profitable fast-grower — CORE-GROWTH. Full coverage.
    adapter.add(
        FixtureCompany(
            profile=_profile("NVDA", "Profitable Fast Grower", market_cap=900e9,
                             adv_3m_usd=8e9, beta=1.75, sector="Technology",
                             industry="Semiconductors", exchange="NASDAQ"),
            annual=make_annuals(6, revenue=60e9, net_income=30e9,
                                operating_cash_flow=32e9, free_cash_flow=28e9,
                                revenue_growth=0.55,
                                **{**common_a, "total_debt": 10e9,
                                   "cash_and_equivalents": 30e9,
                                   "total_equity": 50e9, "shares_diluted": 2.5e9,
                                   "operating_income": 33e9, "invested_capital": 40e9,
                                   "capital_expenditure": -4e9, "pretax_income": 34e9,
                                   "tax_expense": 4e9}),
            quarterly=make_quarters(12, revenue=15e9, net_income=7.5e9,
                                    operating_cash_flow=8e9, shares=2.5e9,
                                    revenue_growth=0.55,
                                    **{**common_q, "total_equity": 50e9,
                                       "cash_and_equivalents": 30e9, "ebitda": 8.5e9,
                                       "gross_profit": 11e9}),
            prices=make_prices(500, start_price=120.0, daily_drift=0.0012),
            multiples={"forward_pe": make_multiple_series(
                "forward_pe",
                [35, 38, 32, 40, 30, 36, 34, 39, 33, 37, 31, 35, 36, 34,
                 38, 32, 35, 37, 33, 36, 34, 35, 38, 31, 36, 35, 34, 37, 33, 36])},
            peers=["AMD", "INTC", "AVGO", "QCOM", "TXN"],
            forward_eps_growth=0.30,
        )
    )

    # 3. Unprofitable grower — SPEC-GROWTH.  Cash-flow statement present but
    # the share-count series is short, which is what breaks A4.
    adapter.add(
        FixtureCompany(
            profile=_profile("RIVN", "Unprofitable Grower", market_cap=15e9,
                             adv_3m_usd=400e6, beta=2.1, sector="Consumer Cyclical",
                             industry="Auto - Manufacturers", exchange="NASDAQ"),
            annual=make_annuals(4, revenue=5e9, net_income=-4e9,
                                operating_cash_flow=-3.5e9, free_cash_flow=-5e9,
                                revenue_growth=0.35,
                                **{**common_a, "total_debt": 5e9,
                                   "cash_and_equivalents": 8e9, "total_equity": 7e9,
                                   "shares_diluted": 1e9, "operating_income": -4.5e9,
                                   "invested_capital": 12e9, "capital_expenditure": -1.5e9,
                                   "pretax_income": -4.2e9, "tax_expense": 0.0}),
            # Only three quarters — A4's two-year share-count series needs nine.
            quarterly=make_quarters(3, revenue=1.3e9, net_income=-1e9,
                                    operating_cash_flow=-875e6, shares=1e9,
                                    **{**common_q, "total_equity": 7e9,
                                       "cash_and_equivalents": 8e9, "ebitda": -1.1e9}),
            prices=make_prices(500, start_price=14.0, daily_drift=-0.0006),
            multiples={"ev_revenue": make_multiple_series(
                "ev_revenue", [3.2, 3.5, 2.8, 4.0, 2.5, 3.0, 3.3, 2.9])},
            peers=["TSLA", "LCID", "F", "GM"],
        )
    )

    # 4. Bank.  Tangible book is absent — the field B3 needs for its P/TBV leg.
    adapter.add(
        FixtureCompany(
            profile=_profile("JPM", "Large Bank", market_cap=600e9, adv_3m_usd=2e9,
                             beta=1.1, sector="Financial Services",
                             industry="Banks - Diversified", is_bank=True),
            annual=make_annuals(10, revenue=160e9, net_income=50e9,
                                operating_cash_flow=55e9, free_cash_flow=50e9,
                                revenue_growth=0.06,
                                **{**common_a, "total_debt": 400e9,
                                   "cash_and_equivalents": 500e9,
                                   "total_equity": 320e9, "shares_diluted": 2.9e9,
                                   "operating_income": 62e9, "invested_capital": 700e9,
                                   "pretax_income": 62e9, "tax_expense": 12e9,
                                   "tangible_book_value": None}),
            quarterly=make_quarters(12, revenue=40e9, net_income=12.5e9,
                                    operating_cash_flow=13e9, shares=2.9e9,
                                    share_growth=-0.02,
                                    **{**common_q, "total_equity": 320e9,
                                       "total_debt": 400e9,
                                       "cash_and_equivalents": 500e9, "ebitda": 16e9}),
            prices=make_prices(500, start_price=210.0, daily_drift=0.0005),
            multiples={"p_b": make_multiple_series(
                "p_b", [1.8, 1.9, 1.7, 2.0, 1.6, 1.85, 1.75, 1.95, 1.7, 1.8,
                        1.9, 1.65, 1.85, 1.75, 1.8, 1.9, 1.7, 1.85, 1.8, 1.75,
                        1.9, 1.8, 1.7, 1.85, 1.8, 1.9, 1.75, 1.8, 1.85, 1.75])},
            peers=["BAC", "WFC", "C", "GS", "MS"],
            dividend_yield=0.023,
        )
    )

    # 5. REIT.  No FFO, no AFFO — B4 cannot run, and P/E is banned here.
    adapter.add(
        FixtureCompany(
            profile=_profile("O", "Net Lease REIT", market_cap=50e9, adv_3m_usd=350e6,
                             beta=0.85, sector="Real Estate",
                             industry="REIT - Retail", is_reit=True),
            annual=make_annuals(9, revenue=5e9, net_income=900e6,
                                operating_cash_flow=3.2e9, free_cash_flow=3.0e9,
                                revenue_growth=0.09,
                                **{**common_a, "total_debt": 25e9,
                                   "cash_and_equivalents": 500e6,
                                   "total_equity": 38e9, "shares_diluted": 870e6,
                                   "operating_income": 1.8e9, "invested_capital": 60e9,
                                   "pretax_income": 950e6, "tax_expense": 50e6}),
            quarterly=make_quarters(12, revenue=1.25e9, net_income=225e6,
                                    operating_cash_flow=800e6, shares=870e6,
                                    share_growth=0.04,
                                    **{**common_q, "total_debt": 25e9,
                                       "total_equity": 38e9,
                                       "cash_and_equivalents": 500e6, "ebitda": 1.1e9}),
            prices=make_prices(500, start_price=58.0, daily_drift=0.0001),
            multiples={"p_affo": make_multiple_series(
                "p_affo", [16, 17, 15, 18, 14, 16.5, 15.5, 17.5, 15, 16, 17,
                           14.5, 16.5, 15.5, 16, 17, 15, 16.5, 16, 15.5, 17,
                           16, 15, 16.5, 16, 17, 15.5, 16, 16.5, 15.5])},
            peers=["NNN", "WPC", "ADC", "SRC", "STAG"],
            dividend_yield=0.055,
        )
    )

    # 6. Insurer.  No combined ratio — B5's primary underwriting read is absent.
    adapter.add(
        FixtureCompany(
            profile=_profile("PGR", "P&C Insurer", market_cap=140e9, adv_3m_usd=900e6,
                             beta=0.6, sector="Financial Services",
                             industry="Insurance - Property & Casualty",
                             is_insurer=True),
            annual=make_annuals(10, revenue=70e9, net_income=7e9,
                                operating_cash_flow=12e9, free_cash_flow=11e9,
                                revenue_growth=0.14,
                                **{**common_a, "total_debt": 7e9,
                                   "cash_and_equivalents": 3e9, "total_equity": 26e9,
                                   "shares_diluted": 585e6, "operating_income": 9e9,
                                   "invested_capital": 33e9, "pretax_income": 9e9,
                                   "tax_expense": 2e9}),
            quarterly=make_quarters(12, revenue=17.5e9, net_income=1.75e9,
                                    operating_cash_flow=3e9, shares=585e6,
                                    **{**common_q, "total_debt": 7e9,
                                       "total_equity": 26e9,
                                       "cash_and_equivalents": 3e9, "ebitda": 2.4e9}),
            prices=make_prices(500, start_price=240.0, daily_drift=0.0006),
            multiples={"p_b": make_multiple_series(
                "p_b", [5.0, 5.4, 4.6, 5.8, 4.2, 5.1, 4.8, 5.5, 4.7, 5.2, 5.6,
                        4.4, 5.1, 4.9, 5.0, 5.3, 4.6, 5.2, 5.0, 4.8, 5.4, 5.0,
                        4.7, 5.1, 5.0, 5.3, 4.9, 5.0, 5.2, 4.8])},
            peers=["ALL", "TRV", "CB", "HIG", "CINF"],
            dividend_yield=0.005,
        )
    )

    # 7. Foreign ADR.  Underlying currency is unmapped — the K5 gap.
    adapter.add(
        FixtureCompany(
            profile=_profile("TSM", "Foreign ADR", market_cap=700e9, adv_3m_usd=1.5e9,
                             beta=1.3, sector="Technology",
                             industry="Semiconductors", is_adr=True,
                             underlying_currency=None, country="TW"),
            annual=make_annuals(8, revenue=90e9, net_income=36e9,
                                operating_cash_flow=55e9, free_cash_flow=25e9,
                                revenue_growth=0.22,
                                **{**common_a, "total_debt": 30e9,
                                   "cash_and_equivalents": 60e9, "total_equity": 120e9,
                                   "shares_diluted": 5.2e9, "operating_income": 42e9,
                                   "invested_capital": 150e9, "pretax_income": 41e9,
                                   "tax_expense": 5e9, "capital_expenditure": -30e9}),
            quarterly=make_quarters(12, revenue=23e9, net_income=9e9,
                                    operating_cash_flow=14e9, shares=5.2e9,
                                    revenue_growth=0.22,
                                    **{**common_q, "total_debt": 30e9,
                                       "total_equity": 120e9,
                                       "cash_and_equivalents": 60e9, "ebitda": 14e9}),
            prices=make_prices(500, start_price=180.0, daily_drift=0.0009),
            multiples={"forward_pe": make_multiple_series(
                "forward_pe", [18, 20, 16, 22, 15, 19, 17, 21, 16, 18, 20, 15,
                               19, 17, 18, 20, 16, 19, 18, 17, 20, 18, 16, 19,
                               18, 20, 17, 18, 19, 17])},
            peers=["INTC", "UMC", "GFS", "ASML"],
        )
    )

    # 8. Spinoff in the last 7 years — C1.1 truncates the series.
    adapter.add(
        FixtureCompany(
            profile=_profile("GE", "Post-Spinoff Conglomerate", market_cap=340e9,
                             adv_3m_usd=1.2e9, beta=1.25, sector="Industrials",
                             industry="Aerospace & Defense"),
            annual=make_annuals(8, revenue=35e9, net_income=6e9,
                                operating_cash_flow=7e9, free_cash_flow=5.5e9,
                                revenue_growth=0.08, **common_a),
            quarterly=make_quarters(12, revenue=9e9, net_income=1.5e9,
                                    operating_cash_flow=1.8e9, shares=1.07e9,
                                    share_growth=-0.03, **common_q),
            prices=make_prices(500, start_price=300.0, daily_drift=0.0011),
            multiples={"trailing_pe": make_multiple_series(
                "trailing_pe", [30, 33, 28, 35, 26, 31, 29, 34, 27, 32, 30, 28,
                                31, 29, 30, 33, 27, 31, 30, 29, 32, 30, 28, 31,
                                30, 33, 29, 30, 31, 29])},
            corporate_actions=[
                CorporateAction(CorporateActionType.SPINOFF,
                                _TODAY - timedelta(days=500),
                                description="spun off the energy business"),
            ],
            peers=["RTX", "HON", "LMT", "NOC", "BA"],
            dividend_yield=0.007,
        )
    )

    # 9. Near-monopoly with no genuine peers — C5's reason for existing.
    adapter.add(
        FixtureCompany(
            profile=_profile("TPL", "Near-Monopoly", market_cap=30e9, adv_3m_usd=120e6,
                             beta=1.0, sector="Energy",
                             industry="Oil & Gas Midstream"),
            annual=make_annuals(8, revenue=700e6, net_income=450e6,
                                operating_cash_flow=500e6, free_cash_flow=480e6,
                                revenue_growth=0.18,
                                **{**common_a, "total_debt": 0.0,
                                   "cash_and_equivalents": 900e6,
                                   "total_equity": 1.1e9, "shares_diluted": 23e6,
                                   "operating_income": 560e6, "invested_capital": 1.1e9,
                                   "pretax_income": 560e6, "tax_expense": 110e6,
                                   "interest_expense": None,
                                   "capital_expenditure": -20e6}),
            quarterly=make_quarters(12, revenue=175e6, net_income=112e6,
                                    operating_cash_flow=125e6, shares=23e6,
                                    **{**common_q, "total_debt": 0.0,
                                       "total_equity": 1.1e9,
                                       "cash_and_equivalents": 900e6, "ebitda": 140e6}),
            prices=make_prices(500, start_price=1300.0, daily_drift=0.0008),
            multiples={"trailing_pe": make_multiple_series(
                "trailing_pe", [55, 60, 50, 65, 45, 57, 52, 62, 48, 58, 54, 47,
                                56, 51, 55, 60, 46, 57, 55, 52, 59, 55, 49, 56,
                                55, 60, 53, 55, 57, 52])},
            # Two candidates, and neither survives C2's size and growth screen.
            peers=["LB", "DMLP"],
            dividend_yield=0.008,
        )
    )

    # 10. Delisted name is simply absent — survivorship bias as a 404.
    #     Deliberately not added.

    adapter.market = MarketData(
        risk_free_rate=0.042,
        risk_free_rate_date=_TODAY,
        fx_rates={"USDSGD": 1.29},
        fx_rate_timestamp=_TODAY,
        group_net_debt_ebitda_median={
            "Agricultural - Machinery": 1.8,
            "Semiconductors": 0.6,
            "Auto - Manufacturers": 2.4,
            "Banks - Diversified": 3.0,
            "REIT - Retail": 5.5,
            "Insurance - Property & Casualty": 1.2,
            "Aerospace & Defense": 2.0,
            "Oil & Gas Midstream": 3.5,
        },
        group_member_counts={
            "Agricultural - Machinery": 14,
            "Semiconductors": 40,
            "Auto - Manufacturers": 18,
            "Banks - Diversified": 60,
            "REIT - Retail": 25,
            "Insurance - Property & Casualty": 30,
            "Aerospace & Defense": 22,
            "Oil & Gas Midstream": 16,
        },
        group_gross_margin_stdev_median={
            "Agricultural - Machinery": 0.020,
            "Semiconductors": 0.035,
            "Aerospace & Defense": 0.022,
        },
        sector_cap_rates={"Real Estate": 0.058},
    )

    _add_peers(adapter)

    # A retail source ships its own taxonomy and no point-in-time history.
    adapter.unsupported = frozenset({"gics_sub_industry", "point_in_time"})
    return adapter


__all__ = ["build_probe_fixture"]
