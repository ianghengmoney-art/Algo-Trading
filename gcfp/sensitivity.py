"""How much of a fair value is analysis, and how much is assumption?

§17 is blunt about this: *"Its fair values are conditional on assumptions, not
discovered facts."*  B1's half-growth re-run and B2's three scenarios expose
that sensitivity — but exposing it per name, in prose, is not the same as
being able to say **which** valuations are load-bearing and which are a
rounding error away from meaningless.

So every valuation gets re-run across a small grid of plausible inputs, and a
name whose fair value swings wildly on a change no analyst could rule out gets
flagged `FRAGILE`.  A 27% discount that becomes a 3% premium when the discount
rate moves one point is not a 27% discount; it is a coin flip with a decimal
point.

This changes no verdict.  It labels the ones you should not trust.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .classification import Classification
from .config import Config
from .data.adapter import DataUnavailable
from .modules.b_valuation import FairValue, MethodRefused, value
from .types import CompanyData, MarketData

#: A fair value moving more than this on a one-point discount-rate change is
#: not a valuation, it is a lever.
FRAGILE_THRESHOLD = 0.25

#: Perturbations applied one at a time.  Each is a change a competent analyst
#: could defend, which is the point: if defensible inputs produce
#: irreconcilable answers, the output is not evidence.
DISCOUNT_RATE_SHIFTS: tuple[float, ...] = (-0.01, 0.01)
ERP_SHIFTS: tuple[float, ...] = (-0.005, 0.005)
TERMINAL_GROWTH_SHIFTS: tuple[float, ...] = (-0.005, 0.005)
FADE_YEAR_SHIFTS: tuple[int, ...] = (-2, 2)


@dataclass(frozen=True)
class Perturbation:
    """One input moved, and what the fair value became."""

    label: str
    fair_value: float | None
    #: Fractional change from the base fair value.
    change: float | None
    note: str = ""

    def as_report_line(self) -> str:
        if self.fair_value is None:
            return f"    {self.label:<28} not computable — {self.note}"
        return (
            f"    {self.label:<28} {self.fair_value:>10,.2f}  "
            f"({self.change:+.1%})"
        )


@dataclass
class SensitivityReport:
    """One name's valuation, stress-tested."""

    symbol: str
    classification: Classification
    base_fair_value: float
    price: float | None
    perturbations: list[Perturbation] = field(default_factory=list)
    terminal_value_share: float | None = None
    fragile_threshold: float = FRAGILE_THRESHOLD

    @property
    def worst_swing(self) -> float:
        """Largest fractional move any single perturbation produced."""
        changes = [abs(p.change) for p in self.perturbations if p.change is not None]
        return max(changes) if changes else 0.0

    @property
    def discount_rate_swing(self) -> float:
        """The one that matters most: a 1pp change in the discount rate."""
        changes = [
            abs(p.change)
            for p in self.perturbations
            if p.change is not None and p.label.startswith("discount rate")
        ]
        return max(changes) if changes else 0.0

    @property
    def is_fragile(self) -> bool:
        return self.discount_rate_swing > self.fragile_threshold

    @property
    def verdict_survives(self) -> bool | None:
        """Whether every perturbation keeps the price below fair value.

        This is the question that actually matters. A fair value that moves a
        lot but stays comfortably above the price still supports the same
        decision; one that crosses the price does not.
        """
        if self.price is None:
            return None
        values = [self.base_fair_value] + [
            p.fair_value for p in self.perturbations if p.fair_value is not None
        ]
        return all(v > self.price for v in values)

    def as_report_lines(self) -> list[str]:
        lines = [
            f"  SENSITIVITY · {self.symbol} ({self.classification.value})",
            f"    base fair value{'':<13} {self.base_fair_value:>10,.2f}",
        ]
        if self.terminal_value_share is not None:
            lines.append(
                f"    terminal value share{'':<8} {self.terminal_value_share:>10.1%}"
                + ("  <- most of this number is a perpetuity assumption"
                   if self.terminal_value_share > 0.60 else "")
            )
        lines.extend(p.as_report_line() for p in self.perturbations)

        if self.is_fragile:
            lines.append(
                f"    ** FRAGILE — a 1pp discount-rate change moves fair value "
                f"{self.discount_rate_swing:.0%}. Treat this valuation as a "
                "range, not a number. **"
            )
        survives = self.verdict_survives
        if survives is False:
            lines.append(
                "    ** VERDICT DOES NOT SURVIVE — at least one defensible "
                "input choice puts the price ABOVE fair value. The buy case "
                "rests on the assumptions, not on the business. **"
            )
        elif survives is True:
            lines.append(
                "    verdict survives every perturbation tested"
            )
        return lines


def _revalue(
    data: CompanyData,
    market: MarketData,
    config: Config,
    classification: Classification,
    **kwargs,
) -> FairValue | None:
    try:
        return value(data, market, config, classification, None, **kwargs)
    except (MethodRefused, DataUnavailable, Exception):
        return None


