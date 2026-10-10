"""Crash protection for the leveraged trend strategy, on daily data.

docs/LEVERAGE_STRATEGY.md, "Crash protection": the monthly 10-month-average
signal, simulated day by day on Kenneth French's daily US market returns
from 1926, so falls inside a month (1929, 1987, 2020) are seen as they
happened, margin is checked on every close, and two protections are tested:
volatility-scaled leverage (L2-V) and a crash stop (L2-VS).

The account is modelled as a broker would see it: at each re-levering,
exposure X = L x equity, financed by a loan B = X - equity (or, below 1x,
the rest held in T-bills). Between re-leverings X moves with the market and
B grows at the T-bill rate plus the spread, so leverage drifts as it would
in a real margin account.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .leverage import BORROW_SPREAD, RUNNING_COST, SMA_MONTHS, SWITCH_COST
from .longhistory import fetch_french

DAILY_FILE = "F-F_Research_Data_Factors_daily"
TRADING_DAYS = 252

#: docs/LEVERAGE_STRATEGY.md, "Crash protection".
MAX_LEVERAGE = 2.0
TARGET_VOL = 0.30
VOL_WINDOW = 20
RESCALE_EVERY = 5
STOP_FALL = 0.10
LEVERAGE_CHANGE_COST = 0.0005  # per 1.0x changed, of equity
MAINTENANCE = 0.25
MAX_DRAWDOWN_LIMIT = -0.55
WORST_MONTH_LIMIT = -0.30
MIN_EXCESS = 0.01
POST_PUBLICATION = 20080101
GOAL = 0.15

#: Crashes shown one by one: (name, first day, last day).
CRASHES = (
    ("1929 crash", 19290903, 19291113),
    ("1929-32 Depression", 19290903, 19320601),
    ("1987 Black Monday", 19870825, 19871204),
    ("2000-02 dot-com", 20000324, 20021009),
    ("2008 financial crisis", 20071009, 20090309),
    ("2020 Covid crash", 20200219, 20200323),
    ("2022 bear market", 20220103, 20221012),
)


def parse_french_daily(text: str) -> dict[str, dict[int, float]]:
    """The daily table of a French CSV: ``{column: {YYYYMMDD: return}}``."""
    columns: list[str] | None = None
    out: dict[str, dict[int, float]] = {}
    started = False
    for raw in text.splitlines():
        line = raw.strip()
        if columns is None:
            if line.startswith(","):
                columns = [c.strip() for c in line.split(",")[1:]]
                out = {c: {} for c in columns}
            continue
        parts = [p.strip() for p in line.split(",")]
        if not parts or not re.fullmatch(r"\d{8}", parts[0]):
            if started:
                break
            continue
        started = True
        day = int(parts[0])
        for name, value in zip(columns, parts[1:]):
            try:
                v = float(value)
            except ValueError:
                continue
            if v <= -99.99:
                continue
            out[name][day] = v / 100.0
    return out


def load_daily(get: Callable[[str], bytes], cache_dir: Path | None = None
               ) -> tuple[dict[int, float], dict[int, float]]:
    table = parse_french_daily(fetch_french(DAILY_FILE, get, cache_dir))
    mkt_rf, rf = table["Mkt-RF"], table["RF"]
    days = sorted(set(mkt_rf) & set(rf))
    return {d: mkt_rf[d] + rf[d] for d in days}, {d: rf[d] for d in days}


# ------------------------------------------------------------------ simulation


@dataclass
class Run:
    name: str
    equity: dict[int, float] = field(default_factory=dict)
    margin_calls: list[int] = field(default_factory=list)
    min_ratio: float = 1.0
    min_ratio_day: int = 0
    switches: int = 0
    stops: int = 0
    days_on: int = 0
    days_total: int = 0


def simulate(market: dict[int, float], rf: dict[int, float], name: str, *,
             trend: bool = True, max_leverage: float = MAX_LEVERAGE,
             vol_scaled: bool = False, stop: bool = False, costs: bool = True,
             lookbacks: tuple[int, ...] = (SMA_MONTHS,),
             borrow_spread: float = BORROW_SPREAD, running_cost: float = RUNNING_COST,
             switch_cost: float = SWITCH_COST) -> Run:
    """One strategy, day by day. ``trend=False`` is buy-and-hold at
    ``max_leverage`` re-levered monthly; ``costs=False`` is the plain index,
    which pays no strategy costs. With several ``lookbacks`` (months), the
    leverage at each month-end is ``max_leverage`` times the share of their
    moving averages the index is above (L2-E)."""
    days = sorted(market)
    run = Run(name)
    equity, exposure, loan = 1.0, 0.0, 0.0
    level, month_levels = 1.0, []
    recent: list[float] = []
    risk_on = False          # the monthly signal's state
    stopped = False          # L2-VS: out until the next month-end
    relever_level = 1.0
    since_rescale = 0

    def target() -> float:
        if not vol_scaled or len(recent) < VOL_WINDOW:
            return max_leverage
        vol = statistics.pstdev(recent[-VOL_WINDOW:]) * math.sqrt(TRADING_DAYS)
        return min(max_leverage, TARGET_VOL / vol) if vol > 0 else max_leverage

    def set_exposure(new_lev: float, switch: bool) -> None:
        nonlocal equity, exposure, loan
        old_lev = exposure / equity if equity > 0 else 0.0
        cost = switch_cost if switch else LEVERAGE_CHANGE_COST * abs(new_lev - old_lev)
        if not costs:
            cost = 0.0
        equity *= 1 - cost
        exposure = new_lev * equity
        loan = exposure - equity  # negative: the rest sits in T-bills

    started = not trend  # buy-and-hold is invested from day one
    if started:
        set_exposure(max_leverage, switch=False)
    for i, d in enumerate(days):
        r, f = market[d], rf[d]
        # --- the day's move ---------------------------------------------
        if exposure > 0 or loan != 0:
            exposure *= 1 + r
            rate = f + (borrow_spread / TRADING_DAYS if loan > 0 else 0.0)
            loan *= 1 + rate
            equity = exposure - loan
            if exposure > 0 and costs:
                equity -= running_cost / TRADING_DAYS * (equity if equity > 0 else 0)
                loan = exposure - equity
        else:
            equity *= 1 + f
        if equity <= 0:
            equity, exposure, loan = 0.0, 0.0, 0.0
        level *= 1 + r
        recent.append(r)
        if len(recent) > VOL_WINDOW * 3:
            recent = recent[-VOL_WINDOW:]
        if started:
            run.days_total += 1
            if exposure > 0:
                run.days_on += 1
                ratio = equity / exposure
                if ratio < run.min_ratio:
                    run.min_ratio, run.min_ratio_day = ratio, d
                if exposure > equity and ratio < MAINTENANCE and (
                        not run.margin_calls or run.margin_calls[-1] // 100 != d // 100):
                    run.margin_calls.append(d)
        run.equity[d] = equity

        month_end = i + 1 == len(days) or days[i + 1] // 100 != d // 100
        # --- the crash stop (daily) ------------------------------------
        if stop and risk_on and not stopped and exposure > 0 and \
                level <= relever_level * (1 - STOP_FALL) and not month_end:
            set_exposure(0.0, switch=True)
            stopped = True
            run.stops += 1
            run.switches += 1
            continue
        # --- month-end: the signal, and re-levering ----------------------
        if month_end:
            month_levels.append(level)
            if not trend:
                set_exposure(max_leverage, switch=False)
                continue
            if len(month_levels) < max(lookbacks):
                continue
            share = sum(level > sum(month_levels[-n:]) / n for n in lookbacks) / len(lookbacks)
            on = share > 0
            started = True
            was_in = exposure > 0
            if on:
                set_exposure(target() * share, switch=not was_in)
                if not was_in:
                    run.switches += 1
            elif was_in:
                set_exposure(0.0, switch=True)
                run.switches += 1
            risk_on, stopped, relever_level, since_rescale = on, False, level, 0
            continue
        # --- weekly volatility rescaling (L2-V, L2-VS) -------------------
        if vol_scaled and risk_on and not stopped and exposure > 0:
            since_rescale += 1
            if since_rescale >= RESCALE_EVERY:
                since_rescale = 0
                set_exposure(target(), switch=False)
    return run


# ------------------------------------------------------------------ statistics


def _ymd(d: int) -> str:
    return f"{d // 10000}-{d // 100 % 100:02d}-{d % 100:02d}"


def cagr(run: Run, start: int, end: int) -> float | None:
    days = [d for d in run.equity if start <= d <= end]
    if len(days) < 2:
        return None
    a, b = run.equity[days[0]], run.equity[days[-1]]
    years = len(days) / TRADING_DAYS
    if a <= 0:
        return None
    return (b / a) ** (1 / years) - 1 if b > 0 else -1.0


def drawdown(run: Run, start: int, end: int) -> tuple[float, int, int]:
    peak, peak_d, worst, w = 0.0, start, 0.0, (start, start)
    for d in sorted(run.equity):
        if not start <= d <= end:
            continue
        v = run.equity[d]
        if v > peak:
            peak, peak_d = v, d
        if peak > 0 and v / peak - 1 < worst:
            worst, w = v / peak - 1, (peak_d, d)
    return worst, w[0], w[1]


def worst_month(run: Run, start: int, end: int) -> tuple[float, int]:
    ends: dict[int, float] = {}
    for d in sorted(run.equity):
        if start <= d <= end:
            ends[d // 100] = run.equity[d]
    months = sorted(ends)
    worst, when = 0.0, months[0] if months else 0
    for a, b in zip(months, months[1:]):
        if ends[a] > 0:
            ch = ends[b] / ends[a] - 1
            if ch < worst:
                worst, when = ch, b
    return worst, when


def window_return(run: Run, start: int, end: int) -> float | None:
    days = [d for d in sorted(run.equity) if start <= d <= end]
    if len(days) < 2:
        return None
    before = [d for d in sorted(run.equity) if d < start]
    a = run.equity[before[-1]] if before else run.equity[days[0]]
    return run.equity[days[-1]] / a - 1 if a > 0 else None


def _pct(x: float | None) -> str:
    return f"{x:+.1%}" if x is not None else "n/a"


def evaluate(market: dict[int, float], rf: dict[int, float]) -> tuple[str, dict[str, bool]]:
    runs = {
        "L2": simulate(market, rf, "L2: trend, 2.0x (daily data)"),
        "L2-V": simulate(market, rf, "L2-V: trend, volatility-scaled", vol_scaled=True),
        "L2-VS": simulate(market, rf, "L2-VS: L2-V + 10% crash stop",
                          vol_scaled=True, stop=True),
    }
    # Compare from the first day any trend run can act (10 month-ends in).
    month_ends = sorted({d // 100 for d in market})
    first = min(d for d in market if d // 100 >= month_ends[SMA_MONTHS])
    last = max(market)
    days = [d for d in sorted(market) if d >= first]
    mid = days[len(days) // 2]
    bench = simulate(market, rf, "market, buy and hold", trend=False, max_leverage=1.0,
                     costs=False)
    context = [
        bench,
        simulate(market, rf, "buy and hold 2.0x, no filter", trend=False),
    ]
    periods = [("full", first, last), ("1st half", first, days[len(days) // 2 - 1]),
               ("2nd half", mid, last), ("since 1990", 19900101, last),
               ("since 2008", POST_PUBLICATION, last)]

    lines = [
        "=" * 78,
        "CRASH PROTECTION — leveraged trend on DAILY data (docs/LEVERAGE_STRATEGY.md)",
        "=" * 78,
        f"Data: Kenneth French daily US market and T-bills, {_ymd(first)} to {_ymd(last)} "
        f"({len(days):,} trading days).",
        f"L2-V: leverage min({MAX_LEVERAGE:.1f}, {TARGET_VOL:.0%} / {VOL_WINDOW}-day "
        f"volatility), reset every {RESCALE_EVERY} days. L2-VS adds: out to T-bills "
        f"after a {STOP_FALL:.0%} fall from the last re-levering, until month-end.",
        "",
        "COMPOUND RETURN PER YEAR (deepest fall on daily closes)",
        f"  {'':36s}" + "".join(f"{p[0]:>17s}" for p in periods),
    ]
    rows = [*runs.values(), *context]
    for run in rows:
        cells = []
        for _, a, b in periods:
            dd = drawdown(run, a, b)[0]
            cells.append(f"{_pct(cagr(run, a, b))} ({dd:.0%})".rjust(17))
        lines.append(f"  {run.name[:36]:36s}" + "".join(cells))

    lines += ["", "THE CRASHES, ONE BY ONE (return over each window)",
              f"  {'':24s}" + "".join(f"{r.name.split(':')[0][:14]:>15s}" for r in rows)]
    for name, a, b in CRASHES:
        if a < first:
            cells = "".join(f"{_pct(window_return(r, a, b)):>15s}" for r in rows)
            lines.append(f"  {name[:24]:24s}{cells}   (before the trend runs start: market rows only meaningful)")
        else:
            cells = "".join(f"{_pct(window_return(r, a, b)):>15s}" for r in rows)
            lines.append(f"  {name[:24]:24s}{cells}")

    lines += ["", "WORST MOMENTS, FULL PERIOD"]
    for run in rows:
        wm, wm_when = worst_month(run, first, last)
        dd, a, b = drawdown(run, first, last)
        worst_day = min(
            (run.equity[d] / run.equity[p] - 1, d)
            for p, d in zip(sorted(run.equity), sorted(run.equity)[1:])
            if p >= first and run.equity[p] > 0
        )
        lines.append(
            f"  {run.name[:36]:36s} worst day {worst_day[0]:+.0%} ({_ymd(worst_day[1])}) · "
            f"worst month {wm:+.0%} ({wm_when // 100}-{wm_when % 100:02d}) · deepest fall "
            f"{dd:.0%} ({_ymd(a)} to {_ymd(b)})")
        if run.days_on and run in runs.values():
            lines.append(
                f"  {'':36s} lowest equity/exposure {run.min_ratio:.0%} on "
                f"{_ymd(run.min_ratio_day)} · margin calls at {MAINTENANCE:.0%}: "
                f"{len(run.margin_calls)}"
                + (f" ({', '.join(_ymd(d) for d in run.margin_calls[:6])})" if run.margin_calls else "")
                + f" · invested {run.days_on / max(run.days_total, 1):.0%} of days"
                + (f" · stops {run.stops}" if run.stops else ""))

    verdicts: dict[str, bool] = {}
    for key, run in runs.items():
        def beats(a, b, margin=0.0):
            s, x = cagr(run, a, b), cagr(bench, a, b)
            return s is not None and x is not None and s - x > margin, s, x
        c1, c2a, c2b, c3 = (beats(first, last, MIN_EXCESS), beats(*periods[1][1:]),
                            beats(*periods[2][1:]), beats(POST_PUBLICATION, last))
        dd = drawdown(run, first, last)
        wm = worst_month(run, first, last)
        checks = [
            (f"beats buy-and-hold by {MIN_EXCESS:.0%}/yr, full period "
             f"({_pct(c1[1])} vs {_pct(c1[2])})", c1[0]),
            (f"beats it in the 1st half ({_pct(c2a[1])} vs {_pct(c2a[2])})", c2a[0]),
            (f"beats it in the 2nd half ({_pct(c2b[1])} vs {_pct(c2b[2])})", c2b[0]),
            (f"beats it since 2008 ({_pct(c3[1])} vs {_pct(c3[2])})", c3[0]),
            (f"deepest fall no deeper than {MAX_DRAWDOWN_LIMIT:.0%} ({dd[0]:.0%})",
             dd[0] >= MAX_DRAWDOWN_LIMIT),
            (f"worst month no worse than {WORST_MONTH_LIMIT:.0%} ({wm[0]:+.0%})",
             wm[0] >= WORST_MONTH_LIMIT),
            (f"no margin call at {MAINTENANCE:.0%} maintenance ({len(run.margin_calls)})",
             not run.margin_calls),
        ]
        verdicts[key] = all(ok for _, ok in checks)
        goal = " · ".join(
            f"{label}: {_pct(cagr(run, a, b))}"
            + (" MET" if (cagr(run, a, b) or 0) >= GOAL else "")
            for label, a, b in (periods[0], periods[3], periods[4]))
        lines += ["", f"CRITERIA — {run.name}"]
        lines += [f"  [{'PASS' if ok else 'FAIL'}] {t}" for t, ok in checks]
        lines.append(f"  GOAL {GOAL:.0%}+/yr — {goal}")
        if key != "L2":
            lines.append(f"  VERDICT: {'PASSED' if verdicts[key] else 'FAILED'}")
        else:
            lines.append("  (reference: L2 as chosen, measured on daily data)")

    passing = [k for k in ("L2-V", "L2-VS") if verdicts[k]]
    chosen = max(passing, key=lambda k: cagr(runs[k], first, last) or -1) if passing else None
    lines += [
        "",
        "SELECTION (registered: the higher full-period CAGR among L2-V and L2-VS if both pass)",
        "  " + (f"selected: {chosen} — replaces L2 in paper trading" if chosen else
                "neither passes — L2 stays in paper trading (operator's decision); "
                "its daily-data row above is its true crash risk"),
        "=" * 78,
    ]
    return "\n".join(lines) + "\n", verdicts


def run(get: Callable[[str], bytes], cache_dir: Path | None = None) -> tuple[str, dict]:
    market, rf = load_daily(get, cache_dir)
    return evaluate(market, rf)
