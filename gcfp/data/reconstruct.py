"""Rebuilding C1's multiple history from prices and per-share fundamentals.

No free source publishes a multiple series, so C1's history has to be derived.
The derivation lives here, alone, because it is used by every adapter that
pairs fundamentals with prices and because it is the one place a lookahead bug
would be both easy to introduce and invisible in the output.

The rule that prevents it: **an observation is dated by when the figures were
filed, not by when the period ended.** A December quarter is not knowable until
it is filed in February, so the trailing multiple on 15 January uses the
September quarter. Matching on period end instead would hand the model months
of information it could not have had, and would improve every backtest.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Sequence

from ..types import MultipleObservation, PeriodFinancials, PricePoint

#: How far past a filing date to look for a usable close.  Filings land on
#: weekends and holidays often enough to matter; a wider gap means the price
#: series has a hole and the observation is dropped rather than stretched.
PRICE_SEARCH_DAYS = 7


def multiple_from_filings(
    quarters: Sequence[PeriodFinancials],
    multiple: str,
    observed_on: date,
    price: float,
) -> float | None:
    """The multiple computable from what had been filed by ``observed_on``.

    Only quarters already filed count toward the trailing figures.  Returns
    ``None`` rather than a substitute whenever a leg is missing — and returns
    ``None`` for a P/E on negative earnings, which is meaningless rather than
    large (Prime Directive 3).
    """
    known = [q for q in quarters if (q.filing_date or q.period_end) <= observed_on]
    known.sort(key=lambda q: q.period_end, reverse=True)
    if len(known) < 4:
        return None

    ttm = known[:4]
    latest = known[0]
    shares = latest.shares_diluted or latest.shares_outstanding
    if not shares or shares <= 0:
        return None

    def total(attr: str) -> float | None:
        values = [getattr(q, attr) for q in ttm]
        return sum(values) if all(v is not None for v in values) else None

    if multiple in ("trailing_pe", "forward_pe"):
        earnings = total("net_income")
        if earnings is None or earnings <= 0:
            return None
        return price / (earnings / shares)

    if multiple == "ev_revenue":
        revenue = total("revenue")
        if revenue is None or revenue <= 0:
            return None
        net_debt = latest.net_debt
        if net_debt is None:
            return None
        return (price * shares + net_debt) / revenue

    if multiple in ("p_b", "p_tbv"):
        book = latest.tangible_book_value if multiple == "p_tbv" else latest.total_equity
        if book is None or book <= 0:
            return None
        return price / (book / shares)

    if multiple == "p_affo":
        affo = latest.adjusted_funds_from_operations or latest.funds_from_operations
        if affo is None or affo <= 0:
            return None
        return price / (affo / shares)

    if multiple == "p_fcf":
        fcf = total("free_cash_flow")
        if fcf is None or fcf <= 0:
            return None
        return price / (fcf / shares)

    return None


def price_on_or_after(
    ordered_dates: Sequence[date],
    by_date: dict[date, PricePoint],
    target: date,
    *,
    max_gap_days: int = PRICE_SEARCH_DAYS,
) -> float | None:
    for day in ordered_dates:
        if day >= target:
            if (day - target).days > max_gap_days:
                return None
            return by_date[day].close
    return None


def reconstruct_series(
    quarters: Sequence[PeriodFinancials],
    prices: Sequence[PricePoint],
    multiple: str,
    *,
    end: date,
    years: int,
) -> list[MultipleObservation]:
    """One observation per quarter, dated by that quarter's filing date."""
    if not quarters or not prices:
        return []

    by_date = {p.price_date: p for p in prices}
    ordered = sorted(by_date)
    cutoff = end - timedelta(days=int(365.25 * years))

    observations: list[MultipleObservation] = []
    for quarter in quarters:
        observed_on = quarter.filing_date or quarter.period_end
        if observed_on > end or observed_on < cutoff:
            continue
        price = price_on_or_after(ordered, by_date, observed_on)
        if price is None:
            continue
        value = multiple_from_filings(quarters, multiple, observed_on, price)
        if value is None:
            continue
        observations.append(
            MultipleObservation(
                observation_date=observed_on, value=value, multiple=multiple
            )
        )

    return sorted(observations, key=lambda o: o.observation_date, reverse=True)


__all__ = [
    "PRICE_SEARCH_DAYS",
    "multiple_from_filings",
    "price_on_or_after",
    "reconstruct_series",
]
