"""Paper trading of the leveraged trend strategy, with a margin-call alarm.

docs/LEVERAGE_STRATEGY.md, stage 2, as chosen by the operator on 2026-10-09:
2.0x the S&P 500 (total return) while it is above its 10-month average,
T-bills otherwise, re-levered to 2.0x at each month's signal.

Run daily on live closes. Each run marks the paper account, applies a new
month's signal once that month has closed, and measures how far the market
is from a margin call on a real account run the same way:

    At the last rebalance, equity E holds exposure L·E (a loan of (L-1)·E).
    After the index moves by x, equity is E·(1 + L·x) and exposure L·E·(1+x).
    The broker calls when equity / exposure falls below the maintenance
    share m, which happens at x = (m·L - 1) / (L·(1 - m)).

The alarm grades how much of that distance has been used. To get back to L
it states two remedies per $10,000 of equity at the last rebalance: add
cash, or sell part of the position to repay part of the loan.

Nothing here places an order.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Sequence

from .leverage import BORROW_SPREAD, RUNNING_COST, SMA_MONTHS, SWITCH_COST

#: Alarm levels by the share of the distance to a margin call already used.
LEVELS = ((1.0, "MARGIN CALL LEVEL"), (0.75, "URGENT"), (0.5, "WARNING"))
OK = "OK"


@dataclass
class LeverageState:
    started_on: date
    leverage: float
    #: Paper equity at the last rebalance (or the start).
    value: float
    #: The index level and date at the last rebalance.
    level_at_rebalance: float
    rebalanced_on: date
    risk_on: bool = False
    #: "YYYY-MM" of the month-end whose signal the position follows.
    signal_month: str | None = None
    start_level: float = 0.0
    log: list[dict] = field(default_factory=list)
    marks: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        raw = asdict(self)
        for key in ("started_on", "rebalanced_on"):
            raw[key] = raw[key].isoformat()
        return json.dumps(raw, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "LeverageState":
        raw = json.loads(text)
        for key in ("started_on", "rebalanced_on"):
            raw[key] = date.fromisoformat(raw[key])
        return cls(**raw)


def load(path: Path) -> LeverageState | None:
    return LeverageState.from_json(path.read_text()) if path.exists() else None


def save(state: LeverageState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.to_json())


# --------------------------------------------------------------------- signal


def month_end_closes(closes: Sequence[tuple[date, float]], today: date
                     ) -> list[tuple[str, date, float]]:
    """The last close of each *completed* month, oldest first. The current
    month counts only once today is past its last trading day, which shows
    as a close in a later month."""
    by_month: dict[str, tuple[date, float]] = {}
    for day, close in sorted(closes):
        by_month[f"{day.year}-{day.month:02d}"] = (day, close)
    current = f"{today.year}-{today.month:02d}"
    return [(m, d, c) for m, (d, c) in sorted(by_month.items()) if m != current]


def signal(month_ends: list[tuple[str, date, float]]) -> tuple[bool, float, float] | None:
    """(risk-on?, last month-end level, its 10-month average)."""
    if len(month_ends) < SMA_MONTHS:
        return None
    last = month_ends[-1][2]
    sma = sum(c for _, _, c in month_ends[-SMA_MONTHS:]) / SMA_MONTHS
    return last > sma, last, sma


# ---------------------------------------------------------------- valuation


def equity_now(state: LeverageState, level: float, rf: float, today: date) -> float:
    """Paper equity at ``level``: levered index move, minus financing and
    running costs while risk-on; T-bill interest while risk-off."""
    days = max((today - state.rebalanced_on).days, 0)
    if not state.risk_on:
        return state.value * (1 + rf * days / 365)
    lev = state.leverage
    move = level / state.level_at_rebalance - 1
    cost = state.value * ((lev - 1) * (rf + BORROW_SPREAD) + RUNNING_COST) * days / 365
    return max(state.value * (1 + lev * move) - cost, 0.0)


def call_move(leverage: float, maintenance: float) -> float:
    """The index move since the rebalance at which the broker calls."""
    return (maintenance * leverage - 1) / (leverage * (1 - maintenance))


@dataclass
class Margin:
    level: str
    move: float              # index move since the last rebalance
    call_at: float           # index move that triggers a margin call
    used: float              # share of that distance used
    equity_ratio: float      # equity / exposure now
    inject_per_10k: float    # cash to add, per $10k equity at the rebalance
    sell_per_10k: float      # position to sell instead, same basis


def margin(state: LeverageState, level: float, maintenance: float) -> Margin | None:
    if not state.risk_on or state.leverage <= 1:
        return None
    lev = state.leverage
    move = level / state.level_at_rebalance - 1
    at = call_move(lev, maintenance)
    used = max(move / at, 0.0) if move < 0 else 0.0
    grade = OK
    for threshold, name in LEVELS:
        if used >= threshold:
            grade = name
            break
    # Per $10,000 of equity at the rebalance (financing costs left out: a
    # few dollars a month against a margin decision).
    e0 = 10_000.0
    exposure = lev * e0 * (1 + move)
    equity = e0 * (1 + lev * move)
    return Margin(
        level=grade, move=move, call_at=at, used=used,
        equity_ratio=equity / exposure if exposure > 0 else 0.0,
        inject_per_10k=max(exposure / lev - equity, 0.0),
        sell_per_10k=max(exposure - lev * equity, 0.0),
    )


# ---------------------------------------------------------------------- step


def step(state: LeverageState | None, closes: Sequence[tuple[date, float]],
         rf: float, today: date, leverage: float, capital: float
         ) -> tuple[LeverageState, str | None]:
    """One run: start the book, apply a new month's signal, or just mark.
    Returns the state and, when the position changed, the instruction."""
    closes = sorted(closes)
    last_day, level = closes[-1]
    months = month_end_closes(closes, today)
    sig = signal(months)
    if state is None:
        state = LeverageState(started_on=today, leverage=leverage, value=capital,
                              level_at_rebalance=level, rebalanced_on=last_day,
                              start_level=level)
    instruction = None
    if sig is not None and months[-1][0] != state.signal_month:
        on, last, sma = sig
        equity = equity_now(state, level, rf, last_day)
        switched = on != state.risk_on or state.signal_month is None
        if switched and (on or state.risk_on):
            equity *= 1 - SWITCH_COST
        state.value, state.level_at_rebalance = equity, level
        state.rebalanced_on, state.signal_month = last_day, months[-1][0]
        previous, state.risk_on = state.risk_on, on
        what = (f"HOLD {state.leverage:.1f}x THE S&P 500" if on else "HOLD T-BILLS (no stocks)")
        state.log.append({
            "date": last_day.isoformat(), "signal_month": months[-1][0],
            "month_end_level": round(last, 2), "average": round(sma, 2),
            "risk_on": on, "value": round(equity, 2),
            "action": what if on != previous or len(state.log) == 0 else f"{what} (re-lever)",
        })
        if on != previous or len(state.log) == 1:
            instruction = what
    value = equity_now(state, level, rf, last_day)
    state.marks.append({"date": last_day.isoformat(), "level": round(level, 2),
                        "value": round(value, 2)})
    # One mark per trading day: a second run on the same close replaces it.
    if len(state.marks) >= 2 and state.marks[-2]["date"] == state.marks[-1]["date"]:
        del state.marks[-2]
    return state, instruction


# -------------------------------------------------------------------- report


def report(state: LeverageState, closes: Sequence[tuple[date, float]], rf: float,
           today: date, maintenance: float, instruction: str | None) -> tuple[str, str]:
    """(full report, alert text). The alert text's first line is the level:
    OK, WARNING, URGENT or MARGIN CALL LEVEL; a position change adds a line."""
    closes = sorted(closes)
    last_day, level = closes[-1]
    months = month_end_closes(closes, today)
    sig = signal(months)
    value = equity_now(state, level, rf, last_day)
    m = margin(state, level, maintenance)
    index_change = level / state.start_level - 1 if state.start_level else 0.0

    lines = [
        "=" * 78,
        f"LEVERAGED TREND — PAPER TRADING · {last_day.isoformat()} close",
        "=" * 78,
        f"{state.leverage:.1f}x the S&P 500 (total return) while above its 10-month "
        "average, T-bills otherwise (docs/LEVERAGE_STRATEGY.md)",
        f"started {state.started_on.isoformat()} · paper value {value:,.0f} "
        f"({value / (state.log[0]['value'] if state.log else value) - 1:+.2%} since the first signal) · "
        f"S&P 500 TR {index_change:+.2%} since the start",
        "",
        "POSITION",
        f"  now: {'RISK-ON: ' + format(state.leverage, '.1f') + 'x the S&P 500' if state.risk_on else 'RISK-OFF: T-bills'}"
        f" (signal of {state.signal_month or 'n/a'}, set {state.rebalanced_on.isoformat()})",
    ]
    if sig is not None:
        on, last, sma = sig
        lines.append(f"  last month-end: {last:,.2f} vs 10-month average {sma:,.2f} "
                     f"({last / sma - 1:+.1%}) -> {'risk-on' if on else 'risk-off'}")
    lines.append("  next signal: the last trading day of this month")
    if instruction:
        lines.append(f"  POSITION CHANGE: {instruction}")
    lines += ["", f"MARGIN (maintenance {maintenance:.0%} of exposure assumed; "
                  "set yours in scripts/leverage_config.json)"]
    if m is None:
        lines.append("  no borrowing while risk-off: no margin risk")
        level_line = OK
    else:
        lines += [
            f"  index since the rebalance: {m.move:+.1%} · a margin call comes at "
            f"{m.call_at:+.1%} · {m.used:.0%} of that distance used",
            f"  equity is {m.equity_ratio:.0%} of exposure (call below {maintenance:.0%})",
            f"  status: {m.level}",
        ]
        level_line = m.level
        if m.level != OK:
            lines += [
                "  TO GET BACK TO "
                f"{state.leverage:.1f}x, per $10,000 of equity at the last rebalance:",
                f"    either add ${m.inject_per_10k:,.0f} cash to the account",
                f"    or sell ${m.sell_per_10k:,.0f} of the position to repay part of the loan",
                "  Selling is usually the safer of the two: it cuts risk instead of adding",
                "  more money at risk, and the trend rule may switch to T-bills at month-end anyway.",
            ]
    lines += ["", "Simulated only. Nothing here places an order.", "=" * 78]
    text = "\n".join(lines) + "\n"

    alert = [level_line]
    if instruction:
        alert.append(f"POSITION CHANGE: {instruction}")
    alert += ["", *lines[1:]]
    return text, "\n".join(alert) + "\n"
