"""Module J — tax and jurisdiction.

Operator-configurable; the default is a Singapore-resident individual, which
has two consequences the system should act on rather than merely note:

* Capital gains tax is 0%, so trimming and rebalancing carry no tax cost.
  Unlike a US resident there is no lock-in penalty for selling a winner, and
  Module G's TRIM-TO-CAP therefore carries no offsetting cost — the system
  never hesitates to recommend a trim on tax grounds.
* US dividend withholding is 30% and non-recoverable, so a 4% US dividend is
  2.8% in hand.  Reporting the headline yield to this operator is reporting a
  number they will never receive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..classification import Classification
from ..config import Config


@dataclass(frozen=True)
class YieldAssessment:
    headline_yield: float | None
    after_withholding_yield: float | None
    withholding_rate: float
    exempt: bool
    exempt_reason: str | None
    thesis_rests_on_yield: bool

    def as_report_line(self) -> str:
        if self.headline_yield is None:
            return "yield: n/a (not a dividend payer)"
        line = (
            f"yield: headline {self.headline_yield:.2%} -> "
            f"after-withholding {self.after_withholding_yield:.2%} "
            f"(withholding {self.withholding_rate:.0%})"
        )
        if self.exempt:
            line += f" · EXEMPT — {self.exempt_reason}"
        if self.thesis_rests_on_yield:
            line += (
                " · FLAG: thesis rests primarily on dividend yield; "
                "appreciation-oriented holdings are more tax-efficient for this "
                "operator profile"
            )
        return line


def assess_yield(
    headline_yield: float | None,
    exchange: str | None,
    classification: Classification,
    config: Config,
) -> YieldAssessment:
    """After-withholding yield, never headline, for any dividend payer."""
    cfg = config.tax
    if headline_yield is None or headline_yield <= 0:
        return YieldAssessment(None, None, 0.0, False, None, False)

    exempt = False
    reason = None
    rate = 0.0

    is_sgx = (exchange or "").upper() in ("SGX", "SES", "SGX-ST")
    if is_sgx and classification is Classification.REIT and cfg.sgx_reit_distribution_exempt:
        exempt = True
        reason = (
            "SGX-listed REIT distributions are tax-exempt to Singapore-resident "
            "individuals — a genuine structural yield advantage over US equivalents"
        )
    elif (exchange or "").upper() in ("NYSE", "NASDAQ", "AMEX", "NYSEARCA", "BATS"):
        rate = cfg.us_dividend_withholding

    after = headline_yield * (1.0 - rate)
    return YieldAssessment(
        headline_yield=headline_yield,
        after_withholding_yield=after,
        withholding_rate=rate,
        exempt=exempt,
        exempt_reason=reason,
        thesis_rests_on_yield=headline_yield >= cfg.dividend_thesis_flag_yield,
    )


def trim_carries_tax_cost(config: Config) -> bool:
    """Whether a trim recommendation should be tempered by a tax consequence."""
    return config.tax.capital_gains_rate > 0.0


def trim_note(config: Config) -> str:
    if trim_carries_tax_cost(config):
        return (
            f"TRIM-TO-CAP carries a {config.tax.capital_gains_rate:.0%} capital "
            "gains cost in this jurisdiction; weigh it against the risk being "
            "trimmed."
        )
    return (
        "Capital gains tax is 0% in this jurisdiction. TRIM-TO-CAP carries no "
        "offsetting cost — there is no tax reason to hold an oversized position."
    )


__all__ = ["YieldAssessment", "assess_yield", "trim_carries_tax_cost", "trim_note"]
