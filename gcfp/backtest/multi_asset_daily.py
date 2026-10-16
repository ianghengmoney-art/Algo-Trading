"""MA's daily crash check (docs/LEVERAGE_STRATEGY.md): the chosen 3.82x
equal-sleeve portfolio replayed day by day, margin checked on every close.

Signals and re-levering stay monthly, as the rule says. Between month-ends
each futures position moves with its own market every day:

    equity_t = equity_{t-1} (1 + rf) + sum_i X_i (r_i - rf)
               - max(sum X - equity, 0) x spread/252 - equity x running/252
    X_i,t    = X_i,{t-1} (1 + r_i)

A broker calls when equity falls below the maintenance share of gross
exposure. Daily series are ``{YYYYMMDD: return}``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .leverage import SMA_MONTHS
from .leverage_daily import load_daily, parse_french_daily
from .longhistory import fetch_french
from .multi_asset import RUNNING, SPREAD, SWITCH, bond_return

INTL_DAILY_FILE = "Developed_ex_US_3_Factors_Daily"
LEVERAGE = 3.82
MAINTENANCE = 0.08
L2_LEVERAGE = 2.0
TRADING_DAYS = 252
WINDOWS = (
    ("2000-02 dot-com", 20010101, 20021009),
    ("2008 crisis", 20071009, 20090309),
    ("Oct 2008 alone", 20081001, 20081031),
    ("2011 euro crisis", 20110429, 20111003),
    ("2015 China scare", 20150720, 20150825),
    ("Feb 2018 vol spike", 20180126, 20180208),
    ("2020 Covid", 20200219, 20200323),
    ("2022 bear", 20220103, 20221012),
)

Daily = dict[int, float]


def _key(d) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def from_levels(levels: list[tuple[int, float]]) -> Daily:
    levels = sorted(levels)
    return {b: lb / la - 1 for (a, la), (b, lb) in zip(levels, levels[1:]) if la > 0}


def treasury_daily(yields: list[tuple[int, float]]) -> Daily:
    """Daily 10-year Treasury returns from daily yields (percent)."""
    pts = sorted(yields)
    out: Daily = {}
    for (a, ya), (b, yb) in zip(pts, pts[1:]):
        days = _days_between(a, b)
        out[b] = bond_return(ya / 100, yb / 100, months=days / 365 * 12)
    return out


def _days_between(a: int, b: int) -> int:
    from datetime import date

    da = date(a // 10000, a // 100 % 100, a % 100)
    db = date(b // 10000, b // 100 % 100, b % 100)
    return (db - da).days


def load(get: Callable[[str], bytes], cache_dir: Path | None, daily) -> tuple[dict[str, Daily], Daily, list[str]]:
    us, rf = load_daily(get, cache_dir)
    notes = ["US stocks: French daily"]
    try:
        t = parse_french_daily(fetch_french(INTL_DAILY_FILE, get, cache_dir))
        intl = {d: t["Mkt-RF"][d] + t["RF"][d] for d in t["Mkt-RF"] if d in t["RF"]}
        if not intl:
            raise ValueError("empty")
        notes.append(f"Intl stocks: French Developed ex US daily from {min(intl)}")
    except Exception as exc:
        intl = from_levels([(_key(d), c) for d, c in daily("EFA")])
        notes.append(f"Intl stocks: Yahoo EFA from {min(intl)} (French daily failed: "
                     f"{type(exc).__name__})")
    tsy = treasury_daily([(_key(d), y) for d, y in daily("^TNX")])
    notes.append(f"Treasuries: from Yahoo ^TNX daily yields, from {min(tsy)}")
    gold = from_levels([(_key(d), c) for d, c in daily("GC=F")])
    notes.append(f"Gold: Yahoo GC=F daily from {min(gold)}")
    return {"US stocks": us, "Intl stocks": intl, "Treasuries": tsy, "Gold": gold}, rf, notes


def align(asset: Daily, calendar: list[int]) -> Daily:
    """The asset's return on each calendar day, compounding any days it
    traded that the calendar does not have into the next calendar day."""
    out: Daily = {}
    pending, j = 1.0, 0
    days = sorted(asset)
    for d in calendar:
        while j < len(days) and days[j] <= d:
            pending *= 1 + asset[days[j]]
            j += 1
        if j > 0:
            out[d] = pending - 1
        pending = 1.0
    return out


def simulate(assets: dict[str, Daily], rf: Daily, leverage: float, start_after: int,
             maintenance: float = MAINTENANCE) -> dict:
    """Day by day on the US calendar (``rf``'s days). Trading starts at the
    first month-end on or after ``start_after`` (YYYYMMDD) at which every
    sleeve has 10 month-ends of history; signals and re-levering are monthly."""
    calendar = sorted(rf)
    names = list(assets)
    aligned = {a: align(assets[a], calendar) for a in names}
    first_seen = {a: min(assets[a]) for a in names}
    level = {a: 1.0 for a in names}
    month_ends: dict[str, list[float]] = {a: [] for a in names}
    equity, X = 1.0, {a: 0.0 for a in names}
    started, in_trend = False, {a: False for a in names}
    out = {"equity": {}, "min_ratio": 1.0, "min_ratio_day": 0, "calls": []}
    for i, d in enumerate(calendar):
        f = rf[d]
        if started:
            gross = sum(X.values())
            pnl = sum(X[a] * (aligned[a].get(d, 0.0) - f) for a in names)
            equity = (equity * (1 + f) + pnl - max(gross - equity, 0) * SPREAD / TRADING_DAYS
                      - equity * RUNNING / TRADING_DAYS)
            equity = max(equity, 0.0)
            for a in names:
                X[a] *= 1 + aligned[a].get(d, 0.0)
            gross = sum(X.values())
            if gross > 0:
                ratio = equity / gross
                if ratio < out["min_ratio"]:
                    out["min_ratio"], out["min_ratio_day"] = ratio, d
                if ratio < maintenance and (not out["calls"] or out["calls"][-1] // 100 != d // 100):
                    out["calls"].append(d)
            out["equity"][d] = equity
        for a in names:
            level[a] *= 1 + aligned[a].get(d, 0.0)
        if i + 1 < len(calendar) and calendar[i + 1] // 100 == d // 100:
            continue  # not a month-end
        for a in names:
            if d >= first_seen[a]:
                month_ends[a].append(level[a])
        if not all(len(month_ends[a]) >= SMA_MONTHS for a in names):
            continue
        if not started:
            if d < start_after:
                continue
            started = True
            out["equity"][d] = equity
        new = {a: month_ends[a][-1] > sum(month_ends[a][-SMA_MONTHS:]) / SMA_MONTHS
               for a in names}
        sleeve = leverage / len(names)
        for a in names:
            if new[a] != in_trend[a] and (new[a] or in_trend[a]):
                equity -= sleeve * equity * SWITCH
        in_trend = new
        X = {a: (sleeve * equity if in_trend[a] else 0.0) for a in names}
    return out


def stats(eq: dict[int, float], a: int, b: int) -> dict:
    days = [d for d in sorted(eq) if a <= d <= b]
    if len(days) < 2:
        return {"cagr": None, "dd": 0.0, "worst_day": 0.0, "worst_month": 0.0}
    peak, dd, worst_day = eq[days[0]], 0.0, 0.0
    for x, y in zip(days, days[1:]):
        if eq[x] > 0:
            worst_day = min(worst_day, eq[y] / eq[x] - 1)
        peak = max(peak, eq[y])
        if peak > 0:
            dd = min(dd, eq[y] / peak - 1)
    ends: dict[int, float] = {}
    for d in days:
        ends[d // 100] = eq[d]
    ms = sorted(ends)
    worst_month = min((ends[y] / ends[x] - 1 for x, y in zip(ms, ms[1:]) if ends[x] > 0),
                      default=0.0)
    years = len(days) / TRADING_DAYS
    a0, b0 = eq[days[0]], eq[days[-1]]
    cagr = (b0 / a0) ** (1 / years) - 1 if a0 > 0 and b0 > 0 else -1.0
    return {"cagr": cagr, "dd": dd, "worst_day": worst_day, "worst_month": worst_month}


def window(eq: dict[int, float], a: int, b: int) -> float | None:
    days = [d for d in sorted(eq) if a <= d <= b]
    before = [d for d in sorted(eq) if d < a]
    if not days:
        return None
    start = eq[before[-1]] if before else eq[days[0]]
    return eq[days[-1]] / start - 1 if start > 0 else None


def _pct(x) -> str:
    return f"{x:+.1%}" if x is not None else "n/a"


def evaluate(assets: dict[str, Daily], rf: Daily, notes: list[str]) -> tuple[str, bool]:
    # Start once every sleeve has 10 month-ends of daily data.
    first_month = max(sorted({d // 100 for d in s})[SMA_MONTHS] for s in assets.values())
    start_after = first_month * 100  # the first month-end on or after its start
    ma = simulate(assets, rf, LEVERAGE, start_after)
    l2 = simulate({"US stocks": assets["US stocks"]}, rf, L2_LEVERAGE, start_after,
                  maintenance=0.25)
    first, last = min(ma["equity"]), max(ma["equity"])
    s_ma, s_l2 = stats(ma["equity"], first, last), stats(l2["equity"], first, last)

    def ymd(d: int) -> str:
        return f"{d // 10000}-{d // 100 % 100:02d}-{d % 100:02d}" if d else "n/a"

    lines = [
        "=" * 78,
        "MA DAILY CRASH CHECK — 3.82x, day by day (docs/LEVERAGE_STRATEGY.md)",
        "=" * 78,
        *[f"Data: {n}" for n in notes],
        f"Period: {ymd(first)} to {ymd(last)}. Monthly signals, daily marks, margin "
        f"checked every close at {MAINTENANCE:.0%} of gross exposure.",
        "",
        f"  {'':28s}{'MA 3.82x':>14s}{'L2 2.0x':>14s}",
        f"  {'per year':28s}{_pct(s_ma['cagr']):>14s}{_pct(s_l2['cagr']):>14s}",
        f"  {'deepest fall (daily)':28s}{s_ma['dd']:>14.0%}{s_l2['dd']:>14.0%}",
        f"  {'worst day':28s}{s_ma['worst_day']:>+14.1%}{s_l2['worst_day']:>+14.1%}",
        f"  {'worst month':28s}{s_ma['worst_month']:>+14.1%}{s_l2['worst_month']:>+14.1%}",
        f"  {'lowest equity / exposure':28s}{ma['min_ratio']:>14.0%}{l2['min_ratio']:>14.0%}",
        f"  {'  on':28s}{ymd(ma['min_ratio_day']):>14s}{ymd(l2['min_ratio_day']):>14s}",
        f"  {'margin calls':28s}{len(ma['calls']):>14d}{len(l2['calls']):>14d}"
        f"   (MA at {MAINTENANCE:.0%}, L2 at 25%)",
        "",
        "CRASH WINDOWS (total change)",
    ]
    for name, a, b in WINDOWS:
        lines.append(f"  {name:28s}{_pct(window(ma['equity'], a, b)):>14s}"
                     f"{_pct(window(l2['equity'], a, b)):>14s}")
    checks = [(f"no margin call at {MAINTENANCE:.0%} maintenance ({len(ma['calls'])})",
               not ma["calls"]),
              (f"deepest daily fall no deeper than L2's ({s_ma['dd']:.0%} vs {s_l2['dd']:.0%})",
               s_ma["dd"] >= s_l2["dd"])]
    ok = all(c for _, c in checks)
    lines += ["", "WHAT WOULD CHANGE THE DECISION (registered)"]
    lines += [f"  [{'OK' if c else 'CONCERN'}] {t}" for t, c in checks]
    lines += [f"  VERDICT: {'no concern: MA stays as chosen' if ok else 'CONCERN: report to the operator before real money'}",
              "=" * 78]
    return "\n".join(lines) + "\n", ok


def _yahoo_daily(symbol: str):
    from datetime import date

    from ..data.prices import YahooPriceSource

    pts = YahooPriceSource().get_prices(symbol, date(1990, 1, 1), date.today())
    return [(p.price_date, p.adjusted_close or p.close) for p in pts]


def run(get: Callable[[str], bytes], cache_dir: Path | None = None,
        daily=_yahoo_daily) -> tuple[str, bool]:
    assets, rf, notes = load(get, cache_dir, daily)
    return evaluate(assets, rf, notes)
