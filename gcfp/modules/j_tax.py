"""Module J -- tax and jurisdiction layer.

Operator-configurable. Default profile: Singapore-resident individual.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config import Params


@dataclass
class TaxTreatment:
    symbol: str
    headline_yield_pct: Optional[float]
    after_withholding_yield_pct: Optional[float]
    withholding_pct: float
    notes: list[str] = field(default_factory=list)
    dividend_thesis_flagged: bool = False


def is_us_listed(exchange: Optional[str], country: Optional[str]) -> bool:
    if exchange and exchange.upper() in {"NYSE", "NASDAQ", "AMEX", "NYSEARCA", "BATS"}:
        return True
    return bool(country and country.upper() in {"US", "USA"})


def is_sgx_reit(exchange: Optional[str], classification: Optional[str]) -> bool:
    return bool(exchange and exchange.upper() == "SGX" and classification == "REIT")


def assess(
    symbol: str,
    exchange: Optional[str],
    country: Optional[str],
    headline_yield_pct: Optional[float],
    params: Params,
    classification: Optional[str] = None,
    thesis_rests_on_yield: bool = False,
) -> TaxTreatment:
    """Compute after-withholding yield and surface the structural implications.

    A 4% US dividend is 2.8% in hand. Reporting the headline number to a
    Singapore-resident investor overstates the income case by 30% on every
    US-listed payer.
    """
    tp = params.tax
    notes: list[str] = []
    withholding = 0.0
    after = headline_yield_pct

    if is_sgx_reit(exchange, classification):
        withholding = tp.local_reit_distribution_tax_pct
        notes.append(
            "SGX-listed REIT distributions are tax-exempt to a Singapore-resident individual -- "
            "a genuine structural yield advantage over the US equivalent."
        )
    elif is_us_listed(exchange, country) and headline_yield_pct:
        withholding = tp.us_dividend_withholding_pct
        after = headline_yield_pct * (1.0 - withholding / 100.0)
        notes.append(
            f"US dividend withholding {withholding:.0f}% is non-recoverable for this operator "
            f"profile: headline {headline_yield_pct:.2f}% is {after:.2f}% in hand."
        )

    flagged = False
    if (
        headline_yield_pct is not None
        and headline_yield_pct >= tp.dividend_thesis_flag_yield_pct
        and is_us_listed(exchange, country)
    ) or thesis_rests_on_yield:
        flagged = True
        notes.append(
            "DIVIDEND-LED THESIS FLAG -- with 0% capital gains tax and 30% dividend "
            "withholding, appreciation-oriented holdings are more tax-efficient than "
            "dividend-oriented ones for this operator profile."
        )

    return TaxTreatment(
        symbol=symbol,
        headline_yield_pct=headline_yield_pct,
        after_withholding_yield_pct=after,
        withholding_pct=withholding,
        notes=notes,
        dividend_thesis_flagged=flagged,
    )


def trim_tax_cost_note(params: Params) -> str:
    """Trimming carries no tax cost under the default profile.

    Stated explicitly so that Module G's TRIM-TO-CAP flag is never quietly
    discounted on tax grounds that do not apply here.
    """
    if params.tax.capital_gains_tax_pct == 0.0:
        return (
            "Capital gains tax 0% -- trimming, rebalancing and switching carry no tax cost. "
            "There is no lock-in penalty for selling a winner and TRIM-TO-CAP carries no "
            "offsetting cost."
        )
    return (
        f"Capital gains tax {params.tax.capital_gains_tax_pct:.1f}% -- weigh realised gains "
        "against the trim."
    )
