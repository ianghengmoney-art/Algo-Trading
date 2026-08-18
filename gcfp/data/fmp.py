"""Financial Modeling Prep adapter.

Capabilities are *probed*, not assumed. FMP gates endpoints by subscription
tier, and the tier -- not the vendor -- decides whether this system can run the
SPEC-GROWTH path or the Module C1 anchor at all. A plan that serves company
profiles but no financial statements will happily answer some calls and refuse
others, so the adapter asks before the engine routes anything.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Optional, Sequence

import requests

from ..types import (
    CompanyProfile,
    Estimates,
    Financials,
    FilingFlags,
    MultiplePoint,
    MultipleSeries,
    PeriodType,
    PricePoint,
)
from .base import Capability, DataAdapter, DataUnavailable

BASE_URL = "https://financialmodelingprep.com/stable"


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(value)[:19], fmt).date()
        except ValueError:
            continue
    return None


def _f(row: dict, *keys: str) -> Optional[float]:
    """First present numeric value among ``keys``.

    Returns ``None`` rather than 0.0 when nothing is present: a missing field
    and a genuine zero must not collapse into the same value.
    """
    for key in keys:
        if key in row and row[key] is not None:
            try:
                return float(row[key])
            except (TypeError, ValueError):
                continue
    return None


class FMPAdapter(DataAdapter):
    name = "fmp"

    def __init__(
        self,
        api_key: Optional[str] = None,
        session: Optional[requests.Session] = None,
        timeout: float = 20.0,
        probe_on_init: bool = False,
    ) -> None:
        self.api_key = api_key or os.environ.get("FMP_API_KEY")
        if not self.api_key:
            raise DataUnavailable("*", "FMP credentials", "set FMP_API_KEY")
        self.session = session or requests.Session()
        self.timeout = timeout
        self._capabilities: Optional[set[Capability]] = None
        if probe_on_init:
            self.probe_capabilities()

    # -- transport --------------------------------------------------------
    def _get(self, path: str, **params: Any) -> Any:
        params["apikey"] = self.api_key
        response = self.session.get(f"{BASE_URL}/{path}", params=params, timeout=self.timeout)
        if response.status_code in (401, 402, 403):
            raise DataUnavailable(
                str(params.get("symbol", path)), path,
                f"HTTP {response.status_code} -- endpoint not available on this subscription tier",
            )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("Error Message"):
            raise DataUnavailable(str(params.get("symbol", path)), path, payload["Error Message"])
        return payload

    # -- capabilities -----------------------------------------------------
    def probe_capabilities(self, probe_symbol: str = "AAPL") -> set[Capability]:
        """Discover what this key can actually reach.

        Each probe is a single cheap call. The result is cached, because the
        answer is a property of the subscription rather than of the symbol.
        """
        found: set[Capability] = set()
        probes: list[tuple[Capability, str, dict]] = [
            (Capability.PROFILE, "profile", {"symbol": probe_symbol}),
            (Capability.PEER_LIST, "stock-peers", {"symbol": probe_symbol}),
            (Capability.ANNUAL_STATEMENTS, "income-statement", {"symbol": probe_symbol, "period": "annual", "limit": 1}),
            (Capability.QUARTERLY_STATEMENTS, "income-statement", {"symbol": probe_symbol, "period": "quarter", "limit": 1}),
            (Capability.PRICE_HISTORY, "historical-price-eod/light", {"symbol": probe_symbol}),
            (Capability.MULTIPLE_HISTORY, "key-metrics", {"symbol": probe_symbol, "period": "annual", "limit": 1}),
            (Capability.ANALYST_ESTIMATES, "analyst-estimates", {"symbol": probe_symbol, "period": "annual", "limit": 1}),
            (Capability.EARNINGS_CALENDAR, "earnings", {"symbol": probe_symbol, "limit": 1}),
        ]
        for capability, path, params in probes:
            try:
                payload = self._get(path, **params)
            except (DataUnavailable, requests.RequestException):
                continue
            if payload:
                found.add(capability)

        # Derived capabilities: both live inside the statement endpoints.
        if Capability.ANNUAL_STATEMENTS in found:
            found.add(Capability.SHARE_COUNT_HISTORY)
            found.add(Capability.CASH_BURN_HISTORY)
            found.add(Capability.BANK_TANGIBLE_BOOK)
            found.add(Capability.SECTOR_AGGREGATES)

        # Deliberately never added: FMP's statement and peer endpoints are
        # as-restated and current-membership respectively.
        self._capabilities = found
        return found

    def capabilities(self) -> set[Capability]:
        if self._capabilities is None:
            return self.probe_capabilities()
        return self._capabilities

    # -- endpoints --------------------------------------------------------
    def get_profile(self, symbol: str) -> CompanyProfile:
        rows = self._get("profile", symbol=symbol)
        if not rows:
            raise DataUnavailable(symbol, "profile")
        row = rows[0]
        industry = (row.get("industry") or "").lower()
        sector = (row.get("sector") or "").lower()
        avg_volume = _f(row, "averageVolume", "volAvg")
        price = _f(row, "price")
        return CompanyProfile(
            symbol=row.get("symbol", symbol),
            name=row.get("companyName"),
            sector=row.get("sector"),
            industry=row.get("industry"),
            exchange=row.get("exchange"),
            country=row.get("country"),
            currency=row.get("currency"),
            market_cap=_f(row, "marketCap", "mktCap"),
            price=price,
            beta=_f(row, "beta"),
            avg_daily_dollar_volume=(avg_volume * price) if avg_volume and price else None,
            is_adr=row.get("isAdr"),
            is_etf=row.get("isEtf"),
            is_fund=row.get("isFund"),
            is_actively_trading=row.get("isActivelyTrading"),
            ipo_date=_parse_date(row.get("ipoDate")),
            is_reit="reit" in industry,
            is_bank="bank" in industry or ("financial" in sector and "bank" in industry),
            is_insurer="insurance" in industry,
            as_of=date.today(),
        )

    def _statements(self, symbol: str, period: str, limit: int) -> list[Financials]:
        income = self._get("income-statement", symbol=symbol, period=period, limit=limit)
        balance = self._get("balance-sheet-statement", symbol=symbol, period=period, limit=limit)
        cash = self._get("cash-flow-statement", symbol=symbol, period=period, limit=limit)

        by_date: dict[str, dict] = {}
        for rows, prefix in ((income, "i"), (balance, "b"), (cash, "c")):
            for row in rows or []:
                key = str(row.get("date") or row.get("period"))
                by_date.setdefault(key, {})[prefix] = row

        out: list[Financials] = []
        for key, parts in by_date.items():
            period_end = _parse_date(key)
            if period_end is None:
                continue
            i, b, c = parts.get("i", {}), parts.get("b", {}), parts.get("c", {})
            out.append(
                Financials(
                    period_end=period_end,
                    period_type=PeriodType.ANNUAL if period == "annual" else PeriodType.QUARTER,
                    filed_date=_parse_date(i.get("filingDate") or b.get("filingDate")),
                    revenue=_f(i, "revenue"),
                    gross_profit=_f(i, "grossProfit"),
                    operating_income=_f(i, "operatingIncome"),
                    ebitda=_f(i, "ebitda", "EBITDA"),
                    net_income=_f(i, "netIncome"),
                    eps_diluted=_f(i, "epsDiluted", "epsdiluted"),
                    interest_expense=_f(i, "interestExpense"),
                    tax_expense=_f(i, "incomeTaxExpense"),
                    pretax_income=_f(i, "incomeBeforeTax"),
                    depreciation_amortisation=_f(i, "depreciationAndAmortization"),
                    current_assets=_f(b, "totalCurrentAssets"),
                    current_liabilities=_f(b, "totalCurrentLiabilities"),
                    cash_and_equivalents=_f(b, "cashAndCashEquivalents"),
                    short_term_investments=_f(b, "shortTermInvestments"),
                    total_debt=_f(b, "totalDebt"),
                    total_assets=_f(b, "totalAssets"),
                    total_liabilities=_f(b, "totalLiabilities"),
                    total_equity=_f(b, "totalStockholdersEquity"),
                    goodwill_and_intangibles=_f(b, "goodwillAndIntangibleAssets"),
                    shares_diluted=_f(i, "weightedAverageShsOutDil"),
                    operating_cash_flow=_f(c, "operatingCashFlow", "netCashProvidedByOperatingActivities"),
                    capital_expenditure=_f(c, "capitalExpenditure"),
                    free_cash_flow=_f(c, "freeCashFlow"),
                    dividends_paid=_f(c, "commonDividendsPaid", "dividendsPaid"),
                    share_repurchase=_f(c, "commonStockRepurchased"),
                )
            )
        return sorted(out, key=lambda f: f.period_end, reverse=True)

    def get_annual_financials(self, symbol: str, years: int = 10) -> Sequence[Financials]:
        return self._statements(symbol, "annual", years)

    def get_quarterly_financials(self, symbol: str, quarters: int = 12) -> Sequence[Financials]:
        return self._statements(symbol, "quarter", quarters)

    def get_price_history(self, symbol: str, years: int = 10) -> Sequence[PricePoint]:
        rows = self._get("historical-price-eod/light", symbol=symbol)
        if isinstance(rows, dict):
            rows = rows.get("historical", [])
        points = []
        for row in rows or []:
            observed = _parse_date(row.get("date"))
            close = _f(row, "close", "price", "adjClose")
            if observed and close is not None:
                points.append(PricePoint(observed, close))
        if not points:
            raise DataUnavailable(symbol, "price history")
        return sorted(points, key=lambda p: p.observed_on)

    def get_multiple_series(self, symbol: str, multiple_name: str, years: int = 10) -> MultipleSeries:
        rows = self._get("key-metrics", symbol=symbol, period="annual", limit=years)
        field_map = {
            "trailing P/E": ("peRatio", "priceToEarningsRatio"),
            "forward P/E": ("peRatio", "priceToEarningsRatio"),
            "P/B": ("pbRatio", "priceToBookRatio"),
            "EV/Revenue": ("evToSales", "enterpriseValueOverRevenue"),
            "P/AFFO": ("priceToOperatingCashFlowsRatio", "pocfratio"),
        }
        keys = field_map.get(multiple_name)
        if keys is None:
            raise DataUnavailable(symbol, f"{multiple_name} history", "multiple not mapped for FMP")
        points = []
        for row in rows or []:
            observed = _parse_date(row.get("date"))
            value = _f(row, *keys)
            if observed and value is not None and value > 0:
                points.append(MultiplePoint(observed, value))
        if not points:
            raise DataUnavailable(symbol, f"{multiple_name} history")
        return MultipleSeries(symbol, multiple_name, tuple(sorted(points, key=lambda p: p.observed_on)))

    def get_peers(self, symbol: str) -> Sequence[str]:
        rows = self._get("stock-peers", symbol=symbol)
        peers = [row.get("symbol") for row in rows or [] if row.get("symbol")]
        if not peers:
            raise DataUnavailable(symbol, "peer list")
        return peers

    def get_estimates(self, symbol: str) -> Estimates:
        rows = self._get("analyst-estimates", symbol=symbol, period="annual", limit=2)
        forward_eps = None
        forward_revenue = None
        if rows:
            forward_eps = _f(rows[0], "epsAvg", "estimatedEpsAvg")
            forward_revenue = _f(rows[0], "revenueAvg", "estimatedRevenueAvg")
        next_earnings = None
        try:
            calendar = self._get("earnings", symbol=symbol, limit=4)
            today = date.today()
            upcoming = sorted(
                [d for d in (_parse_date(r.get("date")) for r in calendar or []) if d and d >= today]
            )
            next_earnings = upcoming[0] if upcoming else None
        except (DataUnavailable, requests.RequestException):
            next_earnings = None
        return Estimates(
            forward_eps=forward_eps,
            forward_revenue=forward_revenue,
            next_earnings_date=next_earnings,
            as_of=date.today(),
        )

    def get_filing_flags(self, symbol: str) -> FilingFlags:
        # FMP does not expose restatement, auditor-change, going-concern or
        # late-filing status as structured fields. Returning all-None is the
        # honest answer; gate A4 turns it into a DATA_GAP rather than a pass.
        raise DataUnavailable(
            symbol, "filing red flags",
            "FMP exposes no structured restatement / auditor-change / going-concern fields",
        )
