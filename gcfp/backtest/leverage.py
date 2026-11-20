"""The leveraged trend strategy pre-registered in docs/LEVERAGE_STRATEGY.md.

Monthly, on Kenneth French's US market return from 1926: hold L times the
market while its total-return index is above its 10-month average, T-bills
otherwise. Pure Python; the series are ``{YYYYMM: return}`` dicts, as in
longhistory.py.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .longhistory import Series, fetch_french, parse_french_csv

MARKET_FILE = "F-F_Research_Data_Factors"

#: docs/LEVERAGE_STRATEGY.md, "Rules".
SMA_MONTHS = 10
BORROW_SPREAD = 0.010      # per year, over the T-bill rate, on the borrowed part
RUNNING_COST = 0.003       # per year while risk-on
SWITCH_COST = 0.002        # of the portfolio, per switch in or out
VARIANTS = (("L1", 1.5), ("L2", 2.0))
#: Pass criterion 4: the S&P 500's 2007-09 fall.
MAX_DRAWDOWN_LIMIT = -0.55
MIN_EXCESS = 0.01
POST_PUBLICATION = 200801
GOAL = (0.15, 0.20)
#: Maintenance margin assumed when flagging months a broker would have
#: called the loan (not part of the rules; reported).
MAINTENANCE_MARGIN = 0.25


@dataclass
class Path_:
    """One strategy's monthly returns, and when it was risk-on."""

    name: str
    returns: dict[int, float]
    risk_on: dict[int, bool] = field(default_factory=dict)
    switches: int = 0


def load_market(get: Callable[[str], bytes], cache_dir: Path | None = None
                ) -> tuple[Series, Series]:
    """(market total return, T-bill) monthly, from French's 3-factor file."""
    table = parse_french_csv(fetch_french(MARKET_FILE, get, cache_dir))
    table = {k.strip(): v for k, v in table.items()}
    mkt_rf, rf = table["Mkt-RF"], table["RF"]
    months = sorted(set(mkt_rf) & set(rf))
    return {m: mkt_rf[m] + rf[m] for m in months}, {m: rf[m] for m in months}


def _levered(r: float, rf: float, lev: float) -> float:
    """One month of ``lev`` times the market, financed at T-bills plus the
    spread, with the running cost; a loss beyond everything is everything."""
    out = lev * r - (lev - 1) * (rf + BORROW_SPREAD / 12) - RUNNING_COST / 12
    return max(out, -1.0)


def trend(market: Series, rf: Series, lev: float, name: str) -> Path_:
    """The registered rule: the signal at the end of month t sets month t+1.

    Months before the first signal are not traded. A switch between risk-on
    and risk-off (the first entry included) costs SWITCH_COST in the month
    it takes effect.
    """
    months = sorted(market)
    level, levels = 1.0, []
    held: dict[int, bool] = {}
    for i, m in enumerate(months[:-1]):
        level *= 1 + market[m]
        levels.append(level)
        if len(levels) >= SMA_MONTHS:
            held[months[i + 1]] = level > sum(levels[-SMA_MONTHS:]) / SMA_MONTHS
    path = Path_(name, {}, risk_on=held)
    previous = False  # in T-bills before the first signal
    for m in months:
        if m not in held:
            continue
        on = held[m]
        r = _levered(market[m], rf[m], lev) if on else rf[m]
        if on != previous:
            path.switches += 1
            r = (1 + r) * (1 - SWITCH_COST) - 1
        path.returns[m] = r
        previous = on
    return path


def buy_and_hold(market: Series, rf: Series, lev: float, name: str,
                 months: list[int]) -> Path_:
    if lev == 1.0:
        return Path_(name, {m: market[m] for m in months})
    return Path_(name, {m: _levered(market[m], rf[m], lev) for m in months})


# ------------------------------------------------------------------ statistics


@dataclass
class Stats:
    cagr: float | None
    vol: float
    max_dd: float
    dd_from: int
    dd_to: int
    worst_month: float
    months: int


