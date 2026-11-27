"""Module K — currency layer (new; fixes v3 flaw 9).

v3 explicitly required testing a foreign ADR in its §17 while providing no FX
handling anywhere.  For a Singapore-based operator holding US-listed securities
— every candidate in this universe — currency is a live exposure, not a
footnote.

K6 is the honest boundary: this module measures and reports FX exposure.  It
does not hedge, forecast, or gate on it.  A 10% adverse currency move is a real
10% loss in base-currency terms with no relationship to business quality, and
nothing in this system prevents it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Sequence

from ..config import Config
from ..types import CompanyProfile


class FxRateUnavailable(Exception):
    """No rate for a required currency pair.

    Raised rather than defaulting to 1.0: a silently unconverted foreign
    position misstates every portfolio-level cap in Module F.
    """


@dataclass(frozen=True)
class FxQuote:
    pair: str
    rate: float
    as_of: date

    def as_log_line(self) -> str:
        return f"{self.pair}={self.rate:.6g} @ {self.as_of.isoformat()}"


@dataclass
class FxTable:
    """Spot rates with their timestamps.  K3 requires both to be reported."""

    rates: dict[str, float] = field(default_factory=dict)
    as_of: date | None = None

    def quote(self, frm: str, to: str) -> FxQuote:
        if frm == to:
            return FxQuote(f"{frm}{to}", 1.0, self.as_of or date.today())
        direct = f"{frm}{to}"
        if direct in self.rates:
            return FxQuote(direct, self.rates[direct], self.as_of or date.today())
        inverse = f"{to}{frm}"
        if inverse in self.rates and self.rates[inverse]:
            return FxQuote(
                direct, 1.0 / self.rates[inverse], self.as_of or date.today()
            )
        raise FxRateUnavailable(
            f"no rate for {frm}->{to}; the position cannot be expressed in base "
            "currency and must not be silently treated as unconverted"
        )

    def convert(self, amount: float, frm: str, to: str) -> tuple[float, FxQuote]:
        q = self.quote(frm, to)
        return amount * q.rate, q


@dataclass(frozen=True)
class DualReport:
    """K3 — every position reports both currencies and the rate used."""

    listing_currency: str
    base_currency: str
    price_listing: float
    fair_value_listing: float
    price_base: float
    fair_value_base: float
    fx: FxQuote

    def as_report_lines(self) -> list[str]:
        return [
            f"  price: {self.price_listing:,.2f} {self.listing_currency} "
            f"= {self.price_base:,.2f} {self.base_currency}",
            f"  fair value: {self.fair_value_listing:,.2f} {self.listing_currency} "
            f"= {self.fair_value_base:,.2f} {self.base_currency}",
            f"  FX: {self.fx.as_log_line()}",
        ]


def dual_report(
    profile: CompanyProfile,
    price_listing: float,
    fair_value_listing: float,
    table: FxTable,
    config: Config,
) -> DualReport:
    """K2/K3.

    Fair value is computed in the security's own reporting currency and only
    then translated at spot.  Inputs are never translated before valuing:
    mixing currencies inside a DCF corrupts the growth and margin series.
    """
    base = config.currency.base_currency
    price_base, quote = table.convert(price_listing, profile.listing_currency, base)
    fv_base, _ = table.convert(fair_value_listing, profile.listing_currency, base)
    return DualReport(
        listing_currency=profile.listing_currency,
        base_currency=base,
        price_listing=price_listing,
        fair_value_listing=fair_value_listing,
        price_base=price_base,
        fair_value_base=fv_base,
        fx=quote,
    )


@dataclass(frozen=True)
class AdrExposure:
    """K5 — the trading currency is not the economic exposure."""

    symbol: str
    trading_currency: str
    underlying_currency: str | None

    def as_report_line(self) -> str:
        if self.underlying_currency is None:
            return (
                f"  ADR · TRADING CURRENCY: {self.trading_currency} · "
                "UNDERLYING EXPOSURE: UNMAPPED — the true economic exposure is "
                "not the trading currency, and this source does not supply it"
            )
        return (
            f"  ADR · TRADING CURRENCY: {self.trading_currency} · "
            f"UNDERLYING EXPOSURE: {self.underlying_currency}"
        )


def adr_exposure(profile: CompanyProfile) -> AdrExposure | None:
    """For ADRs, the underlying currency is the true economic exposure.

    An operator who believes they hold three currencies may actually hold five.
    """
    if not profile.is_adr:
        return None
    return AdrExposure(
        symbol=profile.symbol,
        trading_currency=profile.listing_currency,
        underlying_currency=profile.underlying_currency,
    )


@dataclass
class ExposureReport:
    """K4 — portfolio exposure by currency, as a share of total."""

    base_currency: str
    by_trading_currency: dict[str, float]
    by_underlying_currency: dict[str, float]
    total_value: float

    def as_report_lines(self) -> list[str]:
        lines = [f"FX EXPOSURE (base {self.base_currency}):"]
        for label, table in (
            ("by trading currency", self.by_trading_currency),
            ("by underlying exposure", self.by_underlying_currency),
        ):
            lines.append(f"  {label}:")
            for ccy, share in sorted(table.items(), key=lambda kv: -kv[1]):
                lines.append(f"    {ccy}: {share:.1%}")
        concentrated = [
            f"{c} {s:.0%}" for c, s in self.by_underlying_currency.items() if s >= 0.80
        ]
        if concentrated:
            lines.append(
                f"  CONCENTRATION: {', '.join(concentrated)} — a real exposure "
                "that no equity-level diversification addresses."
            )
        lines.append(
            "  K6: this module measures and reports FX exposure. It does not "
            "hedge, forecast, or gate on it. A 10% adverse move is a real 10% "
            "base-currency loss unrelated to business quality."
        )
        return lines


def exposure_report(
    positions: Sequence[tuple[CompanyProfile, float]],
    config: Config,
) -> ExposureReport:
    """Build K4's report from (profile, base-currency value) pairs."""
    total = sum(v for _, v in positions)
    trading: dict[str, float] = {}
    underlying: dict[str, float] = {}
    for profile, value in positions:
        trading[profile.listing_currency] = (
            trading.get(profile.listing_currency, 0.0) + value
        )
        # For an ADR the economic exposure is the underlying currency; where it
        # is unmapped, say so rather than crediting it to the trading currency.
        ccy = (
            profile.underlying_currency
            if profile.is_adr and profile.underlying_currency
            else "UNMAPPED"
            if profile.is_adr
            else profile.listing_currency
        )
        underlying[ccy] = underlying.get(ccy, 0.0) + value

    if total <= 0:
        return ExposureReport(config.currency.base_currency, {}, {}, 0.0)
    return ExposureReport(
        base_currency=config.currency.base_currency,
        by_trading_currency={k: v / total for k, v in trading.items()},
        by_underlying_currency={k: v / total for k, v in underlying.items()},
        total_value=total,
    )


__all__ = [
    "FxTable",
    "FxQuote",
    "FxRateUnavailable",
    "DualReport",
    "AdrExposure",
    "ExposureReport",
    "dual_report",
    "adr_exposure",
    "exposure_report",
]
