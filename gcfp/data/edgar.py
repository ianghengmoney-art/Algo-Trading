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
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

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
from . import xbrl
from .adapter import Capability, DataAdapter, DataUnavailable, REQUIRED_CAPABILITIES

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
#: Company lists by SIC code.  The only free way to ask "who else is in this
#: industry" without fetching every filer's submissions one at a time.  It
#: serves Atom rather than JSON, and the shape is not contractual, so the
#: parser below is deliberately tolerant and a failure degrades to no
#: candidates rather than raising.
SIC_BROWSE_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&SIC={sic}"
    "&type=10-K&dateb=&owner=include&count={count}&start={start}&output=atom"
)
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
    #: Symbols this adapter should resolve without consulting the SEC ticker
    #: index — the escape hatch for delisted companies, which the index drops.
    cik_overrides: dict[str, int] = field(default_factory=dict)
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
        """Resolve a symbol to its CIK.

        Three routes, in order: an operator-supplied override, a CIK written
        directly (``CIK0000719739`` or ``CIK:719739``), then the SEC ticker
        index.

        The first two exist because of a limitation with real consequences:
        ``company_tickers.json`` lists **currently registered** tickers only.
        A company that delisted is not in it, even though its filings remain
        on EDGAR in full.  A universe built from that index therefore contains
        only survivors, and a backtest run over it will report the returns of
        companies that made it — which is the single most flattering error a
        backtest can make.  Filings are addressable by CIK forever, so a
        delisted name stays reachable if it is addressed that way.
        """
        raw = symbol.strip().upper()
        override = {k.upper(): v for k, v in (self.cik_overrides or {}).items()}
        if raw in override:
            return int(override[raw])

        direct = re.fullmatch(r"CIK[:\-_]?0*(\d{1,10})", raw)
        if direct:
            return int(direct.group(1))

        if self._ticker_map is None:
            payload = self._get_json(TICKERS_URL, cache_key="company_tickers")
            rows = payload.values() if isinstance(payload, dict) else payload
            self._ticker_map = {
                str(r["ticker"]).upper(): int(r["cik_str"])
                for r in rows
                if r.get("ticker") and r.get("cik_str") is not None
            }
        cik = self._ticker_map.get(raw)
        if cik is None:
            raise DataUnavailable(
                "cik",
                f"{symbol} not in the SEC ticker index. That index lists "
                "currently registered tickers only, so a delisted company is "
                "absent from it while its filings remain on EDGAR. Address it "
                "by CIK instead (e.g. 'CIK0000719739') or pass a cik_overrides "
                "entry. Building a universe from the index alone gives a "
                "survivors-only backtest.",
            )
        return cik

    def _get_text(self, url: str, cache_key: str | None = None) -> str:
        """Fetch a non-JSON document, sharing the throttle and the cache."""
        if cache_key and self.cache_dir:
            cached = self.cache_dir / f"{cache_key}.txt"
            if cached.exists():
                return cached.read_text()
        self._throttle()
        resp = self.session.get(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        if cache_key and self.cache_dir:
            (self.cache_dir / f"{cache_key}.txt").write_text(resp.text)
        return resp.text

    def cik_to_ticker(self) -> dict[int, str]:
        """The ticker index, inverted.

        A CIK can carry several tickers (share classes).  The alphabetically
        first is taken so the mapping is deterministic run to run.
        """
        out: dict[int, str] = {}
        for ticker, cik in sorted(self.all_tickers().items()):
            out.setdefault(cik, ticker)
        return out

    def symbols_by_sic(self, sic: str | int, limit: int = 80) -> list[str]:
        """Tickers of other filers sharing an SIC code.

        This is what makes C2 workable on EDGAR.  Sampling the market and
        hoping four names land in the same industry does not work: an
        alphabetical or random slice of a few hundred tickers will contain no
        other oil royalty trader, no other construction-machinery maker, and
        C2 reports "no peers" for a reason that is about the sample rather
        than about the market.  Asking the industry directly removes the
        guesswork.

        Returns ``[]`` rather than raising when the listing cannot be had:
        an empty peer set is a finding C2 already knows how to report, and a
        transport failure here must not take down a whole screen.
        """
        try:
            code = int(str(sic).strip())
        except (TypeError, ValueError):
            return []

        by_cik = self.cik_to_ticker()
        found: list[str] = []
        seen: set[int] = set()
        page = 100
        for start in range(0, max(limit, 1), page):
            try:
                body = self._get_text(
                    SIC_BROWSE_URL.format(sic=code, count=page, start=start),
                    cache_key=f"sic_{code}_{start}",
                )
            except Exception:
                break
            ciks = [int(m) for m in re.findall(r"CIK=(\d{1,10})", body)]
            if not ciks:
                break
            for cik in ciks:
                if cik in seen:
                    continue
                seen.add(cik)
                ticker = by_cik.get(cik)
                if ticker:
                    found.append(ticker)
                if len(found) >= limit:
                    return found
        return found

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
            industry_code=str(sic) if sic is not None else None,
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
            "shares_basic", "dividends_paid", "cost_of_revenue",
            "funds_from_operations",
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

            # GrossProfit is frequently untagged; deriving it from revenue
            # less cost of revenue keeps D4's margin-stability sub-component
            # scoreable instead of silently zero.
            gross_profit = duration("gross_profit")
            if gross_profit is None:
                revenue = duration("revenue")
                cost = duration("cost_of_revenue")
                if revenue is not None and cost is not None:
                    gross_profit = revenue - abs(cost)

            operating_income = duration("operating_income")
            da = duration("depreciation_amortization")
            ebitda = (
                operating_income + da
                if operating_income is not None and da is not None
                else None
            )

            debt_value, debt_basis = self._total_debt(payload, end)

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
                    gross_profit=gross_profit,
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
                    total_debt=debt_value,
                    debt_basis=debt_basis,
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

    #: Fields whose presence proves the parser can read this filer's balance
    #: sheet.  Without them an absent debt tag is uninformative — the whole
    #: statement may simply have failed to resolve.
    _BALANCE_SHEET_PROOF: tuple[str, ...] = ("total_assets", "total_equity")

    def _total_debt(
        self, payload: dict[str, Any], end: date
    ) -> tuple[float | None, str | None]:
        """Debt at a period end, with the basis on which it was arrived at.

        Four outcomes, and the caller is told which:

        ``tagged``
            the filer reports a single combined debt figure.
        ``summed``
            non-current, current and short-term parts added together.  The
            three buckets are disjoint by construction (see ``TAG_CHAINS``),
            so nothing is counted twice.
        ``long_term_only``
            only a total-including-current tag resolved.
        ``inferred_zero``
            no debt tag resolved at this period end, *and* none appears
            anywhere in the filer's filing history, *and* the balance sheet
            otherwise reads cleanly.  Texas Pacific Land is the case this
            exists for: genuinely debt-free, and previously indistinguishable
            from a company whose debt tags this parser could not read.

        Anything else returns ``(None, None)``.  A zero is only ever inferred,
        never assumed: a company with no debt and a company whose debt could
        not be read must not look alike to A2.
        """
        combined = xbrl.latest_instant(
            payload, "total_debt_combined", end, as_of=self.as_of
        )
        if combined is not None:
            return combined.value, "tagged"

        parts = [
            xbrl.latest_instant(payload, name, end, as_of=self.as_of)
            for name in (
                "long_term_debt_noncurrent",
                "long_term_debt_current",
                "short_term_debt",
            )
        ]
        present = [p.value for p in parts if p is not None]
        if present:
            return sum(present), "summed"

        whole = xbrl.latest_instant(
            payload, "long_term_debt_including_current", end, as_of=self.as_of
        )
        if whole is not None:
            return whole.value, "long_term_only"

        return self._infer_zero_debt(payload, end)

    def _infer_zero_debt(
        self, payload: dict[str, Any], end: date
    ) -> tuple[float | None, str | None]:
        """Zero, but only when the silence is the filer's and not the parser's.

        Requires both that the balance sheet read cleanly at this period end
        and that no debt tag — nor any evidence of debt activity, such as
        interest paid or borrowings repaid — appears anywhere in the filing
        history available at ``as_of``.
        """
        for field in self._BALANCE_SHEET_PROOF:
            if xbrl.latest_instant(payload, field, end, as_of=self.as_of) is None:
                return None, None

        debt_fields = (
            "total_debt_combined",
            "long_term_debt_noncurrent",
            "long_term_debt_current",
            "short_term_debt",
            "long_term_debt_including_current",
            "debt_existence_evidence",
        )
        for field in debt_fields:
            if xbrl.has_any_fact(payload, field, as_of=self.as_of):
                # The filer does carry (or has carried) debt, so reading none
                # at this period end is a parsing miss, not a debt-free
                # balance sheet.  Refuse rather than invent a zero.
                return None, None

        return 0.0, "inferred_zero"

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
        """Disposals and acquisitions, from 8-K Item 2.01.

        C1.1 needs to know when a company stopped being the company its own
        price history describes. EDGAR publishes no corporate-action feed, but
        it does publish 8-K filings with standardised item numbers, and
        **Item 2.01 — Completion of Acquisition or Disposition of Assets** is
        the one that fires on a spinoff, a major divestiture, or a
        transformative purchase.

        This is deliberately imprecise in one direction and not the other. Item
        2.01 also covers ordinary asset sales, so this over-reports rather than
        under-reports: a false discontinuity costs a shortened C1 window and a
        logged flag, while a missed spinoff leaves the anchor silently
        comparing a company to a predecessor that no longer exists. Given the
        choice, C1.1 should see too much rather than too little.

        The returned actions carry ``market_cap_share=None``, because the 8-K
        index does not size the transaction. C1.1 treats a SPINOFF as
        transformative regardless of size, so the flag still lands; an
        ACQUISITION without a size cannot clear the 25% test and is reported
        for a human to judge.
        """
        subs = self._submissions(symbol)
        recent = (subs.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        items = recent.get("items") or []

        cutoff_year = date.today().year - years
        out: list[CorporateAction] = []
        for index, form in enumerate(forms):
            if str(form) not in ("8-K", "8-K/A"):
                continue
            filed = xbrl._parse_date(dates[index]) if index < len(dates) else None
            if filed is None or filed.year < cutoff_year:
                continue
            if self.as_of is not None and filed > self.as_of:
                continue
            item_text = str(items[index]) if index < len(items) else ""
            if "2.01" not in item_text:
                continue
            out.append(
                CorporateAction(
                    action_type=CorporateActionType.DIVESTITURE,
                    effective_date=filed,
                    market_cap_share=None,
                    description=(
                        f"8-K Item 2.01 filed {filed.isoformat()} — completion of "
                        "acquisition or disposition of assets. Size not stated in "
                        "the filing index; confirm whether this was transformative."
                    ),
                )
            )
        return tuple(out)

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
            elif name == "corporate_actions":
                out.append(
                    Capability(
                        name, True,
                        "8-K Item 2.01 disposals and acquisitions. Over-reports "
                        "(Item 2.01 also covers ordinary asset sales) and does "
                        "not size the transaction, so C1.1 sees candidates to "
                        "judge rather than a clean feed",
                    )
                )
            elif name == "peer_group":
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
