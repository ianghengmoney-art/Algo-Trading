"""MA, the multi-asset trend portfolio registered in docs/LEVERAGE_STRATEGY.md.

Four sleeves: US stocks, international developed stocks, 10-year US
Treasuries and gold. Each holds its asset while that asset is above its
10-month average and T-bills otherwise; the whole is levered L times.
Compared with L2 (US stocks alone at 2.0x) at the same futures costs over
the same months, at 2.0x and at the leverage that matches L2's volatility.

Monthly; series are ``{YYYYMM: return}``. The loaders take ``get(url) ->
bytes`` so the data can be fetched where the network allows it (GitHub
Actions) and the logic tested anywhere.
"""

from __future__ import annotations

import csv
import io
import json
import math
import statistics
from pathlib import Path
from typing import Callable

from .leverage import SMA_MONTHS, load_market
from .longhistory import fetch_french, parse_french_csv

Series = dict[int, float]

INTL_FILE = "Developed_ex_US_3_Factors"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"
GOLD_URL = "https://datahub.io/core/gold-prices/r/monthly.csv"
YAHOO_GOLD_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
                  "?interval=1mo&range=max")

#: docs/LEVERAGE_STRATEGY.md, MA: futures costs.
SPREAD = 0.003           # per year, on the borrowed part
RUNNING = 0.0005         # per year
SWITCH = 0.0005          # of the sleeve's levered size, per switch
L2_LEVERAGE = 2.0
POST_PUBLICATION = 200801
CRASHES = (("2000-02 dot-com", 200004, 200209), ("2008 crisis", 200711, 200902),
           ("2020 Covid", 202002, 202003), ("2022 bear", 202201, 202209))


# ----------------------------------------------------------------------- data


def bond_return(y0: float, y1: float, months: int = 1) -> float:
    """One period's total return on a 10-year par bond bought at yield ``y0``
    (decimal) and repriced at ``y1`` ``months`` later: semiannual coupons,
    accrued interest included."""
    coupon = y0 / 2 * 100
    t0 = months / 12
    price = 0.0
    for k in range(1, 21):
        t = k / 2 - t0
        if t <= 0:
            price += coupon  # paid during the period (never for one month)
            continue
        flow = coupon + (100 if k == 20 else 0)
        price += flow / (1 + y1 / 2) ** (2 * t)
    return price / 100 - 1


def treasury_returns(csv_text: str) -> Series:
    """Monthly 10-year Treasury returns from FRED's daily DGS10 CSV."""
    month_end: dict[int, float] = {}
    for row in csv.reader(io.StringIO(csv_text)):
        if len(row) < 2 or not row[0][:4].isdigit():
            continue
        try:
            y = float(row[1])
        except ValueError:
            continue  # "." marks a holiday
        month_end[int(row[0][:4] + row[0][5:7])] = y / 100
    months = sorted(month_end)
    return {b: bond_return(month_end[a], month_end[b])
            for a, b in zip(months, months[1:]) if _next_month(a) == b}


def _next_month(m: int) -> int:
    return m + 89 if m % 100 == 12 else m + 1


def gold_returns(get: Callable[[str], bytes]) -> tuple[Series, str]:
    """Monthly gold returns: datahub's monthly price series, extended or
    replaced by Yahoo's COMEX gold future where datahub stops or fails."""
    prices: dict[int, float] = {}
    source = []
    try:
        for row in csv.DictReader(io.StringIO(get(GOLD_URL).decode())):
            prices[int(row["Date"][:4] + row["Date"][5:7])] = float(row["Price"])
        source.append(f"datahub {min(prices)}-{max(prices)}")
    except Exception as exc:
        source.append(f"datahub unavailable ({type(exc).__name__})")
    try:
        payload = json.loads(get(YAHOO_GOLD_URL).decode())
        result = payload["chart"]["result"][0]
        from datetime import datetime, timezone

        yahoo = {}
        for stamp, close in zip(result["timestamp"],
                                result["indicators"]["quote"][0]["close"]):
            if close:
                d = datetime.fromtimestamp(stamp, tz=timezone.utc)
                yahoo[d.year * 100 + d.month] = float(close)
        last = max(prices) if prices else 0
        added = {m: p for m, p in yahoo.items() if m > last}
        # Chain Yahoo's later months onto the datahub level at the join.
        if prices and added and last in yahoo:
            scale = prices[last] / yahoo[last]
            prices.update({m: p * scale for m, p in added.items()})
        elif not prices:
            prices = yahoo
        source.append(f"Yahoo GC=F to {max(yahoo)}")
    except Exception as exc:
        source.append(f"Yahoo unavailable ({type(exc).__name__})")
    months = sorted(prices)
    out = {b: prices[b] / prices[a] - 1 for a, b in zip(months, months[1:])
           if _next_month(a) == b and prices[a] > 0}
    return out, "; ".join(source)


