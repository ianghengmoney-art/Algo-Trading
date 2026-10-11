"""Paper trading of MA, the multi-asset trend portfolio, with a margin alarm.

docs/LEVERAGE_STRATEGY.md: four equal sleeves (US stocks, international
developed stocks, 10-year Treasuries, gold), each held while above its
10-month average and in T-bills otherwise, the whole levered 3.82x (L2's
volatility), re-levered at each month's signal. Run as futures, so:

    equity now = E0 x (1 + rf x t) + sum_i X_i x (P_i / P_i0 - 1 - rf x t)
                 - max(sum X - E0, 0) x spread x t - E0 x running x t

where X_i is sleeve i's exposure at the rebalance (L/4 of equity when in
trend) and t the years since. A futures broker calls when equity falls
below a share m of gross exposure. The alarm grades how much of the
distance from the starting ratio (1/L when all four are in) to m is used.

Live proxies: ^SP500TR, EFA, IEF and GLD (total-return closes where they
pay income). Nothing here places an order.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Sequence

from .leverage import SMA_MONTHS
from .leverage_paper import LEVELS, OK, month_end_closes
from .multi_asset import RUNNING, SPREAD, SWITCH

ASSETS = (("US stocks", "^SP500TR"), ("Intl stocks", "EFA"),
          ("Treasuries", "IEF"), ("Gold", "GLD"))


@dataclass
class MAState:
    started_on: date
    leverage: float
    value: float                       # equity at the last rebalance
    rebalanced_on: date
    signal_month: str | None = None
    #: sleeve -> exposure in dollars at the last rebalance (0 when out)
    exposure: dict[str, float] = field(default_factory=dict)
    #: sleeve -> price at the last rebalance
    price_at: dict[str, float] = field(default_factory=dict)
    in_trend: dict[str, bool] = field(default_factory=dict)
    log: list[dict] = field(default_factory=list)
    marks: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        raw = asdict(self)
        for key in ("started_on", "rebalanced_on"):
            raw[key] = raw[key].isoformat()
        return json.dumps(raw, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "MAState":
        raw = json.loads(text)
        for key in ("started_on", "rebalanced_on"):
            raw[key] = date.fromisoformat(raw[key])
        return cls(**raw)


def load(path: Path) -> MAState | None:
    return MAState.from_json(path.read_text()) if path.exists() else None


def save(state: MAState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.to_json())


Closes = dict[str, Sequence[tuple[date, float]]]


def latest(closes: Closes) -> tuple[date, dict[str, float]]:
    """The latest date every sleeve has a close for, and those closes."""
    common = None
    for series in closes.values():
        days = {d for d, _ in series}
        common = days if common is None else common & days
    day = max(common) if common else max(d for s in closes.values() for d, _ in s)
    out = {}
    for name, series in closes.items():
        before = [c for d, c in sorted(series) if d <= day]
        out[name] = before[-1]
    return day, out


def close_on(series: Sequence[tuple[date, float]], day: date) -> float | None:
    before = [c for d, c in sorted(series) if d <= day]
    return before[-1] if before else None


def refresh_prices_at(state: MAState, closes: Closes) -> None:
    """Re-read each sleeve's rebalance price from today's series. Yahoo's
    dividend-adjusted closes are rescaled after every payout, so a stored
    price would leave the dividend out; read on today's scale it is in."""
    for name, series in closes.items():
        price = close_on(series, state.rebalanced_on)
        if price:
            state.price_at[name] = price


def signals(closes: Closes, today: date) -> dict[str, tuple[bool, float, float]] | None:
    """sleeve -> (in trend?, last month-end close, 10-month average), from
    completed months; None until every sleeve has 10 of them."""
    out = {}
    for name, series in closes.items():
        months = month_end_closes(series, today)
        if len(months) < SMA_MONTHS:
            return None
        last = months[-1][2]
        sma = sum(c for _, _, c in months[-SMA_MONTHS:]) / SMA_MONTHS
        out[name] = (last > sma, last, sma)
    return out


def equity_now(state: MAState, prices: dict[str, float], rf: float, day: date) -> float:
    t = max((day - state.rebalanced_on).days, 0) / 365
    e0 = state.value
    gross = sum(state.exposure.values())
    value = e0 * (1 + rf * t)
    for name, x in state.exposure.items():
        if x and state.price_at.get(name):
            value += x * (prices[name] / state.price_at[name] - 1 - rf * t)
    value -= max(gross - e0, 0) * SPREAD * t + e0 * RUNNING * t
    return max(value, 0.0)


