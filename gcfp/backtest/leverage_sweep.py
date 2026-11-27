"""The growth-optimal (Kelly) leverage, two ways, on daily data from 1926.

1. **Formula.** For returns with mean m and variance s² per day in excess
   of the borrowing cost, long-run compound growth at leverage L is about
   g(L) = r + L·m − L²·s²/2, which peaks at L* = m / s² (the Kelly
   leverage). At L*, growth is r + m²/(2s²); at 2·L* it is back to r: past
   twice Kelly, leverage destroys wealth.
2. **Simulation.** The daily margin-account model of leverage_daily.py, run
   at every leverage from 1x to 4x, with real costs, monthly re-levering
   and the crashes as they happened. The leverage with the highest CAGR is
   the in-sample optimum.

Both are measured in-sample, and both overstate what the future will
reward. The optimum is reported per period to show how much it moves.
Nothing here is a registered rule.
"""

from __future__ import annotations

import math
import statistics
from pathlib import Path
from typing import Callable

from .leverage import BORROW_SPREAD, SMA_MONTHS
from .leverage_daily import (
    MAINTENANCE, POST_PUBLICATION, TRADING_DAYS, _ymd, cagr, drawdown, load_daily,
    simulate, worst_month,
)

LEVELS = (1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.5, 4.0)


def risk_on_days(market: dict[int, float]) -> set[int]:
    """Days the monthly 10-month-average rule holds the market."""
    days = sorted(market)
    level, ends, on, out = 1.0, [], False, set()
    for i, d in enumerate(days):
        if on:
            out.add(d)
        level *= 1 + market[d]
        if i + 1 == len(days) or days[i + 1] // 100 != d // 100:
            ends.append(level)
            if len(ends) >= SMA_MONTHS:
                on = level > sum(ends[-SMA_MONTHS:]) / SMA_MONTHS
    return out


def kelly(market: dict[int, float], rf: dict[int, float], days: list[int]
          ) -> tuple[float, float, float, float]:
    """(Kelly leverage, growth at Kelly per year, excess mean per year,
    volatility per year) over ``days``, excess over T-bills + the spread."""
    x = [market[d] - (rf[d] + BORROW_SPREAD / TRADING_DAYS) for d in days]
    m, s2 = statistics.fmean(x), statistics.pvariance(x)
    l_star = m / s2
    r = statistics.fmean([rf[d] for d in days])
    growth = (r + m * m / (2 * s2)) * TRADING_DAYS
    return l_star, growth, m * TRADING_DAYS, math.sqrt(s2 * TRADING_DAYS)


def _pct(x: float | None) -> str:
    return f"{x:+.1%}" if x is not None else "n/a"


def evaluate(market: dict[int, float], rf: dict[int, float]) -> str:
    month_ends = sorted({d // 100 for d in market})
    first = min(d for d in market if d // 100 >= month_ends[SMA_MONTHS])
    last = max(market)
    days = [d for d in sorted(market) if d >= first]
    mid = days[len(days) // 2]
    periods = [("full", first, last), ("1st half", first, days[len(days) // 2 - 1]),
               ("2nd half", mid, last), ("since 1990", 19900101, last),
               ("since 2008", POST_PUBLICATION, last)]
    on = risk_on_days(market)

    lines = [
        "=" * 78,
        "WHAT LEVERAGE GROWS MONEY FASTEST? (Kelly criterion, daily data)",
        "=" * 78,
        f"Kenneth French daily US market, {_ymd(first)} to {_ymd(last)}. Borrowing at "
        f"T-bills + {BORROW_SPREAD:.1%}.",
        "",
        "1. THE FORMULA: L* = (expected return - borrowing cost) / variance",
        f"  {'':28s}{'Kelly L*':>10s}{'growth at L*':>14s}{'excess/yr':>11s}{'vol/yr':>9s}",
    ]
    for label, a, b in periods:
        span = [d for d in days if a <= d <= b]
        for name, sel in (("trend, risk-on days", [d for d in span if d in on]),
                          ("buy and hold", span)):
            if len(sel) < 250:
                continue
            l_star, growth, ex, vol = kelly(market, rf, sel)
            lines.append(f"  {label + ', ' + name:28s}{l_star:>9.2f}x{_pct(growth):>14s}"
                         f"{_pct(ex):>11s}{vol:>8.0%}")

    lines += ["", "2. THE SIMULATION: CAGR by leverage (deepest fall on daily closes)"]
    for kind, trend in (("TREND (10-month average, T-bills when below)", True),
                        ("BUY AND HOLD, re-levered monthly", False)):
        lines += ["", f"  {kind}",
                  f"  {'L':>6s}" + "".join(f"{p[0]:>17s}" for p in periods)
                  + f"{'worst day':>11s}{'worst month':>13s}{'margin calls':>14s}"]
        best: dict[str, tuple[float, float]] = {}
        for lev in LEVELS:
            run = simulate(market, rf, f"{lev}x", trend=trend, max_leverage=lev,
                           costs=True)
            cells = []
            for label, a, b in periods:
                c = cagr(run, a, b)
                dd = drawdown(run, a, b)[0]
                cells.append(f"{_pct(c)} ({dd:.0%})".rjust(17))
                if c is not None and (label not in best or c > best[label][1]):
                    best[label] = (lev, c)
            eq = [run.equity[d] for d in days]
            worst_day = min((b / a - 1) for a, b in zip(eq, eq[1:]) if a > 0) if eq else 0.0
            wm = worst_month(run, first, last)[0]
            calls = len(run.margin_calls) if lev > 1 else 0
            lines.append(f"  {lev:>5.2f}x" + "".join(cells) + f"{worst_day:>11.0%}"
                         f"{wm:>13.0%}{calls:>14d}")
        lines.append("  best CAGR: " + " · ".join(
            f"{label} {lev:.2f}x ({_pct(c)})" for label, (lev, c) in best.items()))

    lines += [
        "",
        "READING IT",
        "  Kelly maximises long-run growth and nothing else. At full Kelly, halving",
        "  your money along the way is expected, not a tail risk; past 2x Kelly,",
        "  more leverage means LESS growth. Estimates of the inputs are noisy, so",
        "  practitioners use half Kelly: about 3/4 of the growth for 1/2 the swings.",
        f"  Margin calls are counted at {MAINTENANCE:.0%} maintenance (a margin loan);",
        "  futures need far less. A margin loan cannot open more than 2x at all.",
        "=" * 78,
    ]
    return "\n".join(lines) + "\n"


def run(get: Callable[[str], bytes], cache_dir: Path | None = None) -> str:
    market, rf = load_daily(get, cache_dir)
    return evaluate(market, rf)
