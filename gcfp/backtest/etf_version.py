"""MA through leveraged ETFs, for an account too small for futures.

docs/LEVERAGE_STRATEGY.md, "The ETF version for a small account": MA's
monthly signals, held as daily-reset leveraged ETFs (UPRO, EFO, TYD, UGL),
capped at 100% of the capital because leveraged ETFs cannot be bought on
margin, traded only when a signal changes or a holding drifts more than 25%
from its target, at US$1 an order. An implementation check: it selects
nothing, and reports what the account could actually earn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .leverage import SMA_MONTHS
from .multi_asset_daily import (
    LEVERAGE, TRADING_DAYS, align, load, simulate as simulate_futures, stats, window,
    WINDOWS,
)

#: sleeve -> (ETF, leverage, expense ratio)
ETFS = {
    "US stocks": ("UPRO", 3.0, 0.0091),
    "Intl stocks": ("EFO", 2.0, 0.0095),
    "Treasuries": ("TYD", 3.0, 0.0095),
    "Gold": ("UGL", 2.0, 0.0095),
}
SWAP_SPREAD = 0.005   # leveraged ETFs' financing over T-bills, per year
DRIFT_BAND = 0.25
ORDER_COST = 1.0      # US$ per order
CAPITAL = 3100.0      # about SGD 4,000


def targets(in_trend: dict[str, bool], equity: float, leverage: float = LEVERAGE
            ) -> dict[str, float]:
    """Dollars to hold in each sleeve's ETF: L/4 exposure per in-trend sleeve,
    scaled down together if that needs more than all the capital."""
    want = {a: (leverage / len(ETFS)) / ETFS[a][1] if on else 0.0
            for a, on in in_trend.items()}
    total = sum(want.values())
    scale = min(1.0, 1.0 / total) if total > 0 else 1.0
    return {a: w * scale * equity for a, w in want.items()}


def simulate(assets: dict, rf: dict, start_after: int, capital: float = CAPITAL,
             order_cost: float = ORDER_COST) -> dict:
    calendar = sorted(rf)
    names = list(ETFS)
    aligned = {a: align(assets[a], calendar) for a in names}
    first_seen = {a: min(assets[a]) for a in names}
    level = {a: 1.0 for a in names}
    month_ends: dict[str, list[float]] = {a: [] for a in names}
    hold = {a: 0.0 for a in names}
    cash = capital
    in_trend = {a: False for a in names}
    started = False
    out = {"equity": {}, "orders": 0, "commissions": 0.0, "gross": []}
    for i, d in enumerate(calendar):
        f = rf[d]
        if started:
            for a in names:
                _, k, er = ETFS[a]
                u = aligned[a].get(d, 0.0)
                hold[a] *= 1 + k * u - (k - 1) * (f + SWAP_SPREAD / TRADING_DAYS) - er / TRADING_DAYS
                hold[a] = max(hold[a], 0.0)
            cash *= 1 + f
            out["equity"][d] = cash + sum(hold.values())
        for a in names:
            level[a] *= 1 + aligned[a].get(d, 0.0)
        if i + 1 < len(calendar) and calendar[i + 1] // 100 == d // 100:
            continue
        for a in names:
            if d >= first_seen[a]:
                month_ends[a].append(level[a])
        if not all(len(month_ends[a]) >= SMA_MONTHS for a in names):
            continue
        if not started:
            if d < start_after:
                continue
            started = True
            out["equity"][d] = cash
        new = {a: month_ends[a][-1] > sum(month_ends[a][-SMA_MONTHS:]) / SMA_MONTHS
               for a in names}
        equity = cash + sum(hold.values())
        goal = targets(new, equity)
        for a in names:
            changed = new[a] != in_trend[a]
            drifted = new[a] and goal[a] > 0 and abs(hold[a] - goal[a]) > DRIFT_BAND * goal[a]
            if changed or drifted:
                cash += hold[a] - goal[a]
                hold[a] = goal[a]
                cash -= order_cost
                out["orders"] += 1
                out["commissions"] += order_cost
        in_trend = new
        equity = cash + sum(hold.values())
        if equity > 0:
            out["gross"].append(sum(hold[a] * ETFS[a][1] for a in names) / equity)
    return out


def _pct(x) -> str:
    return f"{x:+.1%}" if x is not None else "n/a"


def evaluate(assets: dict, rf: dict, notes: list[str]) -> str:
    first_month = max(sorted({d // 100 for d in s})[SMA_MONTHS] for s in assets.values())
    start_after = first_month * 100
    etf = simulate(assets, rf, start_after)
    free = simulate(assets, rf, start_after, order_cost=0.0)
    fut = simulate_futures(assets, rf, LEVERAGE, start_after)
    first, last = min(etf["equity"]), max(etf["equity"])
    years = len([d for d in etf["equity"] if first <= d <= last]) / TRADING_DAYS
    rows = [("MA via futures, 3.82x", fut["equity"]),
            (f"ETF version, US${CAPITAL:,.0f}, $1 orders", etf["equity"]),
            ("ETF version, no commissions", free["equity"])]

    def ymd(d: int) -> str:
        return f"{d // 10000}-{d // 100 % 100:02d}-{d % 100:02d}"

    lines = [
        "=" * 78,
        "MA AS LEVERAGED ETFs — what a small account could actually earn",
        "=" * 78,
        *[f"Data: {n}" for n in notes],
        f"Period: {ymd(first)} to {ymd(last)}. ETFs: " + ", ".join(
            f"{a} {e} ({k:.0f}x)" for a, (e, k, _) in ETFS.items()),
        "",
        f"  {'':40s}{'per year':>10s}{'deepest':>10s}{'worst day':>11s}{'worst mo':>10s}",
    ]
    for name, eq in rows:
        s = stats(eq, first, last)
        lines.append(f"  {name:40s}{_pct(s['cagr']):>10s}{s['dd']:>10.0%}"
                     f"{s['worst_day']:>+11.1%}{s['worst_month']:>+10.1%}")
    gross = etf["gross"]
    end_value = etf["equity"][last]
    lines += [
        "",
        f"  US${CAPITAL:,.0f} grew to US${end_value:,.0f} in {years:.0f} years "
        f"(futures version, same start: US${CAPITAL * fut['equity'][last] / fut['equity'][first]:,.0f})",
        f"  orders: {etf['orders']} ({etf['orders'] / years:.1f} a year), commissions "
        f"US${etf['commissions']:,.0f} in all",
        f"  gross exposure after each month-end: average {sum(gross) / len(gross):.2f}x, "
        f"highest {max(gross):.2f}x (futures MA reaches 3.82x)",
        "",
        "CRASH WINDOWS (total change)",
        f"  {'':28s}{'futures':>12s}{'ETFs':>12s}",
    ]
    for name, a, b in WINDOWS:
        lines.append(f"  {name:28s}{_pct(window(fut['equity'], a, b)):>12s}"
                     f"{_pct(window(etf['equity'], a, b)):>12s}")
    lines += [
        "",
        "READING IT",
        "  The ETF version holds less when all four sleeves are in trend (capital",
        "  runs out), pays ETF fees, and resets leverage daily, which helps in",
        "  steady trends and hurts in choppy markets. It is what an account this",
        "  size can hold; the futures version needs about US$150,000 or more.",
        "=" * 78,
    ]
    return "\n".join(lines) + "\n"


def _yahoo_daily(symbol: str):
    from datetime import date

    from ..data.prices import YahooPriceSource

    pts = YahooPriceSource().get_prices(symbol, date(1990, 1, 1), date.today())
    return [(p.price_date, p.adjusted_close or p.close) for p in pts]


def run(get: Callable[[str], bytes], cache_dir: Path | None = None,
        daily=_yahoo_daily) -> str:
    assets, rf, notes = load(get, cache_dir, daily)
    return evaluate(assets, rf, notes)