def gross_now(state: MAState, prices: dict[str, float]) -> float:
    return sum(x * prices[n] / state.price_at[n]
               for n, x in state.exposure.items() if x and state.price_at.get(n))


def step(state: MAState | None, closes: Closes, rf: float, today: date,
         leverage: float, capital: float) -> tuple[MAState, list[str]]:
    """Start, apply a new month's signals, or mark. Returns the state and the
    sleeve changes to act on (empty when nothing changed)."""
    day, prices = latest(closes)
    sig = signals(closes, today)
    if state is None:
        state = MAState(started_on=today, leverage=leverage, value=capital,
                        rebalanced_on=day)
    else:
        refresh_prices_at(state, closes)
    changes: list[str] = []
    months = month_end_closes(next(iter(closes.values())), today)
    month = months[-1][0] if months else None
    if sig is not None and month != state.signal_month:
        equity = equity_now(state, prices, rf, day)
        sleeve = state.leverage / len(closes)
        new_trend = {n: s[0] for n, s in sig.items()}
        for name, on in new_trend.items():
            was = state.in_trend.get(name)
            if was is None or was != on:
                if on or was:
                    equity -= sleeve * equity * SWITCH
                if was is None and not on:
                    changes.append(f"{name}: stay out (T-bills)")
                else:
                    changes.append(f"{name}: {'BUY (in trend)' if on else 'SELL to T-bills'}")
        state.value, state.rebalanced_on, state.signal_month = equity, day, month
        state.in_trend = new_trend
        state.exposure = {n: (sleeve * equity if on else 0.0) for n, on in new_trend.items()}
        state.price_at = dict(prices)
        state.log.append({
            "date": day.isoformat(), "signal_month": month, "value": round(equity, 2),
            "in_trend": new_trend, "changes": changes,
            "gross_leverage": round(sum(state.exposure.values()) / equity, 2) if equity else 0,
        })
    value = equity_now(state, prices, rf, day)
    state.marks.append({"date": day.isoformat(), "value": round(value, 2),
                        "prices": {k: round(v, 4) for k, v in prices.items()}})
    if len(state.marks) >= 2 and state.marks[-2]["date"] == state.marks[-1]["date"]:
        del state.marks[-2]
    return state, changes


@dataclass
class Margin:
    level: str
    ratio: float          # equity / gross exposure now
    ratio_start: float    # at the rebalance
    used: float           # share of the distance to the call used
    inject_per_10k: float
    sell_per_10k: float


def margin(state: MAState, prices: dict[str, float], rf: float, day: date,
           maintenance: float) -> Margin | None:
    gross = gross_now(state, prices)
    if gross <= 0:
        return None
    equity = equity_now(state, prices, rf, day)
    start_gross = sum(state.exposure.values())
    r0 = state.value / start_gross
    ratio = equity / gross
    used = max((r0 - ratio) / (r0 - maintenance), 0.0) if r0 > maintenance else 1.0
    grade = OK
    for threshold, name in LEVELS:
        if used >= threshold:
            grade = name
            break
    scale = 10_000 / state.value if state.value else 0.0
    lev = start_gross / state.value
    return Margin(grade, ratio, r0, used,
                  inject_per_10k=max(gross / lev - equity, 0.0) * scale,
                  sell_per_10k=max(gross - lev * equity, 0.0) * scale)