def analyse(
    data: CompanyData,
    market: MarketData,
    config: Config,
    classification: Classification,
    base: FairValue,
    *,
    price: float | None = None,
    valuation_kwargs: dict | None = None,
) -> SensitivityReport:
    """Re-run one valuation across the perturbation grid."""
    valuation_kwargs = valuation_kwargs or {}
    report = SensitivityReport(
        symbol=data.profile.symbol,
        classification=classification,
        base_fair_value=base.fair_value_per_share,
        price=price,
        terminal_value_share=base.terminal_value_share,
    )

    def add(label: str, config_variant: Config, market_variant: MarketData | None = None) -> None:
        result = _revalue(
            data, market_variant or market, config_variant, classification,
            **valuation_kwargs,
        )
        if result is None:
            report.perturbations.append(
                Perturbation(label, None, None, "method refused or data missing")
            )
            return
        change = (
            result.fair_value_per_share / base.fair_value_per_share - 1.0
            if base.fair_value_per_share
            else None
        )
        report.perturbations.append(
            Perturbation(label, result.fair_value_per_share, change)
        )

    # Discount rate, via the absolute floor — the cleanest single lever, since
    # the floor binds for most CORE names anyway.
    for shift in DISCOUNT_RATE_SHIFTS:
        variant = dataclasses.replace(
            config,
            discount_rate=dataclasses.replace(
                config.discount_rate,
                absolute_floor=max(config.discount_rate.absolute_floor + shift, 0.01),
                core_growth_floor=max(config.discount_rate.core_growth_floor + shift, 0.01),
                spec_growth_floor=max(config.discount_rate.spec_growth_floor + shift, 0.01),
            ),
        )
        add(f"discount rate {shift:+.0%}", variant)

    # Equity risk premium, within the sweep range the spec already names.
    for shift in ERP_SHIFTS:
        variant = dataclasses.replace(
            config,
            discount_rate=dataclasses.replace(
                config.discount_rate,
                equity_risk_premium=config.discount_rate.equity_risk_premium + shift,
            ),
        )
        add(f"equity risk premium {shift:+.1%}", variant)

    # Terminal growth, which the spec caps at 2.5% and never above.
    for shift in TERMINAL_GROWTH_SHIFTS:
        capped = min(
            max(config.valuation.terminal_growth_cap + shift, 0.0), 0.025
        )
        if capped == config.valuation.terminal_growth_cap:
            continue
        variant = dataclasses.replace(
            config,
            valuation=dataclasses.replace(
                config.valuation, terminal_growth_cap=capped
            ),
        )
        add(f"terminal growth {shift:+.1%}", variant)

    # Fade length: how long above-terminal growth is assumed to persist.
    for shift in FADE_YEAR_SHIFTS:
        years = config.valuation.b1_projection_years + shift
        stage_one = min(config.valuation.b1_stage_one_years, max(years - 1, 1))
        if years < 3:
            continue
        variant = dataclasses.replace(
            config,
            valuation=dataclasses.replace(
                config.valuation,
                b1_projection_years=years,
                b1_stage_one_years=stage_one,
            ),
        )
        add(f"projection {shift:+d} years", variant)

    return report


@dataclass
class SensitivitySummary:
    """Fragility across a whole screen."""

    reports: list[SensitivityReport] = field(default_factory=list)

    def record(self, report: SensitivityReport) -> None:
        self.reports.append(report)

    @property
    def fragile(self) -> list[SensitivityReport]:
        return [r for r in self.reports if r.is_fragile]

    @property
    def verdict_at_risk(self) -> list[SensitivityReport]:
        return [r for r in self.reports if r.verdict_survives is False]

    def as_report_lines(self) -> list[str]:
        lines = ["VALUATION SENSITIVITY"]
        if not self.reports:
            lines.append("  no valuations analysed")
            return lines

        lines.append(f"  valuations analysed: {len(self.reports)}")
        lines.append(
            f"  FRAGILE (>{FRAGILE_THRESHOLD:.0%} swing on 1pp discount rate): "
            f"{len(self.fragile)}"
        )
        lines.append(
            f"  verdict does not survive perturbation: {len(self.verdict_at_risk)}"
        )
        high_terminal = [
            r for r in self.reports
            if r.terminal_value_share is not None and r.terminal_value_share > 0.60
        ]
        if high_terminal:
            lines.append(
                f"  more than 60% of value in the terminal assumption: "
                f"{len(high_terminal)}"
            )
        if self.verdict_at_risk:
            lines.append("")
            lines.append(
                "  Names whose buy case rests on the assumptions rather than "
                "the business:"
            )
            for report in self.verdict_at_risk:
                lines.append(f"    {report.symbol}")
        return lines


__all__ = [
    "FRAGILE_THRESHOLD",
    "Perturbation",
    "SensitivityReport",
    "SensitivitySummary",
    "analyse",
]
