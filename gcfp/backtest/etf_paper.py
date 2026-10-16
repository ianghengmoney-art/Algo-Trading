"""Paper trading of MA's leveraged-ETF version for a small SGD account.

The operator's real account is about SGD 4,000, too small for futures
(docs/LEVERAGE_STRATEGY.md, "The ETF version for a small account"). This
book holds the real ETFs (UPRO, EFO, TYD, UGL, and SGOV for the T-bill part)
on MA's own monthly signals, sized by gcfp/backtest/etf_version.targets,
traded only when a signal changes or a holding drifts more than 25% from its
target, at US$1 an order. It is valued daily in US dollars and in SGD.

Each holding is kept as the dollars put in and the date, and valued with
dividend-adjusted closes re-read on today's scale, so distributions (SGOV's
monthly interest above all) count. Nothing here places an order.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Sequence

from .etf_version import DRIFT_BAND, ETFS, targets
from .multi_asset_paper import close_on

CASH_ETF = "SGOV"
ORDER_COST = 1.0


@dataclass
class ETFBook:
    started_on: date
    currency: str
    start_amount: float
    fx_start: float                      # currency units per US$ at the start
    #: ETF -> [US$ put in, ISO date put in]
    lots: dict[str, list] = field(default_factory=dict)
    in_trend: dict[str, bool] = field(default_factory=dict)
    signal_month: str | None = None
    orders: int = 0
    commissions: float = 0.0
    log: list[dict] = field(default_factory=list)
    marks: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        raw = asdict(self)
        raw["started_on"] = self.started_on.isoformat()
        return json.dumps(raw, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "ETFBook":
        raw = json.loads(text)
        raw["started_on"] = date.fromisoformat(raw["started_on"])
        return cls(**raw)


def load(path: Path) -> ETFBook | None:
    return ETFBook.from_json(path.read_text()) if path.exists() else None


def save(book: ETFBook, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(book.to_json())


Closes = dict[str, Sequence[tuple[date, float]]]


def value_of(book: ETFBook, closes: Closes, day: date) -> dict[str, float]:
    """US$ value of each lot at ``day``."""
    out = {}
    for etf, (dollars, since) in book.lots.items():
        p0 = close_on(closes[etf], date.fromisoformat(since))
        p1 = close_on(closes[etf], day)
        out[etf] = dollars * p1 / p0 if p0 and p1 else dollars
    return out


def step(book: ETFBook | None, closes: Closes, in_trend: dict[str, bool],
         signal_month: str | None, day: date, fx: float, currency: str,
         amount: float, order_cost: float = ORDER_COST) -> tuple[ETFBook, list[str]]:
    """Start the book in T-bills, then follow MA's monthly signals."""
    if book is None:
        book = ETFBook(started_on=day, currency=currency, start_amount=amount,
                       fx_start=fx, lots={CASH_ETF: [amount / fx, day.isoformat()]})
    trades: list[str] = []
    if signal_month and signal_month != book.signal_month and in_trend:
        values = value_of(book, closes, day)
        equity = sum(values.values())
        goal = targets(in_trend, equity)
        cash = values.get(CASH_ETF, 0.0)
        # Untraded holdings keep their original lot; traded ones restart today.
        new_lots = {etf: lot for etf, lot in book.lots.items() if etf != CASH_ETF}
        for sleeve, (etf, k, _) in ETFS.items():
            now = values.get(etf, 0.0)
            changed = in_trend.get(sleeve, False) != book.in_trend.get(sleeve, False)
            drifted = (in_trend.get(sleeve) and goal[sleeve] > 0
                       and abs(now - goal[sleeve]) > DRIFT_BAND * goal[sleeve])
            if not (changed or drifted):
                continue
            cash += now - goal[sleeve] - order_cost
            book.orders += 1
            book.commissions += order_cost
            if goal[sleeve] > 0:
                new_lots[etf] = [goal[sleeve], day.isoformat()]
            else:
                new_lots.pop(etf, None)
            verb = "BUY" if goal[sleeve] > now else "SELL"
            trades.append(f"{verb} {etf} to US${goal[sleeve]:,.0f} (was US${now:,.0f})")
        new_lots[CASH_ETF] = [cash, day.isoformat()]
        book.lots = new_lots
        book.in_trend = dict(in_trend)
        book.signal_month = signal_month
        book.log.append({"date": day.isoformat(), "signal_month": signal_month,
                         "equity_usd": round(equity, 2), "trades": trades})
    total = sum(value_of(book, closes, day).values())
    book.marks.append({"date": day.isoformat(), "usd": round(total, 2),
                       "local": round(total * fx, 2)})
    if len(book.marks) >= 2 and book.marks[-2]["date"] == book.marks[-1]["date"]:
        del book.marks[-2]
    return book, trades


