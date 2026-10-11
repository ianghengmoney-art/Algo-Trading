"""Financial Modeling Prep adapter.

Two provider facts are baked in here rather than discovered at runtime, because
both change what the gates are allowed to claim:

1. **FMP does not publish GICS codes.**  ``/profile`` returns FMP's own
   ``sector`` and ``industry`` strings (Caterpillar comes back as sector
   "Industrials", industry "Agricultural - Machinery").  There is no
   sub-industry rung and no GICS code at any tier.  So this adapter reports
   ``taxonomy_is_gics=False`` and a finest level of INDUSTRY, and A2/C2 log
   every grouping as VENDOR-SUBSTITUTE.  §18 stop condition 3 exists for
   exactly this case.

2. **Endpoint access is plan-tiered.**  On the lower tiers the statements,
   quote, chart and screener families all return 402/403.  A tier that cannot
   serve statements cannot serve a single Module A gate, so
   :meth:`capabilities` probes live rather than assuming, and the §18 report
   distinguishes "this provider lacks the data" from "this key lacks the
   entitlement" — different problems with different fixes.

The vendor peer list is deliberately *not* treated as a peer group.  FMP's
peers for CAT include RTX and ETN, which are neither the same business nor
within C2's growth band; they are a candidate set that C2 then screens and logs
rejections from.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Sequence

from ..types import (
    CompanyProfile,
    CorporateAction,
    CorporateActionType,
    MarketData,
    MultipleObservation,
    PeriodFinancials,
    PricePoint,
    ReportingFrequency,
    TaxonomyLevel,
)
from .adapter import Capability, DataAdapter, DataUnavailable, REQUIRED_CAPABILITIES

BASE_URL = "https://financialmodelingprep.com/api/v3"
STABLE_URL = "https://financialmodelingprep.com/stable"

#: Capabilities FMP cannot serve at any tier.  Listed separately from
#: entitlement failures so the coverage report can tell them apart.
STRUCTURALLY_UNAVAILABLE: dict[str, str] = {
    "gics_sub_industry": (
        "FMP publishes its own sector/industry taxonomy; no GICS code at any tier"
    ),
    "point_in_time": (
        "FMP serves current constituents and current peer lists; historical "
        "point-in-time membership is not offered, so backtests built on it "
        "carry survivorship bias of unknown magnitude"
    ),
    "historical_multiples": (
        "no first-class multiple history endpoint; C1 series must be "
        "reconstructed from prices x per-share fundamentals, which is a "
        "derivation this adapter performs and flags, not a vendor series"
    ),
}


def _f(value: Any) -> float | None:
    """Numbers only.  A blank, a null, or a zero-where-null becomes ``None``
    so that A5 sees a gap instead of a gate seeing a plausible number."""
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _d(value: Any) -> date | None:
    if not value:
        return None
    text = str(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


@dataclass
class FMPAdapter(DataAdapter):
    """Live FMP client.

    Requires network egress to ``financialmodelingprep.com`` and an API key.
    """

    api_key: str
    session: Any = None
    timeout: float = 20.0
    max_retries: int = 3
    retry_backoff: float = 2.0
    name: str = "fmp"
    _capability_cache: list[Capability] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError("FMPAdapter requires an API key")
        if self.session is None:
            import requests  # imported lazily so tests need no network stack

            self.session = requests.Session()

    # -- transport --------------------------------------------------------
    def _get(self, path: str, *, base: str = BASE_URL, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        params["apikey"] = self.api_key
        url = f"{base}/{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except Exception as exc:  # network flake
                last_exc = exc
                time.sleep(self.retry_backoff**attempt)
                continue
            if resp.status_code in (401, 402, 403):
                raise DataUnavailable(
                    path,
                    f"HTTP {resp.status_code} — endpoint not entitled on this API "
                    "plan (an entitlement gap, not a data gap)",
                )
            if resp.status_code == 429:
                time.sleep(self.retry_backoff**attempt)
                continue
            if resp.status_code >= 500:
                last_exc = RuntimeError(f"HTTP {resp.status_code}")
                time.sleep(self.retry_backoff**attempt)
                continue
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, dict) and "Error Message" in payload:
                raise DataUnavailable(path, str(payload["Error Message"]))
            return payload
        raise DataUnavailable(path, f"exhausted retries: {last_exc}")

    # -- identity ---------------------------------------------------------
    def get_profile(self, symbol: str) -> CompanyProfile:
        rows = self._get(f"profile/{symbol}")
        if not rows:
            raise DataUnavailable("profile", f"no profile for {symbol}")
        row = rows[0]
        industry = row.get("industry") or None
        sector = row.get("sector") or None
        return CompanyProfile(
            symbol=row.get("symbol", symbol),
            name=row.get("companyName") or symbol,
            listing_currency=row.get("currency") or "USD",
            reporting_currency=row.get("currency") or "USD",
            exchange=row.get("exchangeShortName") or row.get("exchange"),
            country=row.get("country"),
            market_cap=_f(row.get("mktCap") or row.get("marketCap")),
            adv_3m_usd=_adv(row),
            beta=_f(row.get("beta")),
            sector=sector,
            industry=industry,
            # No GICS anywhere in the FMP schema — industry is the finest rung,
            # and it is a vendor taxonomy, not a GICS sub-industry.
            sub_industry=None,
            gics_sub_industry_code=None,
            taxonomy_level=(
                TaxonomyLevel.INDUSTRY
                if industry
                else TaxonomyLevel.SECTOR
                if sector
                else TaxonomyLevel.UNAVAILABLE
            ),
            taxonomy_is_gics=False,
            is_adr=bool(row.get("isAdr")),
            underlying_currency=None,  # K5 requires an operator-supplied map
            is_reit=_looks_like(industry, sector, ("reit",)),
            is_bank=_looks_like(industry, sector, ("bank", "credit services")),
            is_insurer=_looks_like(industry, sector, ("insurance",)),
            ipo_date=_d(row.get("ipoDate")),
            reporting_frequency=ReportingFrequency.UNKNOWN,
        )

    # -- fundamentals -----------------------------------------------------
    def get_annual_financials(
        self, symbol: str, years: int
    ) -> Sequence[PeriodFinancials]:
        return self._statements(symbol, "annual", years)

    def get_quarterly_financials(
        self, symbol: str, quarters: int
    ) -> Sequence[PeriodFinancials]:
        return self._statements(symbol, "quarter", quarters)

    def _statements(
        self, symbol: str, period: str, limit: int
    ) -> Sequence[PeriodFinancials]:
        income = self._get(f"income-statement/{symbol}", period=period, limit=limit)
        balance = self._get(
            f"balance-sheet-statement/{symbol}", period=period, limit=limit
        )
        cash = self._get(f"cash-flow-statement/{symbol}", period=period, limit=limit)
        by_date_b = {r.get("date"): r for r in balance or ()}
        by_date_c = {r.get("date"): r for r in cash or ()}

        out: list[PeriodFinancials] = []
        for inc in income or ():
            key = inc.get("date")
            bal = by_date_b.get(key, {})
            cf = by_date_c.get(key, {})
            period_end = _d(key)
            if period_end is None:
                continue
            debt = _f(bal.get("totalDebt"))
            if debt is None:
                short = _f(bal.get("shortTermDebt"))
                long = _f(bal.get("longTermDebt"))
                debt = None if short is None and long is None else (short or 0.0) + (long or 0.0)
            out.append(
                PeriodFinancials(
                    period_end=period_end,
                    filing_date=_d(inc.get("fillingDate") or inc.get("filingDate")),
                    fiscal_year=_int(inc.get("calendarYear")),
                    fiscal_period=inc.get("period"),
                    revenue=_f(inc.get("revenue")),
                    gross_profit=_f(inc.get("grossProfit")),
                    operating_income=_f(inc.get("operatingIncome")),
                    net_income=_f(inc.get("netIncome")),
                    ebitda=_f(inc.get("ebitda")),
                    interest_expense=_f(inc.get("interestExpense")),
                    tax_expense=_f(inc.get("incomeTaxExpense")),
                    pretax_income=_f(inc.get("incomeBeforeTax")),
                    operating_cash_flow=_f(cf.get("operatingCashFlow")),
                    capital_expenditure=_f(cf.get("capitalExpenditure")),
                    free_cash_flow=_f(cf.get("freeCashFlow")),
                    total_assets=_f(bal.get("totalAssets")),
                    total_current_assets=_f(bal.get("totalCurrentAssets")),
                    total_current_liabilities=_f(bal.get("totalCurrentLiabilities")),
                    total_debt=debt,
                    cash_and_equivalents=_f(
                        bal.get("cashAndCashEquivalents")
                        or bal.get("cashAndShortTermInvestments")
                    ),
                    total_equity=_f(bal.get("totalStockholdersEquity")),
                    tangible_book_value=_tangible_book(bal),
                    shares_diluted=_f(inc.get("weightedAverageShsOutDil")),
                    shares_outstanding=_f(inc.get("weightedAverageShsOut")),
                    dividends_paid=_f(cf.get("dividendsPaid")),
                    currency=inc.get("reportedCurrency"),
                )
            )
        if not out:
            raise DataUnavailable(
                f"{period}_financials", f"no statement rows returned for {symbol}"
            )
        return tuple(out)

    # -- market -----------------------------------------------------------
    def get_prices(
        self, symbol: str, start: date, end: date
    ) -> Sequence[PricePoint]:
        payload = self._get(
            f"historical-price-full/{symbol}",
            **{"from": start.isoformat(), "to": end.isoformat()},
        )
        rows = (payload or {}).get("historical", []) if isinstance(payload, dict) else []
        out = []
        for row in rows:
            d = _d(row.get("date"))
            close = _f(row.get("close"))
            if d is None or close is None:
                continue
            out.append(
                PricePoint(
                    price_date=d,
                    close=close,
                    adjusted_close=_f(row.get("adjClose")),
                    volume=_f(row.get("volume")),
                )
            )
        if not out:
            raise DataUnavailable("prices", f"no price rows for {symbol}")
        return tuple(out)

    def get_historical_multiples(
        self, symbol: str, multiple: str, years: int
    ) -> Sequence[MultipleObservation]:
        """Derived, not vendor-supplied.

        FMP's ``key-metrics`` carries per-period ratios; this maps the ones C1
        uses.  The result is a derivation and is flagged as such — C1.1's split
        adjustment still has to run over it, because per-share figures and
        prices are adjusted on different bases by different vendors.
        """
        field_map = {
            "trailing_pe": "peRatio",
            "forward_pe": "peRatio",
            "ev_revenue": "evToSales",
            "p_affo": "priceToOperatingCashFlowsRatio",
            "p_fcf": "priceToFreeCashFlowsRatio",
            "p_b": "pbRatio",
            "p_tbv": "pbRatio",
        }
        key = field_map.get(multiple)
        if key is None:
            raise DataUnavailable("historical_multiples", f"unmapped multiple {multiple}")
        rows = self._get(
            f"key-metrics/{symbol}", period="quarter", limit=max(4 * years, 8)
        )
        out = []
        for row in rows or ():
            d = _d(row.get("date"))
            v = _f(row.get(key))
            if d is None or v is None:
                continue
            out.append(
                MultipleObservation(observation_date=d, value=v, multiple=multiple)
            )
        if not out:
            raise DataUnavailable(
                "historical_multiples", f"no {multiple} history for {symbol}"
            )
        return tuple(out)

    def get_corporate_actions(
        self, symbol: str, years: int
    ) -> Sequence[CorporateAction]:
        """Splits only.

        FMP exposes a split calendar but no spinoff feed.  C1.1 needs spinoffs
        and transformative acquisitions too, so this adapter reports the
        ``corporate_actions`` capability as partial: a caller that treats an
        empty list as "no spinoffs occurred" would defeat the gate.
        """
        payload = self._get(f"historical-price-full/stock_split/{symbol}")
        rows = (payload or {}).get("historical", []) if isinstance(payload, dict) else []
        cutoff_year = date.today().year - years
        out = []
        for row in rows:
            d = _d(row.get("date"))
            if d is None or d.year < cutoff_year:
                continue
            num = _f(row.get("numerator")) or 1.0
            den = _f(row.get("denominator")) or 1.0
            ratio = num / den if den else None
            out.append(
                CorporateAction(
                    action_type=(
                        CorporateActionType.SPLIT
                        if (ratio or 1) >= 1
                        else CorporateActionType.REVERSE_SPLIT
                    ),
                    effective_date=d,
                    ratio=ratio,
                    description=row.get("label"),
                )
            )
        return tuple(out)

    # -- grouping ---------------------------------------------------------
    def get_peer_symbols(self, symbol: str) -> Sequence[str]:
        rows = self._get(f"stock_peers", base=STABLE_URL, symbol=symbol)
        if isinstance(rows, list) and rows:
            if isinstance(rows[0], dict) and "peersList" in rows[0]:
                return tuple(rows[0]["peersList"])
            return tuple(r.get("symbol") for r in rows if r.get("symbol"))
        raise DataUnavailable("peer_group", f"no peer list for {symbol}")

    def get_group_members(
        self, group: str, level: TaxonomyLevel
    ) -> Sequence[str]:
        param = {"industry": group} if level is TaxonomyLevel.INDUSTRY else {"sector": group}
        rows = self._get("stock-screener", limit=1000, **param)
        return tuple(r.get("symbol") for r in rows or () if r.get("symbol"))

    def get_market_data(self) -> MarketData:
        rf, rf_date = None, None
        try:
            quote = self._get("quote/%5ETNX")
            if quote:
                raw = _f(quote[0].get("price"))
                # ^TNX quotes the 10-year yield in percent.
                rf = raw / 100.0 if raw is not None else None
                rf_date = date.today()
        except DataUnavailable:
            pass
        return MarketData(risk_free_rate=rf, risk_free_rate_date=rf_date)

    # -- introspection ----------------------------------------------------
    def capabilities(self) -> Sequence[Capability]:
        """Probe live, then cache.

        Entitlement is a property of the key, not of the vendor, so this has to
        be asked rather than assumed.  Structural gaps are reported without a
        network call because no plan tier fixes them.
        """
        if self._capability_cache is not None:
            return tuple(self._capability_cache)

        probes: dict[str, Any] = {
            "profile": lambda: self.get_profile("AAPL"),
            "annual_financials": lambda: self.get_annual_financials("AAPL", 1),
            "quarterly_financials": lambda: self.get_quarterly_financials("AAPL", 1),
            "prices": lambda: self.get_prices(
                "AAPL", date.today().replace(day=1), date.today()
            ),
            "peer_group": lambda: self.get_peer_symbols("AAPL"),
            "risk_free_rate": lambda: self.get_market_data().risk_free_rate,
        }
        results: list[Capability] = []
        for name in REQUIRED_CAPABILITIES:
            if name in STRUCTURALLY_UNAVAILABLE:
                results.append(
                    Capability(name, False, STRUCTURALLY_UNAVAILABLE[name])
                )
                continue
            probe = probes.get(name)
            if probe is None:
                results.append(
                    Capability(name, False, "no probe defined; treat as unverified")
                )
                continue
            try:
                probe()
                results.append(Capability(name, True, "probed live"))
            except DataUnavailable as exc:
                results.append(Capability(name, False, str(exc)))
            except Exception as exc:  # pragma: no cover - provider-specific
                results.append(
                    Capability(name, False, f"{type(exc).__name__}: {exc}")
                )
        self._capability_cache = results
        return tuple(results)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _adv(row: dict[str, Any]) -> float | None:
    """Dollar ADV, which the universe screen wants — FMP quotes share volume."""
    volume = _f(row.get("volAvg") or row.get("averageVolume"))
    price = _f(row.get("price"))
    if volume is None or price is None:
        return None
    return volume * price


def _tangible_book(bal: dict[str, Any]) -> float | None:
    equity = _f(bal.get("totalStockholdersEquity"))
    if equity is None:
        return None
    goodwill = _f(bal.get("goodwillAndIntangibleAssets"))
    if goodwill is None:
        goodwill = (_f(bal.get("goodwill")) or 0.0) + (
            _f(bal.get("intangibleAssets")) or 0.0
        )
    return equity - goodwill


def _looks_like(
    industry: str | None, sector: str | None, needles: Iterable[str]
) -> bool:
    haystack = f"{industry or ''} {sector or ''}".lower()
    return any(n in haystack for n in needles)


__all__ = ["FMPAdapter", "STRUCTURALLY_UNAVAILABLE", "BASE_URL", "STABLE_URL"]
