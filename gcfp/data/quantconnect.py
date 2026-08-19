"""QuantConnect adapter.

Maps QuantConnect's Morningstar-sourced ``Fundamental`` object onto the GCFP
data model. Written against the published ``quantconnect-stubs`` API surface
rather than from memory, and duck-typed throughout so this module imports,
and is testable, outside the QC runtime.

Why this source matters: it is the first one available to this system that is
genuinely point-in-time. FMP's peer lists are current-membership and its
statements are as-restated, which made the Section 12 backtest uncertifiable.
QC's fundamentals are as-of-date, so the backtest windows the spec makes
mandatory become runnable.

Two structural wins over the FMP path, both of which close findings from the
original coverage report:

  * ``company_reference.is_reit`` and ``industry_template_code`` are explicit
    fields, and ``asset_classification.sic`` carries the SEC-filed code. The A6
    structural router no longer has to infer REIT/BANK/INSURER from vendor
    industry strings -- the mechanism that mislabelled Caterpillar as
    "Agricultural - Machinery" and is failure mode #1 in Section 13.
  * ``financial_statements.period_auditor`` and ``auditor_report_status`` make
    part of gate A4 enforceable. FMP exposed nothing at all here.

One gap survives: Morningstar carries no recurring-maintenance-capex or
straight-line-rent line, so REIT **AFFO** remains uncomputable and Module B4
falls back to P/FFO with its warning. FFO itself is derivable.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable, Iterable, Optional, Sequence

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

# Morningstar period keys on a MultiPeriodField, most-preferred first.
ANNUAL_PERIODS = ("12M", "1Y", "TTM")
QUARTERLY_PERIODS = ("3M", "Q")

# company_reference.industry_template_code -- Morningstar's statement-template
# code, which is the authoritative signal for which valuation path applies.
TEMPLATE_BANK = "B"
TEMPLATE_INSURANCE = "I"
TEMPLATE_REIT = "R"
TEMPLATE_UTILITY = "U"

# SIC ranges, used only to corroborate the template code.
SIC_BANK = range(6000, 6300)
SIC_INSURANCE = range(6300, 6500)
SIC_REIT = range(6798, 6799)


def _num(value: Any) -> Optional[float]:
    """Coerce to float, refusing anything that is not a real number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def period_value(field: Any, periods: Sequence[str]) -> Optional[float]:
    """Read one period off a ``MultiPeriodField``.

    QC returns 0.0 rather than null for an absent figure, which is exactly the
    silent-zero this system refuses to act on. ``has_period_value`` and
    ``has_value`` are what separate "reported zero" from "not reported", so
    they are consulted before the number is trusted; a field that answers
    neither is treated as missing rather than as zero.
    """
    if field is None:
        return None

    for period in periods:
        try:
            if field.has_period_value(period):
                return _num(field.get_period_value(period))
        except (AttributeError, TypeError, KeyError):
            break

    try:
        if field.has_value:
            return _num(field.value)
        return None
    except (AttributeError, TypeError):
        pass

    return _num(field)


def _to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _attr(obj: Any, *path: str) -> Any:
    """Walk an attribute path, returning None the moment it breaks."""
    current = obj
    for name in path:
        if current is None:
            return None
        current = getattr(current, name, None)
    return current