def report(book: ETFBook, closes: Closes, day: date, fx: float, trades: list[str]) -> str:
    values = value_of(book, closes, day)
    total = sum(values.values())
    local = total * fx
    start_usd = book.start_amount / book.fx_start
    gross = sum(values.get(etf, 0.0) * k for etf, k, _ in ETFS.values()) / total if total else 0.0
    lines = [
        "",
        "=" * 78,
        f"ETF VERSION — PAPER ACCOUNT, {book.currency} {book.start_amount:,.0f} "
        f"(started {book.started_on.isoformat()})",
        "=" * 78,
        f"value {book.currency} {local:,.0f} ({local / book.start_amount - 1:+.2%}) · "
        f"US${total:,.0f} ({total / start_usd - 1:+.2%} in US$) · exposure {gross:.2f}x",
        f"orders {book.orders} · commissions US${book.commissions:,.0f}",
        "",
        f"  {'ETF':6s} {'sleeve':12s} {'US$':>9s} {'share':>7s} {'shares':>9s} {'price':>9s}",
    ]
    names = {etf: sleeve for sleeve, (etf, _, _) in ETFS.items()}
    names[CASH_ETF] = "T-bills"
    for etf, v in sorted(values.items(), key=lambda kv: -kv[1]):
        price = close_on(closes[etf], day)
        lines.append(f"  {etf:6s} {names.get(etf, ''):12s} {v:>9,.0f} {v / total:>7.0%} "
                     f"{(v / price if price else 0):>9.2f} {price or 0:>9,.2f}")
    if trades:
        lines += ["", "  TRADES THIS MONTH (copy these if trading for real):"]
        lines += [f"    {t}" for t in trades]
    lines += ["  Simulated only. Nothing here places an order.", "=" * 78]
    return "\n".join(lines) + "\n"


# --------------------------------------------------- futures contract ticket

#: sleeve -> (Yahoo symbol, US$ per point, label). International stocks use
#: EFA shares: the ICE mini MSCI EAFE future is about US$130,000 a contract.
CONTRACTS = {
    "US stocks": ("MES=F", 5.0, "Micro E-mini S&P 500 (MES)"),
    "Treasuries": ("ZN=F", 1000.0, "10-year T-note (ZN)"),
    "Gold": ("MGC=F", 10.0, "Micro gold (MGC)"),
    "Intl stocks": ("EFA", 1.0, "EFA shares (EAFE futures too large)"),
}


def contract_ticket(in_trend: dict[str, bool], equity: float, leverage: float,
                    prices: dict[str, float]) -> str:
    """Whole contracts (or shares) per sleeve for a futures account, and
    how far rounding moves each sleeve from its target."""
    sleeve = leverage / len(CONTRACTS)
    lines = ["", f"FUTURES CONTRACT TICKET — US${equity:,.0f} account at {leverage:.2f}x",
             f"  {'sleeve':12s} {'instrument':36s} {'target':>10s} {'qty':>6s} "
             f"{'actual':>10s} {'off by':>7s}"]
    for name, (symbol, mult, label) in CONTRACTS.items():
        target = sleeve * equity if in_trend.get(name) else 0.0
        price = prices.get(symbol)
        if not price:
            lines.append(f"  {name:12s} {label:36s} price unavailable")
            continue
        per = price * mult
        qty = round(target / per) if target else 0
        actual = qty * per
        off = (actual / target - 1) if target else 0.0
        lines.append(f"  {name:12s} {label:36s} {target:>10,.0f} {qty:>6d} "
                     f"{actual:>10,.0f} {off:>+7.0%}")
    lines.append("  Rounding to whole contracts moves each sleeve off its target; "
                 "smaller accounts drift further.")
    return "\n".join(lines) + "\n"