def stats(returns: dict[int, float], start: int, end: int) -> Stats:
    months = [m for m in sorted(returns) if start <= m <= end]
    if not months:
        return Stats(None, 0.0, 0.0, 0, 0, 0.0, 0)
    value, peak, peak_m = 1.0, 1.0, months[0]
    worst, w_from, w_to = 0.0, months[0], months[0]
    rs = []
    for m in months:
        r = returns[m]
        rs.append(r)
        value *= 1 + r
        if value > peak:
            peak, peak_m = value, m
        dd = value / peak - 1 if peak > 0 else -1.0
        if dd < worst:
            worst, w_from, w_to = dd, peak_m, m
    years = len(months) / 12
    cagr = value ** (1 / years) - 1 if value > 0 else -1.0
    vol = statistics.pstdev(rs) * math.sqrt(12) if len(rs) > 1 else 0.0
    return Stats(cagr, vol, worst, w_from, w_to, min(rs), len(months))


def _ym(m: int) -> str:
    return f"{m // 100}-{m % 100:02d}"


def _pct(x: float | None) -> str:
    return f"{x:+.2%}" if x is not None else "n/a"


# ---------------------------------------------------------------------- report


def evaluate(market: Series, rf: Series) -> tuple[str, dict[str, bool]]:
    variants = {name: trend(market, rf, lev, f"{name}: trend, {lev:.1f}x while risk-on")
                for name, lev in VARIANTS}
    first = min(variants["L1"].returns)
    months = [m for m in sorted(market) if m >= first]
    last = months[-1]
    mid = months[len(months) // 2]
    context = [
        buy_and_hold(market, rf, 1.0, "market, buy and hold (1x)", months),
        trend(market, rf, 1.0, "trend, 1.0x (no leverage)"),
        buy_and_hold(market, rf, 1.5, "buy and hold 1.5x, no filter", months),
        buy_and_hold(market, rf, 2.0, "buy and hold 2.0x, no filter", months),
    ]
    bench = context[0]
    periods = [
        ("full period", first, last),
        ("earlier half", first, months[len(months) // 2 - 1]),
        ("later half", mid, last),
        ("since 1990", 199001, last),
        ("since 2008", POST_PUBLICATION, last),
    ]

    lines = [
        "=" * 78,
        "LEVERAGED TREND STRATEGY — docs/LEVERAGE_STRATEGY.md (2 variants registered)",
        "=" * 78,
        f"Data: Kenneth French US market (CRSP, dividends included), T-bills; "
        f"{_ym(first)} to {_ym(last)} ({len(months)} months).",
        f"Rule: {SMA_MONTHS}-month average; borrowing at T-bills + {BORROW_SPREAD:.1%}; "
        f"{RUNNING_COST:.1%}/yr while risk-on; {SWITCH_COST:.1%} per switch.",
        "",
        "COMPOUND RETURN PER YEAR (max drawdown in brackets)",
    ]
    rows = [*variants.values(), *context]
    header = f"  {'':38s}" + "".join(f"{p[0]:>18s}" for p in periods)
    lines.append(header)
    for path in rows:
        cells = []
        for _, a, b in periods:
            s = stats(path.returns, a, b)
            cells.append(f"{_pct(s.cagr)} ({s.max_dd:.0%})".rjust(18))
        lines.append(f"  {path.name[:38]:38s}" + "".join(cells))

    lines += ["", "GROWTH OF $10,000 OVER THE FULL PERIOD"]
    for path in rows:
        s = stats(path.returns, first, last)
        end_value = 10_000 * (1 + s.cagr) ** (s.months / 12) if s.cagr is not None else 0
        lines.append(f"  {path.name[:38]:38s} ${end_value:,.0f} · volatility {s.vol:.0%}/yr"
                     f" · worst month {s.worst_month:+.0%} · deepest fall "
                     f"{s.max_dd:.0%} ({_ym(s.dd_from)} to {_ym(s.dd_to)})")

    verdicts: dict[str, bool] = {}
    for name, lev in VARIANTS:
        path = variants[name]
        on = [m for m, v in path.risk_on.items() if v]
        share = len(on) / max(len(path.risk_on), 1)
        # Debt is (1 - 1/L) of the assets at the start of the month; equity
        # falls below the maintenance share once assets fall by more than this.
        call_at = 1 - (1 - 1 / lev) / (1 - MAINTENANCE_MARGIN) if lev > 1 else None
        calls = [m for m in on if call_at is not None and market[m] <= -call_at]
        full_s, full_b = stats(path.returns, first, last), stats(bench.returns, first, last)

        def beats(a, b, margin=0.0):
            s, x = stats(path.returns, a, b), stats(bench.returns, a, b)
            return (s.cagr is not None and x.cagr is not None and s.cagr - x.cagr > margin,
                    s.cagr, x.cagr)

        c1 = beats(first, last, MIN_EXCESS)
        c2a = beats(*periods[1][1:])
        c2b = beats(*periods[2][1:])
        c3 = beats(POST_PUBLICATION, last)
        c4 = full_s.max_dd >= MAX_DRAWDOWN_LIMIT
        checks = [
            (f"beats buy-and-hold by {MIN_EXCESS:.0%}/yr over the full period "
             f"({_pct(c1[1])} vs {_pct(c1[2])})", c1[0]),
            (f"beats it in the earlier half ({_pct(c2a[1])} vs {_pct(c2a[2])})", c2a[0]),
            (f"beats it in the later half ({_pct(c2b[1])} vs {_pct(c2b[2])})", c2b[0]),
            (f"beats it since 2008, after publication ({_pct(c3[1])} vs {_pct(c3[2])})", c3[0]),
            (f"max drawdown no deeper than {MAX_DRAWDOWN_LIMIT:.0%} "
             f"({full_s.max_dd:.0%}, {_ym(full_s.dd_from)} to {_ym(full_s.dd_to)})", c4),
        ]
        passed = all(ok for _, ok in checks)
        verdicts[name] = passed
        lines += ["", f"PRE-REGISTERED CRITERIA — {path.name}"]
        lines += [f"  [{'PASS' if ok else 'FAIL'}] {text}" for text, ok in checks]
        lines.append(f"  risk-on {share:.0%} of months · {path.switches} switches · "
                     f"about {path.switches / (len(months) / 12):.1f} a year")
        if call_at is not None:
            lines.append(
                f"  margin check (not a rule): a {call_at:.0%} market fall in one month "
                f"would breach {MAINTENANCE_MARGIN:.0%} maintenance margin; risk-on "
                f"months that fell that far: {len(calls)}"
                + (f" ({', '.join(_ym(m) for m in calls)})" if calls else ""))
        goal = []
        for label, a, b in (periods[0], periods[3], periods[4]):
            s = stats(path.returns, a, b)
            hit = s.cagr is not None and s.cagr >= GOAL[0]
            goal.append(f"{label}: {_pct(s.cagr)} {'MET' if hit else 'not met'}")
        lines.append(f"  GOAL {GOAL[0]:.0%}-{GOAL[1]:.0%}/yr — " + " · ".join(goal))
        lines.append(f"  VERDICT: {'PASSED' if passed else 'FAILED'}")

    passing = [name for name, _ in VARIANTS if verdicts[name]]
    chosen = passing[-1] if passing else None
    lines += [
        "",
        "SELECTION (registered: the higher leverage among those passing all four)",
        f"  {'selected: ' + chosen + ' — goes to paper trading' if chosen else 'none passes — an unleveraged index fund remains the recommendation'}",
        "",
        "NOTES",
        "  Monthly data: falls inside a month (Oct 1987, Mar 2020) hit a risk-on",
        "  holder in full, and are counted. Taxes are not modelled.",
        "=" * 78,
    ]
    return "\n".join(lines) + "\n", verdicts


def run(get: Callable[[str], bytes], cache_dir: Path | None = None) -> tuple[str, dict]:
    market, rf = load_market(get, cache_dir)
    return evaluate(market, rf)