def report(state: MAState, closes: Closes, rf: float, today: date, maintenance: float,
           changes: list[str]) -> tuple[str, str]:
    day, prices = latest(closes)
    refresh_prices_at(state, closes)
    sig = signals(closes, today) or {}
    value = equity_now(state, prices, rf, day)
    first = state.log[0]["value"] if state.log else state.value
    m = margin(state, prices, rf, day, maintenance)
    lines = [
        "=" * 78,
        f"MULTI-ASSET TREND (MA) — PAPER TRADING · {day.isoformat()} close",
        "=" * 78,
        f"{state.leverage:.2f}x total across 4 sleeves, each in its market while above its "
        "10-month average, else T-bills (docs/LEVERAGE_STRATEGY.md)",
        f"started {state.started_on.isoformat()} · paper value {value:,.0f} "
        f"({value / first - 1:+.2%} since the first signal)",
        "",
        f"SLEEVES (signal of {state.signal_month or 'n/a'}, set {state.rebalanced_on.isoformat()})",
    ]
    for name, symbol in ASSETS:
        s = sig.get(name)
        x = state.exposure.get(name, 0.0)
        move = (prices[name] / state.price_at[name] - 1) if state.price_at.get(name) else 0.0
        lines.append(
            f"  {name:12s} ({symbol:8s}) "
            + (f"{'IN ' if state.in_trend.get(name) else 'OUT'} · "
               f"month-end {s[1]:,.2f} vs average {s[2]:,.2f} ({s[1] / s[2] - 1:+.1%})"
               if s else "no signal yet")
            + f" · exposure {x:,.0f} · since rebalance {move:+.1%}")
    gross = gross_now(state, prices)
    lines.append(f"  gross exposure {gross:,.0f} = {gross / value:.2f}x of equity"
                 if value else "  gross exposure n/a")
    lines.append("  next signals: the last trading day of this month")
    if changes:
        lines.append("  CHANGES THIS MONTH: " + "; ".join(changes))
    lines += ["", f"MARGIN (futures; a call below {maintenance:.0%} of gross exposure; "
                  "set in scripts/leverage_config.json)"]
    if m is None:
        lines.append("  nothing held: no margin risk")
        level = OK
    else:
        lines += [f"  equity is {m.ratio:.0%} of exposure (started at {m.ratio_start:.0%}); "
                  f"{m.used:.0%} of the distance to a call used",
                  f"  status: {m.level}"]
        level = m.level
        if m.level != OK:
            lines += [f"  TO GET BACK TO {state.leverage:.2f}x, per $10,000 of equity at the "
                      "last rebalance:",
                      f"    either add ${m.inject_per_10k:,.0f} cash",
                      f"    or cut positions by ${m.sell_per_10k:,.0f} in total, spread across "
                      "the sleeves in proportion",
                      "  Cutting positions is usually the safer of the two."]
    lines += ["", "Simulated only. Nothing here places an order.", "=" * 78]
    text = "\n".join(lines) + "\n"
    alert = [level] + ([f"POSITION CHANGE: {'; '.join(changes)}"] if changes else [])
    return text, "\n".join(alert + ["", *lines[1:]]) + "\n"


# ------------------------------------------------------------- trade ticket


def trade_ticket(in_trend: dict[str, bool], amount: float, currency: str,
                 fx_per_usd: float, etf_prices: dict[str, float]) -> str:
    """What to hold in a small account, via the leveraged-ETF version
    (gcfp/backtest/etf_version.py): dollars and shares per ETF, the rest in
    a T-bill ETF. ``fx_per_usd`` is units of ``currency`` per US dollar."""
    from .etf_version import ETFS, targets

    usd = amount / fx_per_usd
    goal = targets(in_trend, usd)
    lines = ["", f"YOUR TRADE TICKET — {currency} {amount:,.0f} (about US${usd:,.0f}), "
                 "leveraged-ETF version",
             f"  {'sleeve':12s} {'ETF':6s} {'in trend':>9s} {'US$':>9s} {'shares':>9s} "
             f"{'whole':>6s}"]
    held = 0.0
    for name, (etf, k, _) in ETFS.items():
        dollars = goal.get(name, 0.0)
        price = etf_prices.get(etf)
        shares = dollars / price if price else 0.0
        held += dollars
        lines.append(f"  {name:12s} {etf:6s} {('yes' if in_trend.get(name) else 'no'):>9s} "
                     f"{dollars:>9,.0f} {shares:>9.2f} {int(shares):>6d}"
                     + (f"   (price {price:,.2f})" if price else "   (price n/a)"))
    gross = sum(goal.get(n, 0.0) * ETFS[n][1] for n in ETFS) / usd if usd else 0.0
    lines += [f"  {'rest':12s} {'SGOV':6s} {'':>9s} {usd - held:>9,.0f}   (US T-bill ETF)",
              f"  total exposure {gross:.2f}x of the account",
              "  Fractional shares need a broker that offers them (e.g. Interactive "
              "Brokers); otherwise use the whole-share column.",
              "  Trade only the lines whose 'in trend' changed this month, or that "
              "drifted more than 25% from the US$ amount: each order costs money."]
    return "\n".join(lines) + "\n"