def load_assets(get: Callable[[str], bytes], cache_dir: Path | None = None
                ) -> tuple[dict[str, Series], Series, list[str]]:
    """({asset: monthly total return}, T-bill, notes on the sources)."""
    us, rf = load_market(get, cache_dir)
    notes = [f"US stocks: French, {min(us)}-{max(us)}"]
    assets = {"US stocks": us}
    intl = parse_french_csv(fetch_french(INTL_FILE, get, cache_dir))
    intl = {k.strip(): v for k, v in intl.items()}
    assets["Intl stocks"] = {m: intl["Mkt-RF"][m] + intl["RF"][m]
                             for m in intl["Mkt-RF"] if m in intl["RF"]}
    notes.append(f"Intl stocks: French Developed ex US, {min(assets['Intl stocks'])}-"
                 f"{max(assets['Intl stocks'])}")
    assets["Treasuries"] = treasury_returns(get(FRED_URL).decode())
    notes.append(f"Treasuries: built from FRED DGS10, {min(assets['Treasuries'])}-"
                 f"{max(assets['Treasuries'])}")
    gold, gold_note = gold_returns(get)
    if gold:
        assets["Gold"] = gold
    notes.append(f"Gold: {gold_note}")
    return assets, rf, notes


# ----------------------------------------------------------------- simulation


def signals(asset: Series) -> dict[int, bool]:
    """For each month, whether the asset is held in it (set by the month
    before's close against its 10-month average)."""
    months = sorted(asset)
    level, levels, held = 1.0, [], {}
    for i, m in enumerate(months[:-1]):
        level *= 1 + asset[m]
        levels.append(level)
        if len(levels) >= SMA_MONTHS:
            held[months[i + 1]] = level > sum(levels[-SMA_MONTHS:]) / SMA_MONTHS
    return held


def portfolio(assets: dict[str, Series], rf: Series, leverage: float,
              months: list[int]) -> Series:
    """Monthly returns of the levered equal-sleeve trend portfolio."""
    held = {a: signals(s) for a, s in assets.items()}
    prev: dict[str, bool] = {}
    out: Series = {}
    for m in months:
        live = [a for a in assets if m in held[a] and m in assets[a]]
        if not live:
            out[m] = rf[m]
            continue
        sleeve = leverage / len(live)
        exposure = sum(sleeve for a in live if held[a][m])
        excess = sum(sleeve * (assets[a][m] - rf[m]) for a in live if held[a][m])
        r = rf[m] + excess - max(exposure - 1, 0) * SPREAD / 12 - RUNNING / 12
        switches = sum(1 for a in live if a in prev and prev[a] != held[a][m])
        r -= switches * sleeve * SWITCH
        out[m] = max(r, -1.0)
        prev = {a: held[a][m] for a in live}
    return out


# ------------------------------------------------------------------ statistics


def stats(r: Series, a: int, b: int) -> dict:
    ms = [m for m in sorted(r) if a <= m <= b]
    v, peak, dd, worst = 1.0, 1.0, 0.0, 0.0
    for m in ms:
        v *= 1 + r[m]
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
        worst = min(worst, r[m])
    years = len(ms) / 12
    return {
        "cagr": v ** (1 / years) - 1 if v > 0 and years > 0 else -1.0,
        "vol": statistics.pstdev([r[m] for m in ms]) * math.sqrt(12) if len(ms) > 1 else 0.0,
        "dd": dd, "worst": worst,
    }


def window(r: Series, a: int, b: int) -> float:
    v = 1.0
    for m in sorted(r):
        if a <= m <= b:
            v *= 1 + r[m]
    return v - 1


def vol_matched_leverage(assets: dict[str, Series], rf: Series, months: list[int],
                         target_vol: float) -> float:
    lo, hi = 0.25, 8.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if stats(portfolio(assets, rf, mid, months), months[0], months[-1])["vol"] < target_vol:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _pct(x: float) -> str:
    return f"{x:+.1%}"


