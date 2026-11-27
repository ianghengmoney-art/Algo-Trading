"""Module E — buy / sell triggers.

Prime Directive 5: price is never a buy or sell reason on its own.  Technicals
are entry-timing hygiene only.  The one place price enters this module is the
earnings blackout, which delays an *alert* without changing a verdict — the
name stays on the pass list either way.

Prime Directive 1: alert-only.  Nothing here executes.  ``Signal`` is a
message, and the only thing the system does with it is write it to a report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum

from ..classification import Classification
from ..config import Config
from ..ledger import AuditLedger
from ..modules.b_valuation import FairValue
from ..modules.c_anchors import TriangulationResult
from ..modules.d_conviction import ConvictionScore


class SignalType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    REVIEW = "REVIEW"
    HOLD = "HOLD"
    NO_ACTION = "NO ACTION"

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.value


@dataclass
class Signal:
    """An alert.  Never an order.

    There is no ``execute`` method and no broker field, by design: the system
    must be incapable of placing an order, not merely configured not to.
    """

    symbol: str
    signal: SignalType
    classification: Classification
    reasons: list[str] = field(default_factory=list)
    #: Each of the three BUY conditions, so a near-miss is legible.
    conditions: dict[str, bool] = field(default_factory=dict)
    values: dict[str, float] = field(default_factory=dict)
    suppressed_until: date | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.signal in (SignalType.BUY, SignalType.SELL)

    def as_report_lines(self) -> list[str]:
        lines = [f"{self.signal.value} · {self.symbol} ({self.classification.value})"]
        for name, met in self.conditions.items():
            mark = "PASS" if met else "FAIL"
            lines.append(f"  [{mark}] {name}")
        lines.extend(f"  {r}" for r in self.reasons)
        if self.suppressed_until:
            lines.append(
                f"  ALERT SUPPRESSED until {self.suppressed_until.isoformat()} "
                "(earnings blackout) — the name remains on the pass list"
            )
        lines.extend(f"  {f}" for f in self.flags)
        return lines


def _trading_days_until(target: date, start: date) -> int:
    """Approximate trading days between two dates (weekends excluded).

    Exchange holidays are not modelled; the blackout is deliberately a rough
    hygiene rule, and erring a day long costs nothing.
    """
    if target <= start:
        return 0
    days = 0
    cursor = start
    while cursor < target:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def in_earnings_blackout(
    next_earnings: date | None, config: Config, as_of: date | None = None
) -> bool:
    """Within 10 trading days before a scheduled earnings date."""
    if next_earnings is None:
        return False
    as_of = as_of or date.today()
    remaining = _trading_days_until(next_earnings, as_of)
    return 0 < remaining <= config.triggers.earnings_blackout_trading_days


def evaluate_buy(
    symbol: str,
    classification: Classification,
    price: float,
    fair_value: FairValue,
    triangulation: TriangulationResult,
    conviction: ConvictionScore,
    config: Config,
    next_earnings: date | None = None,
    as_of: date | None = None,
    ledger: AuditLedger | None = None,
) -> Signal:
    """BUY requires all three conditions.

    1. Price >= X% below Module B fair value (X per classification, +10pp in
       SINGLE-ANCHOR MODE)
    2. Both computable anchors confirm undervaluation (or the single available
       anchor, under C5's penalties)
    3. Conviction score >= 60
    """
    as_of = as_of or date.today()
    single = triangulation.mode.is_single
    threshold = config.buy_threshold(classification, single)

    discount = fair_value.discount_to(price)
    cond_discount = discount >= threshold
    cond_anchors = triangulation.both_confirm_undervaluation
    cond_conviction = conviction.total >= config.triggers.min_conviction_to_buy

    conditions = {
        f"discount {discount:.1%} >= gate {threshold:.0%}"
        + (" (SINGLE-ANCHOR +10pp)" if single else ""): cond_discount,
        f"anchors confirm undervaluation ({triangulation.mode.value})": cond_anchors,
        f"conviction {conviction.total:.1f} >= "
        f"{config.triggers.min_conviction_to_buy:.0f}": cond_conviction,
    }

    reasons: list[str] = []
    flags = list(fair_value.flags) + list(triangulation.flags) + list(conviction.flags)

    if triangulation.anchors_disagree:
        # C4 refuses a combined verdict; that is a REVIEW, not a BUY and not a
        # silent pass.
        signal = Signal(
            symbol=symbol,
            signal=SignalType.REVIEW,
            classification=classification,
            reasons=[
                "REVIEW — mixed valuation signal; C4 divergence exceeded, so no "
                "combined verdict is offered. Both anchor readings are shown above."
            ],
            conditions=conditions,
            values={
                "price": price,
                "fair_value": fair_value.fair_value_per_share,
                "discount": discount,
                "threshold": threshold,
                "conviction": conviction.total,
            },
            flags=flags,
        )
        if ledger is not None:
            ledger.note(f"E · {signal.signal.value} — anchors disagree")
        return signal

    all_met = cond_discount and cond_anchors and cond_conviction
    signal_type = SignalType.BUY if all_met else SignalType.NO_ACTION
    if not all_met:
        unmet = [name for name, met in conditions.items() if not met]
        reasons.append("does not qualify: " + "; ".join(unmet))

    suppressed = None
    if all_met and in_earnings_blackout(next_earnings, config, as_of):
        suppressed = next_earnings
        reasons.append(
            "entry-timing hygiene: within 10 trading days of scheduled earnings"
        )

    signal = Signal(
        symbol=symbol,
        signal=signal_type,
        classification=classification,
        reasons=reasons,
        conditions=conditions,
        values={
            "price": price,
            "fair_value": fair_value.fair_value_per_share,
            "discount": discount,
            "threshold": threshold,
            "conviction": conviction.total,
        },
        suppressed_until=suppressed,
        flags=flags,
    )
    if ledger is not None:
        ledger.note(
            f"E · {signal_type.value} discount={discount:.1%} "
            f"threshold={threshold:.0%} conviction={conviction.total:.1f} "
            f"mode={triangulation.mode.value}"
        )
    return signal


def evaluate_sell(
    symbol: str,
    classification: Classification,
    price: float,
    fair_value: FairValue,
    triangulation: TriangulationResult,
    config: Config,
    module_a_failed: bool,
    ledger: AuditLedger | None = None,
) -> Signal:
    """SELL requires either Module A outright FAIL on quarterly re-run, or
    price >= Y% above fair value with both anchors agreeing on overvaluation.

    A mixed signal is no automatic trigger: it surfaces as "REVIEW — mixed
    valuation signal" with both anchors shown.
    """
    threshold = config.sell_threshold(classification)
    premium = -fair_value.discount_to(price)  # positive means above fair value

    conditions = {
        "Module A outright FAIL": module_a_failed,
        f"premium {premium:.1%} >= {threshold:.0%}": premium >= threshold,
        "both anchors agree on overvaluation": triangulation.both_confirm_overvaluation,
    }
    values = {
        "price": price,
        "fair_value": fair_value.fair_value_per_share,
        "premium": premium,
        "threshold": threshold,
    }

    if module_a_failed:
        signal = Signal(
            symbol,
            SignalType.SELL,
            classification,
            ["Module A outright FAIL on re-run"],
            conditions,
            values,
        )
    elif premium >= threshold and triangulation.both_confirm_overvaluation:
        signal = Signal(
            symbol,
            SignalType.SELL,
            classification,
            [
                f"price {premium:.1%} above fair value and both anchors confirm "
                "overvaluation"
            ],
            conditions,
            values,
        )
    elif premium >= threshold or triangulation.both_confirm_overvaluation:
        signal = Signal(
            symbol,
            SignalType.REVIEW,
            classification,
            [
                "REVIEW — mixed valuation signal; one condition met and one not. "
                "No automatic trigger."
            ],
            conditions,
            values,
            flags=list(triangulation.flags),
        )
    else:
        signal = Signal(symbol, SignalType.HOLD, classification, [], conditions, values)

    if ledger is not None:
        ledger.note(
            f"E · {signal.signal.value} premium={premium:.1%} "
            f"threshold={threshold:.0%} a_failed={module_a_failed}"
        )
    return signal


__all__ = [
    "Signal",
    "SignalType",
    "evaluate_buy",
    "evaluate_sell",
    "in_earnings_blackout",
]