class QCDataAdapter(DataAdapter):
    """Serves GCFP from QuantConnect fundamentals.

    ``fundamentals`` is the current universe selection, keyed by symbol string.
    ``history_provider`` is called as ``history_provider(symbol, years)`` and
    must return ``Fundamental`` snapshots oldest-first; inside an algorithm
    that is a thin wrapper over ``algorithm.history``. It is injected rather
    than called directly so this class can be tested without the QC runtime.
    """

    name = "quantconnect"

    def __init__(
        self,
        fundamentals: Optional[dict[str, Any]] = None,
        history_provider: Optional[Callable[[str, int], Sequence[Any]]] = None,
        history_years: int = 10,
        as_of: Optional[date] = None,
    ) -> None:
        self._fundamentals: dict[str, Any] = dict(fundamentals or {})
        self._history_provider = history_provider
        self.history_years = history_years
        self.as_of = as_of
        self._history_cache: dict[str, Sequence[Any]] = {}

    # -- universe plumbing -------------------------------------------------
    def set_universe(self, fundamentals: Iterable[Any]) -> None:
        """Replace the current snapshot from a universe-selection callback."""
        self._fundamentals = {str(f.symbol): f for f in fundamentals}
        self._history_cache.clear()

    def symbols(self) -> list[str]:
        return sorted(self._fundamentals)

    def capabilities(self) -> set[Capability]:
        served = {
            Capability.PROFILE,
            Capability.ANNUAL_STATEMENTS,
            Capability.QUARTERLY_STATEMENTS,
            Capability.SHARE_COUNT_HISTORY,
            Capability.CASH_BURN_HISTORY,
            Capability.PRICE_HISTORY,
            Capability.MULTIPLE_HISTORY,
            Capability.PEER_LIST,
            Capability.SECTOR_AGGREGATES,
            Capability.BANK_TANGIBLE_BOOK,
            Capability.INSURER_UNDERWRITING,
            Capability.REIT_FFO,
            Capability.DELISTED_COVERAGE,
            # The reason this adapter exists. Morningstar fundamentals are
            # as-of-date, so a backtest on them is not reading the future.
            Capability.POINT_IN_TIME,
            # Partial: period_auditor and auditor_report_status are served, so
            # auditor changes are detectable. Restatements, going-concern
            # language and late filings are not, and gate A4 keeps returning
            # DATA_GAP for those.
            Capability.FILING_FLAGS,
        }
        # forward_pe_ratio is an analyst-derived figure, which is enough for
        # the C3 PEGY check. There is no earnings calendar, so Module E's
        # blackout never defers on this source -- see get_estimates.
        served.add(Capability.ANALYST_ESTIMATES)
        return served

    def _fundamental(self, symbol: str) -> Any:
        f = self._fundamentals.get(symbol)
        if f is None:
            raise DataUnavailable(symbol, "fundamental", "not in the current universe selection")
        return f

    def _history(self, symbol: str) -> Sequence[Any]:
        if symbol in self._history_cache:
            return self._history_cache[symbol]
        if self._history_provider is None:
            raise DataUnavailable(symbol, "fundamental history", "no history provider configured")
        rows = list(self._history_provider(symbol, self.history_years) or ())
        self._history_cache[symbol] = rows
        return rows

    # -- profile -----------------------------------------------------------
    def get_profile(self, symbol: str) -> CompanyProfile:
        f = self._fundamental(symbol)
        company = _attr(f, "company_reference")
        security = _attr(f, "security_reference")
        classification = _attr(f, "asset_classification")

        sic = _attr(classification, "sic")
        template = _attr(company, "industry_template_code")
        is_reit = bool(_attr(company, "is_reit"))

        sic_int: Optional[int] = None
        try:
            sic_int = int(sic) if sic not in (None, "") else None
        except (TypeError, ValueError):
            sic_int = None

        is_bank = template == TEMPLATE_BANK or (sic_int in SIC_BANK if sic_int else False)
        is_insurer = template == TEMPLATE_INSURANCE or (sic_int in SIC_INSURANCE if sic_int else False)
        is_reit = is_reit or template == TEMPLATE_REIT or (sic_int in SIC_REIT if sic_int else False)

        price = _num(_attr(f, "price")) or _num(_attr(f, "value"))
        market_cap = _num(_attr(f, "market_cap"))

        return CompanyProfile(
            symbol=symbol,
            name=_attr(company, "standard_name") or _attr(company, "short_name"),
            sector=_sector_name(_attr(classification, "morningstar_sector_code")),
            industry=str(_attr(classification, "morningstar_industry_group_code") or "") or None,
            exchange=str(_attr(security, "exchange_id") or "") or None,
            country=str(_attr(company, "country_id") or "") or None,
            currency=str(_attr(security, "currency_id") or "") or None,
            market_cap=market_cap,
            price=price,
            beta=None,  # not carried on Fundamental; Module B falls back to its floor
            avg_daily_dollar_volume=_num(_attr(f, "dollar_volume")),
            shares_outstanding=None,
            is_adr=bool(_attr(security, "is_depositary_receipt")),
            is_etf=False,
            is_fund=False,
            is_actively_trading=_attr(security, "delisting_date") is None,
            ipo_date=_to_date(_attr(security, "ipo_date")),
            sic_code=str(sic) if sic not in (None, "") else None,
            is_reit=is_reit,
            is_bank=is_bank,
            is_insurer=is_insurer,
            as_of=self.as_of,
        )

    # -- statements --------------------------------------------------------
    def _financials(self, f: Any, periods: Sequence[str], period_type: PeriodType) -> Optional[Financials]:
        statements = _attr(f, "financial_statements")
        income = _attr(statements, "income_statement")
        balance = _attr(statements, "balance_sheet")
        cash = _attr(statements, "cash_flow_statement")
        earnings = _attr(f, "earning_reports")
        if statements is None:
            return None

        period_end = _to_date(period_value_date(_attr(statements, "period_ending_date"), periods))
        if period_end is None:
            period_end = _to_date(_attr(f, "end_time"))
        if period_end is None:
            return None

        def inc(name: str) -> Optional[float]:
            return period_value(getattr(income, name, None), periods)

        def bal(name: str) -> Optional[float]:
            return period_value(getattr(balance, name, None), periods)

        def flow(name: str) -> Optional[float]:
            return period_value(getattr(cash, name, None), periods)

        def earn(name: str) -> Optional[float]:
            return period_value(getattr(earnings, name, None), periods)

        net_income = inc("net_income")
        depreciation = inc("depreciation_and_amortization") or flow("depreciation_amortization_depletion")

        return Financials(
            period_end=period_end,
            period_type=period_type,
            filed_date=_to_date(period_value_date(_attr(statements, "file_date"), periods)),
            revenue=inc("total_revenue") or inc("operating_revenue"),
            gross_profit=inc("gross_profit"),
            operating_income=inc("operating_income"),
            ebitda=inc("ebitda") or inc("normalized_ebitda"),
            net_income=net_income,
            eps_diluted=earn("diluted_eps"),
            interest_expense=inc("interest_expense"),
            tax_expense=inc("tax_provision"),
            pretax_income=inc("pretax_income"),
            depreciation_amortisation=depreciation,
            current_assets=bal("current_assets"),
            current_liabilities=bal("current_liabilities"),
            cash_and_equivalents=bal("cash_and_cash_equivalents"),
            short_term_investments=bal("available_for_sale_securities"),
            total_debt=bal("total_debt"),
            total_assets=bal("total_assets"),
            total_liabilities=bal("total_liabilities_net_minority_interest"),
            total_equity=bal("stockholders_equity"),
            goodwill_and_intangibles=bal("goodwill_and_other_intangible_assets"),
            shares_diluted=earn("diluted_average_shares"),
            operating_cash_flow=flow("operating_cash_flow"),
            capital_expenditure=flow("capital_expenditure"),
            free_cash_flow=flow("free_cash_flow"),
            dividends_paid=flow("cash_dividends_paid"),
            share_repurchase=flow("repurchase_of_capital_stock"),
            # REIT: FFO is derivable from these; AFFO is not, because
            # Morningstar carries no recurring-capex or straight-line-rent
            # line. Module B4 warns and falls back to P/FFO.
            real_estate_depreciation=depreciation,
            gains_on_property_sales=inc("gain_on_sale_of_ppe") or inc("gain_loss_on_sale_of_business"),
            recurring_capex=None,
            straight_line_rent_adjustment=None,
            # Bank.
            tangible_common_equity=bal("tangible_book_value"),
            net_interest_income=inc("net_interest_income"),
            # Insurer.
            earned_premium=inc("total_premiums_earned"),
            losses_and_lae_incurred=inc("policyholder_benefits_gross"),
            underwriting_expense=inc("underwriting_expenses"),
            net_investment_income=inc("net_investment_income"),
            unrealised_investment_gains=inc("unrealized_gain_loss"),
        )

    def _statement_series(self, symbol: str, periods, period_type, limit: int) -> Sequence[Financials]:
        rows = self._history(symbol)
        if not rows:
            raise DataUnavailable(symbol, "fundamental history", "history provider returned nothing")

        seen: dict[date, Financials] = {}
        for row in rows:
            parsed = self._financials(row, periods, period_type)
            if parsed is not None:
                seen[parsed.period_end] = parsed

        out = sorted(seen.values(), key=lambda f: f.period_end, reverse=True)[:limit]
        if not out:
            raise DataUnavailable(symbol, f"{period_type.value} statements")
        return out

    def get_annual_financials(self, symbol: str, years: int = 10) -> Sequence[Financials]:
        return self._statement_series(symbol, ANNUAL_PERIODS, PeriodType.ANNUAL, years)

    def get_quarterly_financials(self, symbol: str, quarters: int = 12) -> Sequence[Financials]:
        return self._statement_series(symbol, QUARTERLY_PERIODS, PeriodType.QUARTER, quarters)

    # -- filing flags ------------------------------------------------------
    def get_filing_flags(self, symbol: str) -> FilingFlags:
        """Auditor change is detectable. The rest is not, and stays unknown.

        Returning ``None`` for the three unavailable flags is deliberate: gate
        A4 converts unknown into DATA_GAP, which is the correct reading. A
        clean bill of health would have to be earned from filing text this
        source does not carry.
        """
        history = []
        try:
            history = list(self._history(symbol))
        except DataUnavailable:
            history = []

        auditors: list[str] = []
        for row in reversed(history):  # newest first
            auditor = _attr(row, "financial_statements", "period_auditor")
            auditor = period_value_text(auditor, ANNUAL_PERIODS) or _attr(row, "company_reference", "auditor")
            if auditor:
                auditors.append(str(auditor))

        changed: Optional[bool] = None
        if len(auditors) >= 2:
            recent = auditors[:4]  # roughly the last twelve months of snapshots
            changed = len(set(recent)) > 1

        return FilingFlags(
            restatement_within_lookback=None,
            auditor_change_within_lookback=changed,
            auditor_change_reason=None,
            going_concern_language=None,
            delayed_filing=None,
            as_of=self.as_of,
        )

    # -- prices and multiples ---------------------------------------------
    def get_price_history(self, symbol: str, years: int = 10) -> Sequence[PricePoint]:
        rows = self._history(symbol)
        points = []
        for row in rows:
            observed = _to_date(_attr(row, "end_time"))
            close = _num(_attr(row, "price")) or _num(_attr(row, "value"))
            if observed and close:
                points.append(PricePoint(observed, close))
        if not points:
            raise DataUnavailable(symbol, "price history")
        return sorted(points, key=lambda p: p.observed_on)

    MULTIPLE_FIELDS = {
        "trailing P/E": "pe_ratio",
        "forward P/E": "forward_pe_ratio",
        "P/B": "pb_ratio",
        "EV/Revenue": "ev_to_revenue",
        # No P/AFFO on this source. P/FFO is approximated by price to operating
        # cash flow, which is the closest honest stand-in; B4 already reports
        # which basis answered.
        "P/AFFO": "pcf_ratio",
    }

    def get_multiple_series(self, symbol: str, multiple_name: str, years: int = 10) -> MultipleSeries:
        field = self.MULTIPLE_FIELDS.get(multiple_name)
        if field is None:
            raise DataUnavailable(symbol, f"{multiple_name} history", "multiple not mapped for QuantConnect")

        points = []
        for row in self._history(symbol):
            observed = _to_date(_attr(row, "end_time"))
            value = _num(_attr(row, "valuation_ratios", field))
            if observed and value is not None and value > 0:
                points.append(MultiplePoint(observed, value))
        if not points:
            raise DataUnavailable(symbol, f"{multiple_name} history", "no positive observations")
        return MultipleSeries(symbol, multiple_name, tuple(sorted(points, key=lambda p: p.observed_on)))

    # -- peers and sector --------------------------------------------------
    def get_peers(self, symbol: str) -> Sequence[str]:
        """Peers from the same Morningstar industry group, within the universe.

        This is a candidate pool, not a peer set. Module C2 applies the size
        and growth bands and logs every rejection -- the vendor list is never
        taken as the answer.
        """
        f = self._fundamental(symbol)
        group = _attr(f, "asset_classification", "morningstar_industry_group_code")
        if group is None:
            raise DataUnavailable(symbol, "peer list", "no industry group code")
        peers = [
            other
            for other, of in self._fundamentals.items()
            if other != symbol and _attr(of, "asset_classification", "morningstar_industry_group_code") == group
        ]
        if not peers:
            raise DataUnavailable(symbol, "peer list", "no same-industry names in the universe")
        return peers

    def get_sector_net_debt_ebitda(self, sector: str) -> Sequence[float]:
        out: list[float] = []
        for symbol, f in self._fundamentals.items():
            if _sector_name(_attr(f, "asset_classification", "morningstar_sector_code")) != sector:
                continue
            statements = _attr(f, "financial_statements")
            balance = _attr(statements, "balance_sheet")
            income = _attr(statements, "income_statement")
            debt = period_value(getattr(balance, "total_debt", None), ANNUAL_PERIODS)
            cash = period_value(getattr(balance, "cash_and_cash_equivalents", None), ANNUAL_PERIODS)
            ebitda = period_value(getattr(income, "ebitda", None), ANNUAL_PERIODS)
            if debt is None or cash is None or not ebitda or ebitda <= 0:
                continue
            out.append((debt - cash) / ebitda)
        if not out:
            raise DataUnavailable(sector, "sector leverage aggregates", "no computable sector members")
        return out

    # -- estimates ---------------------------------------------------------
    def get_estimates(self, symbol: str) -> Estimates:
        """Forward P/E and dividend per share are served; earnings dates are not.

        ``next_earnings_date`` stays None, so Module E's ten-day pre-earnings
        blackout never fires on this source. That is a real reduction in entry
        hygiene and is recorded in the coverage report rather than papered over.
        """
        f = self._fundamental(symbol)
        ratios = _attr(f, "valuation_ratios")
        price = _num(_attr(f, "price")) or _num(_attr(f, "value"))
        forward_pe = _num(_attr(ratios, "forward_pe_ratio"))
        trailing_pe = _num(_attr(ratios, "pe_ratio"))

        forward_eps = price / forward_pe if price and forward_pe and forward_pe > 0 else None
        growth = None
        if forward_pe and trailing_pe and forward_pe > 0:
            growth = 100.0 * (trailing_pe / forward_pe - 1.0)

        dps = period_value(_attr(f, "earning_reports", "dividend_per_share"), ANNUAL_PERIODS)
        dividend_yield = 100.0 * dps / price if dps and price else None

        return Estimates(
            forward_eps=forward_eps,
            forward_eps_growth_pct=growth,
            forward_revenue=None,
            dividend_yield_pct=dividend_yield,
            next_earnings_date=None,
            as_of=self.as_of,
        )


def period_value_date(field: Any, periods: Sequence[str]) -> Any:
    """``get_period_value`` for a date-typed MultiPeriodField."""
    if field is None:
        return None
    for period in periods:
        try:
            if field.has_period_value(period):
                return field.get_period_value(period)
        except (AttributeError, TypeError, KeyError):
            break
    return getattr(field, "value", None)


def period_value_text(field: Any, periods: Sequence[str]) -> Optional[str]:
    value = period_value_date(field, periods)
    return str(value) if value not in (None, "") else None


# Morningstar sector codes. Mapped to names so Module A2's sector-relative
# leverage test and Module C2's sector match read the same way they do on
# every other adapter.
MORNINGSTAR_SECTORS = {
    101: "Basic Materials",
    102: "Consumer Cyclical",
    103: "Financial Services",
    104: "Real Estate",
    205: "Consumer Defensive",
    206: "Healthcare",
    207: "Utilities",
    308: "Communication Services",
    309: "Energy",
    310: "Industrials",
    311: "Technology",
}


def _sector_name(code: Any) -> Optional[str]:
    try:
        return MORNINGSTAR_SECTORS.get(int(code))
    except (TypeError, ValueError):
        return None
