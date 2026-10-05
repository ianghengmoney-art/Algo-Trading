"""yfinance adapter.

Free, and the only source in this repository that covers quarterly statements,
share-count history and long price history without a paid tier -- which is
exactly what the two Section 17 stop conditions turn on.

Two standing caveats, neither of which the adapter can fix:
  * Yahoo restates. Statements are as-currently-reported, so this source is not
    point-in-time and ``Capability.POINT_IN_TIME`` is never advertised.
  * Yahoo supplies no peer list and no filing red flags. The peer set must come
    from a sector screen elsewhere, and gate A4 will return DATA_GAP.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional, Sequence

from ..types import (
    CompanyProfile,
    Estimates,
    Financials,
    MultiplePoint,
    MultipleSeries,
    PeriodType,
    PricePoint,
)
from .base import Capability, DataAdapter, DataUnavailable

# Yahoo's row labels, in the order we prefer them.
ROW_ALIASES = {
    "revenue": ("Total Revenue", "Operating Revenue"),
    "gross_profit": ("Gross Profit",),
    "operating_income": ("Operating Income", "Total Operating Income As Reported"),
    "ebitda": ("EBITDA", "Normalized EBITDA"),
    "net_income": ("Net Income", "Net Income Common Stockholders"),
    "pretax_income": ("Pretax Income",),
    "tax_expense": ("Tax Provision",),
    "depreciation_amortisation": ("Reconciled Depreciation", "Depreciation And Amortization"),
    "current_assets": ("Current Assets", "Total Current Assets"),
    "current_liabilities": ("Current Liabilities", "Total Current Liabilities"),
    "cash_and_equivalents": ("Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"),
    "short_term_investments": ("Other Short Term Investments",),
    "total_debt": ("Total Debt",),
    "total_assets": ("Total Assets",),
    "total_liabilities": ("Total Liabilities Net Minority Interest",),
    "total_equity": ("Stockholders Equity", "Total Equity Gross Minority Interest"),
    "goodwill_and_intangibles": ("Goodwill And Other Intangible Assets", "Goodwill"),
    "shares_diluted": ("Diluted Average Shares", "Basic Average Shares"),
    "operating_cash_flow": ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
    "capital_expenditure": ("Capital Expenditure",),
    "free_cash_flow": ("Free Cash Flow",),
    "dividends_paid": ("Cash Dividends Paid", "Common Stock Dividend Paid"),
    "share_repurchase": ("Repurchase Of Capital Stock",),
    "tangible_common_equity": ("Tangible Book Value",),
}


def _pick(frame: Any, column: Any, names: Sequence[str]) -> Optional[float]:
    if frame is None or getattr(frame, "empty", True):
        return None
    for name in names:
        if name in frame.index:
            try:
                value = frame.loc[name, column]
            except KeyError:
                continue
            if value is None:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if numeric != numeric:  # NaN
                continue
            return numeric
    return None


class YFinanceAdapter(DataAdapter):
    name = "yfinance"

    def __init__(self) -> None:
        try:
            import yfinance
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise DataUnavailable("*", "yfinance", "pip install yfinance") from exc
        self._yf = yfinance
        self._cache: dict[str, Any] = {}

    def capabilities(self) -> set[Capability]:
        return {
            Capability.PROFILE,
            Capability.ANNUAL_STATEMENTS,
            Capability.QUARTERLY_STATEMENTS,
            Capability.SHARE_COUNT_HISTORY,
            Capability.CASH_BURN_HISTORY,
            Capability.PRICE_HISTORY,
            Capability.ANALYST_ESTIMATES,
            Capability.EARNINGS_CALENDAR,
            Capability.BANK_TANGIBLE_BOOK,
        }

    def _ticker(self, symbol: str):
        if symbol not in self._cache:
            self._cache[symbol] = self._yf.Ticker(symbol)
        return self._cache[symbol]

    def get_profile(self, symbol: str) -> CompanyProfile:
        ticker = self._ticker(symbol)
        info = getattr(ticker, "info", None) or {}
        if not info:
            raise DataUnavailable(symbol, "profile", "yfinance returned no info")
        industry = (info.get("industry") or "").lower()
        quote_type = (info.get("quoteType") or "").upper()
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        volume = info.get("averageVolume") or info.get("averageDailyVolume3Month")
        return CompanyProfile(
            symbol=symbol,
            name=info.get("longName") or info.get("shortName"),
            sector=info.get("sector"),
            industry=info.get("industry"),
            exchange=info.get("exchange"),
            country=info.get("country"),
            currency=info.get("currency"),
            market_cap=info.get("marketCap"),
            price=price,
            beta=info.get("beta"),
            avg_daily_dollar_volume=(volume * price) if volume and price else None,
            shares_outstanding=info.get("sharesOutstanding"),
            is_etf=quote_type == "ETF",
            is_fund=quote_type in {"MUTUALFUND", "FUND"},
            is_actively_trading=True,
            is_reit="reit" in industry,
            is_bank="bank" in industry,
            is_insurer="insurance" in industry,
            as_of=date.today(),
        )

    def _frames(self, symbol: str, quarterly: bool):
        ticker = self._ticker(symbol)
        if quarterly:
            return ticker.quarterly_income_stmt, ticker.quarterly_balance_sheet, ticker.quarterly_cashflow
        return ticker.income_stmt, ticker.balance_sheet, ticker.cashflow

    def _statements(self, symbol: str, quarterly: bool, limit: int) -> Sequence[Financials]:
        income, balance, cash = self._frames(symbol, quarterly)
        if income is None or getattr(income, "empty", True):
            raise DataUnavailable(symbol, "statements", "yfinance returned an empty frame")

        out: list[Financials] = []
        for column in list(income.columns)[:limit]:
            period_end = column.date() if hasattr(column, "date") else column
            values = {}
            for field, names in ROW_ALIASES.items():
                values[field] = (
                    _pick(income, column, names)
                    if _pick(income, column, names) is not None
                    else _pick(balance, column, names)
                    if _pick(balance, column, names) is not None
                    else _pick(cash, column, names)
                )
            out.append(
                Financials(
                    period_end=period_end,
                    period_type=PeriodType.QUARTER if quarterly else PeriodType.ANNUAL,
                    **values,
                )
            )
        return sorted(out, key=lambda f: f.period_end, reverse=True)

    def get_annual_financials(self, symbol: str, years: int = 10) -> Sequence[Financials]:
        return self._statements(symbol, quarterly=False, limit=years)

    def get_quarterly_financials(self, symbol: str, quarters: int = 12) -> Sequence[Financials]:
        return self._statements(symbol, quarterly=True, limit=quarters)

    def get_price_history(self, symbol: str, years: int = 10) -> Sequence[PricePoint]:
        history = self._ticker(symbol).history(period=f"{years}y", interval="1mo")
        if history is None or getattr(history, "empty", True):
            raise DataUnavailable(symbol, "price history")
        points = [
            PricePoint(index.date() if hasattr(index, "date") else index, float(row["Close"]))
            for index, row in history.iterrows()
            if row.get("Close") == row.get("Close")
        ]
        return sorted(points, key=lambda p: p.observed_on)

    def get_multiple_series(self, symbol: str, multiple_name: str, years: int = 10) -> MultipleSeries:
        """Reconstruct a historical multiple from price history and reported earnings.

        Only ``trailing P/E``, ``P/B`` and ``EV/Revenue`` can be rebuilt this
        way, and only annually -- one observation per fiscal year, which is
        seven to ten points across the required window. That is enough for a
        median and a percentile, and thin for a standard deviation; Module C1
        reports the observation count so the reader can weigh it.
        """
        supported = {"trailing P/E": "net_income", "P/B": "total_equity", "EV/Revenue": "revenue"}
        if multiple_name not in supported:
            raise DataUnavailable(symbol, f"{multiple_name} history", "not reconstructible from yfinance")

        annuals = self.get_annual_financials(symbol, years)
        prices = self.get_price_history(symbol, years)
        if not annuals or not prices:
            raise DataUnavailable(symbol, f"{multiple_name} history")

        attribute = supported[multiple_name]
        points: list[MultiplePoint] = []
        for period in annuals:
            denominator = getattr(period, attribute, None)
            shares = period.shares_diluted
            if denominator is None or not shares or denominator <= 0:
                continue
            nearby = min(prices, key=lambda p: abs((p.observed_on - period.period_end).days))
            if abs((nearby.observed_on - period.period_end).days) > 45:
                continue
            per_share = denominator / shares
            if multiple_name == "EV/Revenue":
                net_debt = period.net_debt or 0.0
                enterprise = nearby.close * shares + net_debt
                value = enterprise / denominator
            else:
                value = nearby.close / per_share
            if value > 0:
                points.append(MultiplePoint(period.period_end, value))

        if not points:
            raise DataUnavailable(symbol, f"{multiple_name} history", "no usable observations")
        return MultipleSeries(symbol, multiple_name, tuple(sorted(points, key=lambda p: p.observed_on)))

    def get_estimates(self, symbol: str) -> Estimates:
        ticker = self._ticker(symbol)
        info = getattr(ticker, "info", None) or {}
        next_earnings = None
        try:
            calendar = ticker.calendar
            if isinstance(calendar, dict):
                dates = calendar.get("Earnings Date") or []
                next_earnings = dates[0] if dates else None
                if hasattr(next_earnings, "date"):
                    next_earnings = next_earnings.date()
        except Exception:  # pragma: no cover - yfinance surface varies
            next_earnings = None
        growth = info.get("earningsGrowth")
        yield_pct = info.get("dividendYield")
        return Estimates(
            forward_eps=info.get("forwardEps"),
            forward_eps_growth_pct=(growth * 100.0) if isinstance(growth, (int, float)) else None,
            dividend_yield_pct=(yield_pct * 100.0) if isinstance(yield_pct, (int, float)) and yield_pct < 1 else yield_pct,
            next_earnings_date=next_earnings,
            as_of=date.today(),
        )