def evaluate(assets: dict[str, Series], rf: Series, notes: list[str]) -> tuple[str, bool]:
    held = {a: signals(s) for a, s in assets.items()}
    # Common period: every asset has a signal (gold may start later; the
    # registered rule uses the assets with data, and the start is when the
    # international series first has one, as stated).
    first = min(held["Intl stocks"])
    # Gold, if its sources stop early, simply drops out of later months.
    last = min(max(assets[a]) for a in ("US stocks", "Intl stocks", "Treasuries"))
    last = min(last, max(rf))
    months = [m for m in sorted(rf) if first <= m <= last]
    mid = months[len(months) // 2]
    periods = [("full", months[0], last), ("1st half", months[0], months[len(months) // 2 - 1]),
               ("2nd half", mid, last), ("since 2008", POST_PUBLICATION, last)]

    l2 = portfolio({"US stocks": assets["US stocks"]}, rf, L2_LEVERAGE, months)
    target = stats(l2, months[0], last)["vol"]
    lev = vol_matched_leverage(assets, rf, months, target)
    rows = [
        (f"L2: US stocks only, {L2_LEVERAGE:.1f}x", l2),
        (f"MA at {L2_LEVERAGE:.1f}x", portfolio(assets, rf, L2_LEVERAGE, months)),
        (f"MA at {lev:.2f}x (same volatility as L2)", portfolio(assets, rf, lev, months)),
        ("MA unlevered (1.0x)", portfolio(assets, rf, 1.0, months)),
        ("US stocks, buy and hold", {m: assets["US stocks"][m] for m in months}),
    ]
    for a in assets:
        rows.append((f"  sleeve: {a} trend, 1.0x", portfolio({a: assets[a]}, rf, 1.0, months)))

    lines = [
        "=" * 78,
        "MA — MULTI-ASSET TREND vs L2 (monthly, futures costs) — docs/LEVERAGE_STRATEGY.md",
        "=" * 78,
        *[f"Data: {n}" for n in notes],
        f"Common period: {months[0] // 100}-{months[0] % 100:02d} to "
        f"{last // 100}-{last % 100:02d} ({len(months)} months).",
        "",
        "COMPOUND RETURN PER YEAR (deepest fall); then full-period volatility and worst month",
        f"  {'':42s}" + "".join(f"{p[0]:>16s}" for p in periods),
    ]
    for name, r in rows:
        cells = []
        for _, a, b in periods:
            s = stats(r, a, b)
            cells.append(f"{_pct(s['cagr'])} ({s['dd']:.0%})".rjust(16))
        full = stats(r, months[0], last)
        lines.append(f"  {name[:42]:42s}" + "".join(cells)
                     + f"   vol {full['vol']:.0%} · worst month {full['worst']:+.0%}")

    lines += ["", "THE CRASHES (return over each window)",
              f"  {'':18s}" + "".join(f"{n[:22]:>24s}" for n, _ in rows[:4])]
    for name, a, b in CRASHES:
        lines.append(f"  {name:18s}" + "".join(f"{_pct(window(r, a, b)):>24s}"
                                                for _, r in rows[:4]))

    # Correlations of the trend sleeves, the source of any gain.
    sleeves = {a: portfolio({a: assets[a]}, rf, 1.0, months) for a in assets}
    names = list(sleeves)
    lines += ["", "CORRELATION OF THE TREND SLEEVES (monthly)",
              "  " + " " * 14 + "".join(f"{n[:12]:>13s}" for n in names)]
    for x in names:
        row = []
        for y in names:
            xs = [sleeves[x][m] for m in months]
            ys = [sleeves[y][m] for m in months]
            try:
                row.append(f"{statistics.correlation(xs, ys):>13.2f}")
            except statistics.StatisticsError:
                row.append(f"{'n/a':>13s}")
        lines.append(f"  {x[:14]:14s}" + "".join(row))

    ma = rows[2][1]
    checks = []
    for label, a, b in periods[:3]:
        x, y = stats(ma, a, b)["cagr"], stats(l2, a, b)["cagr"]
        checks.append((f"higher CAGR than L2, {label} ({_pct(x)} vs {_pct(y)})", x > y))
    s_ma, s_l2 = stats(ma, months[0], last), stats(l2, months[0], last)
    checks.append((f"deepest fall no deeper than L2's ({s_ma['dd']:.0%} vs {s_l2['dd']:.0%})",
                   s_ma["dd"] >= s_l2["dd"]))
    checks.append((f"worst month no worse than L2's ({s_ma['worst']:+.0%} vs "
                   f"{s_l2['worst']:+.0%})", s_ma["worst"] >= s_l2["worst"]))
    passed = all(ok for _, ok in checks)
    lines += ["", f"REGISTERED CRITERION — MA at {lev:.2f}x (volatility-matched) vs L2"]
    lines += [f"  [{'PASS' if ok else 'FAIL'}] {t}" for t, ok in checks]
    lines += [f"  VERDICT: {'MA REPLACES L2' if passed else 'L2 STAYS'}",
              f"  ({lev:.2f}x is fitted in-sample to match L2's volatility; stated, not hidden.)",
              "=" * 78]
    return "\n".join(lines) + "\n", passed


def run(get: Callable[[str], bytes], cache_dir: Path | None = None) -> tuple[str, bool]:
    assets, rf, notes = load_assets(get, cache_dir)
    return evaluate(assets, rf, notes)
