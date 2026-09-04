"""SEC EDGAR adapter — free, official, and natively point-in-time.

EDGAR is where the paid vendors get their fundamentals.  Using it directly
costs nothing, needs no key, and carries one property no retail vendor sells:
every fact arrives with the date it was *filed*, so a backtest can be run
against what was actually knowable at the time.  §13.8 asks for point-in-time
data and warns that without it results are inflated by an unknown amount;
this adapter is the cheapest way to satisfy that.

What it cannot supply:

* **Prices.**  EDGAR holds filings, not quotes.  Pair it with a price source
  via :class:`~gcfp.data.composite.CompositeAdapter`.
* **Analyst estimates.**  So C3's PEGY reports "n/a" — which the spec
  explicitly permits — and CORE-GROWTH's C1 anchor uses a trailing multiple
  by explicit configuration rather than silently degrading.
* **REIT AFFO and insurer combined ratios.**  Most filers report these as
  custom XBRL extensions the companyfacts API does not expose.  The adapter
  reports the capability as unsupported rather than returning a wrong number,
  which routes those classifications to a reported data gap.

SEC access policy requires a descriptive User-Agent with contact details and
asks for no more than ten requests a second.  Both are enforced here.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from ..types import (
    CompanyProfile,
    CorporateAction,
    MarketData,
    MultipleObservation,
    PeriodFinancials,
    PricePoint,
    ReportingFrequency,
    TaxonomyLevel,
)
from . import xbrl
from .adapter import Capability, DataAdapter, DataUnavailable, REQUIRED_CAPABILITIES

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

#: SEC asks for no more than 10 requests/second.  This adapter stays well
#: under, because a weekly screen has no reason to hurry and a blocked IP
#: costs far more than the minutes saved.
MIN_REQUEST_INTERVAL = 0.15

#: Capabilities EDGAR structurally cannot serve, listed so the §18 coverage
#: report distinguishes them from things a different plan or key would fix.
STRUCTURALLY_UNAVAILABLE: dict[str, str] = {
    "prices": "EDGAR holds filings, not quotes; pair with a price source",
    "historical_multiples": (
        "no multiple series; C1 is reconstructed from prices x per-share "
        "fundamentals by the composite adapter"
    ),
    "forward_estimates": (
        "no analyst estimates; C3 reports PEGY n/a and CORE-GROWTH uses a "
        "trailing anchor by explicit configuration"
    ),
    "reit_ffo_affo": (
        "FFO/AFFO are almost always custom XBRL extensions, not us-gaap tags; "
        "the REIT path cannot be valued from this source alone"
    ),
    "insurer_combined_ratio": (
        "combined ratio is not a us-gaap tag; the insurer path cannot be "
        "valued from this source alone"
    ),
    "fx_rates": "EDGAR publishes no exchange rates",
    "risk_free_rate": "EDGAR publishes no Treasury yields",
}

#: SIC divisions, used as the coarsest rung of the grouping ladder.  SIC is a
#: worse taxonomy than GICS — broader and older — but it is free, official, and
#: attached to every filer, which makes it the honest substitute.
SIC_DIVISIONS: tuple[tuple[int, int, str], ...] = (
    (100, 999, "Agriculture, Forestry & Fishing"),
    (1000, 1499, "Mining"),
    (1500, 1799, "Construction"),
    (2000, 3999, "Manufacturing"),
    (4000, 4999, "Transportation & Utilities"),
    (5000, 5199, "Wholesale Trade"),
    (5200, 5999, "Retail Trade"),
    (6000, 6799, "Finance, Insurance & Real Estate"),
    (7000, 8999, "Services"),
    (9100, 9999, "Public Administration"),
)

#: SIC ranges that decide A6's structure flags.  These are the tags that
#: outrank profitability in the ambiguity rule, so getting them from an
#: official classification rather than a keyword match on a description
#: matters.
SIC_BANK_RANGES = ((6020, 6036), (6060, 6062), (6099, 6099), (6110, 6141))
SIC_INSURER_RANGES = ((6310, 6411),)
SIC_REIT_RANGES = ((6798, 6798),)


def _in_ranges(sic: int | None, ranges: Sequence[tuple[int, int]]) -> bool:
    if sic is None:
        return False
    return any(low <= sic <= high for low, high in ranges)


def sic_division(sic: int | None) -> str | None:
    if sic is None:
        return None
    for low, high, name in SIC_DIVISIONS:
        if low <= sic <= high:
            return name
    return None


@dataclass
class EdgarAdapter(DataAdapter):
    """Fundamentals from SEC filings.

    ``user_agent`` must identify you with contact details — SEC policy, and
    they do block anonymous scrapers.  ``as_of`` restricts every fact to what
    had been filed by that date, which is how a point-in-time backtest is run.
    """

    user_agent: str
    session: Any = None
    timeout: float = 30.0
    max_retries: int = 3
    cache_dir: Path | None = None
    as_of: date | None = None
    name: str = "edgar"
    _last_request: float = field(default=0.0, repr=False)
    _ticker_map: dict[str, int] | None = field(default=None, repr=False)
    _facts_cache: dict[int, dict[str, Any]] = field(default_factory=dict, repr=False)
    _submissions_cache: dict[int, dict[str, Any]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if "@" not in self.user_agent:
            raise ValueError(
                "SEC requires a User-Agent identifying you with contact details, "
                'e.g. "gcfp-research you@example.com"'
            )
        if self.session is None:
            import requests

            self.session = requests.Session()
        if self.cache_dir is not None:
            self.cache_dir = Path(self.cache_dir)
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- transport --------------------------------------------------------
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        self._last_request = time.monotonic()

    def _get_json(self, url: str, cache_key: str | None = None) -> Any:
        if cache_key and self.cache_dir:
            cached = self.cache_dir / f"{cache_key}.json"
            if cached.exists():
                return json.loads(cached.read_text())

        last: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.session.get(
                    url,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept-Encoding": "gzip, deflate",
                    },
                    timeout=self.timeout,
                )
            except Exception as exc:
                last = exc
                time.sleep(2.0**attempt)
                continue
            if resp.status_code == 404:
                raise DataUnavailable(url, "not found on EDGAR")
            if resp.status_code in (403, 429):
                # Almost always the rate limit or a missing User-Agent.
                last = RuntimeError(f"HTTP {resp.status_code}")
                time.sleep(2.0 ** (attempt + 1))
                continue
            resp.raise_for_status()
            payload = resp.json()
            if cache_key and self.cache_dir:
                (self.cache_dir / f"{cache_key}.json").write_text(json.dumps(payload))
            return payload
        raise DataUnavailable(url, f"exhausted retries: {last}")

    # -- identity ---------------------------------------------------------
    def ticker_to_cik(self, symbol: str) -> int:
        if self._ticker_map is None:
            payload = self._get_json(TICKERS_URL, cache_key="company_tickers")
            rows = payload.values() if isinstance(payload, dict) else payload
            self._ticker_map = {
                str(r["ticker"]).upper(): int(r["cik_str"])
                for r in rows
                if r.get("ticker") and r.get("cik_str") is not None
            }
        cik = self._ticker_map.get(symbol.upper())
        if cik is None:
            raise DataUnavailable("cik", f"{symbol} not in the SEC ticker index")
        return cik

    def all_tickers(self) -> dict[str, int]:
        """Every ticker the SEC indexes — the raw material for the universe."""
        if self._ticker_map is None:
            self.ticker_to_cik("AAPL")
        return dict(self._ticker_map or {})

    def _facts(self, symbol: str) -> dict[str, Any]:
        cik = self.ticker_to_cik(symbol)
        if cik not in self._facts_cache:
            self._facts_cache[cik] = self._get_json(
                COMPANYFACTS_URL.format(cik=cik), cache_key=f"facts_{cik:010d}"
            )
        return self._facts_cache[cik]

    def _submissions(self, symbol: str) -> dict[str, Any]:
        cik = self.ticker_to_cik(symbol)
        if cik not in self._submissions_cache:
            self._submissions_cache[cik] = self._get_json(
                SUBMISSIONS_URL.format(cik=cik), cache_key=f"sub_{cik:010d}"
            )
        return self._submissions_cache[cik]

    def get_profile(self, symbol: str) -> CompanyProfile:
        subs = self._submissions(symbol)
        sic_raw = subs.get("sic")
        try:
            sic = int(sic_raw) if sic_raw else None
        except (TypeError, ValueError):
            sic = None

        filings = self._recent_filings(subs)
        last_report, frequency = self._reporting_pattern(filings)

        return CompanyProfile(
            symbol=symbol.upper(),
            name=subs.get("name") or symbol.upper(),
            listing_currency="USD",
            reporting_currency="USD",
            exchange=self._primary_exchange(subs),
            country=(subs.get("addresses", {}).get("business", {}) or {}).get(
                "stateOrCountry"
            ),
            market_cap=None,  # needs a price; the composite adapter fills it
            adv_3m_usd=None,
            beta=None,  # computed from price history, not supplied
            sector=sic_division(sic),
            industry=(subs.get("sicDescription") or None),
            sub_industry=None,
            gics_sub_industry_code=None,
            # SIC is an official classification but it is not GICS, and the
            # difference is logged everywhere a grouping is used.
            taxonomy_level=(
                TaxonomyLevel.INDUSTRY if subs.get("sicDescription") else TaxonomyLevel.SECTOR
            ),
            taxonomy_is_gics=False,
            is_adr=False,
            underlying_currency=None,
            is_reit=_in_ranges(sic, SIC_REIT_RANGES),
            is_bank=_in_ranges(sic, SIC_BANK_RANGES),
            is_insurer=_in_ranges(sic, SIC_INSURER_RANGES),
            ipo_date=None,
            reporting_frequency=frequency,
            last_report_date=last_report,
        )

    @staticmethod
    def _primary_exchange(subs: dict[str, Any]) -> str | None:
        exchanges = subs.get("exchanges") or []
        return str(exchanges[0]) if exchanges else None

    @staticmethod
    def _recent_filings(subs: dict[str, Any]) -> list[tuple[date, str]]:
        recent = (subs.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        out: list[tuple[date, str]] = []
        for form, filed in zip(forms, dates):
            parsed = xbrl._parse_date(filed)
            if parsed and form in ("10-K", "10-Q", "20-F", "40-F", "6-K"):
                out.append((parsed, str(form)))
        return sorted(out, reverse=True)

    def _reporting_pattern(
        self, filings: Sequence[tuple[date, str]]
    ) -> tuple[date | None, ReportingFrequency]:
        """H1 keys its cadence off how often this filer actually reports.

        A 20-F filer reports annually and a 10-Q filer quarterly; assuming
        quarterly for both is what v3 flaw 11 was about.
        """
        if self.as_of is not None:
            filings = [f for f in filings if f[0] <= self.as_of]
        if not filings:
            return None, ReportingFrequency.UNKNOWN

        last = filings[0][0]
        forms = [form for _, form in filings[:8]]
        if "10-Q" in forms:
            return last, ReportingFrequency.QUARTERLY
        if any(f in ("20-F", "40-F") for f in forms):
            return last, ReportingFrequency.SEMIANNUAL
        return last, ReportingFrequency.UNKNOWN

    # -- fundamentals -----------------------------------------------------
    def get_annual_financials(
        self, symbol: str, years: int
    ) -> Sequence[PeriodFinancials]:
        return self._periods(symbol, annual=True, limit=years)

    def get_quarterly_financials(
        self, symbol: str, quarters: int
    ) -> Sequence[PeriodFinancials]:
        return self._periods(symbol, annual=False, limit=quarters)

    def _periods(
        self, symbol: str, *, annual: bool, limit: int
    ) -> Sequence[PeriodFinancials]:
        payload = self._facts(symbol)
        fields = [
            "revenue", "net_income", "operating_cash_flow", "capital_expenditure",
            "operating_income", "gross_profit", "interest_expense", "tax_expense",
            "pretax_income", "depreciation_amortization", "shares_diluted",
            "shares_basic", "dividends_paid", "funds_from_operations",
            "adjusted_funds_from_operations",
        ]
        resolved = {
            f: xbrl.select_facts(payload, f, annual=annual, as_of=self.as_of)
            for f in fields
        }

        # Period ends come from revenue where possible, net income otherwise —
        # a company with no revenue tag still has periods worth evaluating.
        ends = sorted(
            set(resolved["revenue"]) | set(resolved["net_income"]), reverse=True
        )
        if not ends:
            raise DataUnavailable(
                "financials",
                f"no revenue or net-income facts for {symbol} "
                f"({'annual' if annual else 'quarterly'})",
            )

        out: list[PeriodFinancials] = []
        for end in ends[:limit]:
            def duration(name: str) -> float | None:
                fact = resolved[name].get(end)
                return fact.value if fact else None

            def instant(name: str) -> float | None:
                fact = xbrl.latest_instant(payload, name, end, as_of=self.as_of)
                return fact.value if fact else None

            filed = None
            for name in ("revenue", "net_income"):
                fact = resolved[name].get(end)
                if fact:
                    filed = fact.filed if filed is None else min(filed, fact.filed)

            ocf = duration("operating_cash_flow")
            capex = duration("capital_expenditure")
            # XBRL reports capex as a positive outflow; the rest of this
            # system follows the vendor convention of a negative number, so it
            # is negated once here rather than special-cased at every use.
            capex_signed = -abs(capex) if capex is not None else None
            fcf = (
                ocf + capex_signed
                if ocf is not None and capex_signed is not None
                else None
            )

            operating_income = duration("operating_income")
            da = duration("depreciation_amortization")
            ebitda = (
                operating_income + da
                if operating_income is not None and da is not None
                else None
            )

            equity = instant("total_equity")
            goodwill = instant("goodwill") or 0.0
            intangibles = instant("intangible_assets") or 0.0
            tangible_book = (
                equity - goodwill - intangibles if equity is not None else None
            )

            out.append(
                PeriodFinancials(
                    period_end=end,
                    filing_date=filed,
                    fiscal_year=end.year,
                    fiscal_period="FY" if annual else None,
                    revenue=duration("revenue"),
                    gross_profit=duration("gross_profit"),
                    operating_income=operating_income,
                    net_income=duration("net_income"),
                    ebitda=ebitda,
                    interest_expense=duration("interest_expense"),
                    tax_expense=duration("tax_expense"),
                    pretax_income=duration("pretax_income"),
                    operating_cash_flow=ocf,
                    capital_expenditure=capex_signed,
                    free_cash_flow=fcf,
                    total_assets=instant("total_assets"),
                    total_current_assets=instant("total_current_assets"),
                    total_current_liabilities=instant("total_current_liabilities"),
                    total_debt=self._total_debt(payload, end),
                    cash_and_equivalents=instant("cash_and_equivalents"),
                    total_equity=equity,
                    tangible_book_value=tangible_book,
                    shares_diluted=duration("shares_diluted") or duration("shares_basic"),
                    shares_outstanding=instant("shares_outstanding"),
                    dividends_paid=duration("dividends_paid"),
                    funds_from_operations=duration("funds_from_operations"),
                    adjusted_funds_from_operations=duration(
                        "adjusted_funds_from_operations"
                    ),
                    currency="USD",
                )
            )
        return tuple(out)

    def _total_debt(self, payload: dict[str, Any], end: date) -> float | None:
        """Debt, summed from its parts when no combined tag exists.

        Returns ``None`` rather than 0.0 when nothing is tagged: a company with
        no debt and a company whose debt could not be read must not look alike
        to A2.
        """
        combined = xbrl.latest_instant(
            payload, "total_debt_combined", end, as_of=self.as_of
        )
        if combined is not None:
            return combined.value

        parts = [
            xbrl.latest_instant(payload, name, end, as_of=self.as_of)
            for name in ("long_term_debt_noncurrent", "long_term_debt_current", "short_term_debt")
        ]
        present = [p.value for p in parts if p is not None]
        if not present:
            return None
        return sum(present)

    # -- unavailable from EDGAR ------------------------------------------
    def get_prices(self, symbol: str, start: date, end: date) -> Sequence[PricePoint]:
        raise DataUnavailable(
            "prices", "EDGAR holds filings, not quotes; pair with a price source"
        )

    def get_historical_multiples(
        self, symbol: str, multiple: str, years: int
    ) -> Sequence[MultipleObservation]:
        raise DataUnavailable(
            "historical_multiples",
            "no multiple series in EDGAR; the composite adapter reconstructs C1 "
            "from prices and per-share fundamentals",
        )

    def get_corporate_actions(
        self, symbol: str, years: int
    ) -> Sequence[CorporateAction]:
        """Not derivable from companyfacts.

        Splits show up in price feeds; spinoffs appear in 8-K narrative text
        that this adapter does not parse.  Returning an empty tuple would read
        as "no spinoffs occurred", which would defeat C1.1, so this raises.
        """
        raise DataUnavailable(
            "corporate_actions",
            "EDGAR companyfacts carries no corporate-action feed; C1.1 cannot "
            "detect spinoffs from this source and an empty list must not be "
            "read as 'none occurred'",
        )

    def get_peer_symbols(self, symbol: str) -> Sequence[str]:
        raise DataUnavailable(
            "peer_group",
            "EDGAR publishes no peer lists; peers are built from the universe "
            "by SIC grouping, size and growth (see gcfp.universe)",
        )

    def get_group_members(self, group: str, level: TaxonomyLevel) -> Sequence[str]:
        raise DataUnavailable(
            "group_members",
            "build groupings from the universe rather than per-request",
        )

    def get_market_data(self) -> MarketData:
        """EDGAR supplies no market-wide inputs.

        The risk-free rate and FX rates come from elsewhere; returning an empty
        MarketData keeps B1 failing loudly on a missing rate rather than
        quietly substituting one.
        """
        return MarketData()

    # -- introspection ----------------------------------------------------
    def capabilities(self) -> Sequence[Capability]:
        supported = {
            "profile", "annual_financials", "quarterly_financials",
            "share_count_history", "cash_burn", "bank_tangible_book",
            "point_in_time",
        }
        out = []
        for name in REQUIRED_CAPABILITIES:
            if name in STRUCTURALLY_UNAVAILABLE:
                out.append(Capability(name, False, STRUCTURALLY_UNAVAILABLE[name]))
            elif name in supported:
                detail = (
                    "as-filed with filing dates — natively point-in-time"
                    if name == "point_in_time"
                    else "from XBRL company facts"
                )
                out.append(Capability(name, True, detail))
            elif name == "gics_sub_industry":
                out.append(
                    Capability(
                        name, False,
                        "SIC codes only; official but coarser than GICS, and "
                        "logged as VENDOR-SUBSTITUTE wherever a grouping is used",
                    )
                )
            elif name in ("peer_group", "corporate_actions"):
                out.append(
                    Capability(name, False, "built elsewhere; see gcfp.universe")
                )
            else:
                out.append(Capability(name, False, "not served by EDGAR"))
        return tuple(out)

    def field_coverage(self, symbol: str) -> dict[str, str | None]:
        """Which XBRL tag supplies each field for this filer.

        Useful when a name produces surprising gate values: the answer is
        usually that its filer tags something unusually, and the fix is to
        extend the chain rather than to special-case the company.
        """
        return xbrl.available_fields(self._facts(symbol))


__all__ = ["EdgarAdapter", "STRUCTURALLY_UNAVAILABLE", "sic_division"]
