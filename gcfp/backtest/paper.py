"""Paper trading (§13.11): the backtest's own monthly step, run on today.

A backtest catches strategy flaws; paper trading catches pipeline flaws —
stale data, a feed that changed format, a price that never arrives. The two
are different failure classes, so a clean backtest does not substitute.

Paper trading here is deliberately *not* a second implementation. Each month
it calls the same ``Backtester._rebalance`` the backtest walks with, on live
data, and carries the book from one month to the next in a JSON file. If
paper trading and the backtest disagree, the cause is the data, which is
exactly what §13.11 is for.

Between rebalances the book is marked to market (weekly, with the screen),
so the report always shows where it stands. Nothing here places an order.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from pathlib import Path
from typing import Any

from ..classification import Classification
from .engine import Backtester, BacktestResult, RebalanceRecord
from .metrics import picks_vs_index
from .portfolio import (
    BacktestBook,
    BacktestPosition,
    ClosedPosition,
    SimulatedFill,
    Snapshot,
)

FORMAT_VERSION = 1


def _encode(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Classification):
        return value.value
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value


def _decode(cls, raw: dict) -> Any:
    kwargs = {}
    for f in fields(cls):
        if f.name not in raw:
            continue
        value = raw[f.name]
        kind = str(f.type)
        if value is not None and "date" in kind:
            value = date.fromisoformat(value)
        elif value is not None and "Classification" in kind:
            value = Classification(value)
        kwargs[f.name] = value
    return cls(**kwargs)


def book_to_dict(book: BacktestBook) -> dict:
    return {
        "cash": book.cash,
        "positions": {s: _encode(asdict(p)) for s, p in book.positions.items()},
        "closed": [_encode(asdict(c)) for c in book.closed],
        "fills": [_encode(asdict(f)) for f in book.fills],
        "snapshots": [_encode(asdict(s)) for s in book.snapshots],
        "drawdowns": dict(book._drawdowns),
    }


def book_from_dict(raw: dict) -> BacktestBook:
    book = BacktestBook(cash=raw["cash"])
    book.positions = {
        s: _decode(BacktestPosition, p) for s, p in raw.get("positions", {}).items()
    }
    book.closed = [_decode(ClosedPosition, c) for c in raw.get("closed", [])]
    book.fills = [_decode(SimulatedFill, f) for f in raw.get("fills", [])]
    book.snapshots = [_decode(Snapshot, s) for s in raw.get("snapshots", [])]
    book._drawdowns = dict(raw.get("drawdowns", {}))
    return book


@dataclass
class PaperState:
    """Everything carried from one run to the next."""

    started_on: date
    #: Fixed when paper trading starts, so every month screens the same
    #: companies and the months are comparable.
    symbols: list[str]
    book: BacktestBook
    last_rebalance: date | None = None
    #: Index level at the last ballast update; see ``Backtester._grow_ballast``.
    ballast_level: float | None = None
    #: One entry per rebalance: date, evaluated, BUY signals, buys, sells.
    log: list[dict] = field(default_factory=list)
    #: The index the spare money follows, fixed at the start: levels of two
    #: different indices cannot be chained.
    benchmark_symbol: str = "^GSPC"

    def due(self, today: date) -> bool:
        """Monthly, like the backtest: once in each calendar month."""
        last = self.last_rebalance
        return last is None or (last.year, last.month) != (today.year, today.month)

    def to_json(self) -> str:
        return json.dumps({
            "format": FORMAT_VERSION,
            "started_on": self.started_on.isoformat(),
            "symbols": self.symbols,
            "last_rebalance": self.last_rebalance.isoformat() if self.last_rebalance else None,
            "ballast_level": self.ballast_level,
            "benchmark_symbol": self.benchmark_symbol,
            "log": self.log,
            "book": book_to_dict(self.book),
        }, indent=1)

    @classmethod
    def from_json(cls, text: str) -> "PaperState":
        raw = json.loads(text)
        if raw.get("format") != FORMAT_VERSION:
            raise ValueError(f"unknown paper book format {raw.get('format')!r}")
        last = raw.get("last_rebalance")
        return cls(
            started_on=date.fromisoformat(raw["started_on"]),
            symbols=list(raw["symbols"]),
            book=book_from_dict(raw["book"]),
            last_rebalance=date.fromisoformat(last) if last else None,
            ballast_level=raw.get("ballast_level"),
            log=list(raw.get("log", [])),
            benchmark_symbol=raw.get("benchmark_symbol", "^GSPC"),
        )


def load_state(path: Path) -> PaperState | None:
    path = Path(path)
    return PaperState.from_json(path.read_text()) if path.exists() else None


def save_state(state: PaperState, path: Path) -> None:
    """Written to a temporary file first, so an interrupted run can never
    leave half a book behind."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(state.to_json())
    tmp.replace(path)


def rebalance(
    backtester: Backtester, state: PaperState, today: date
) -> RebalanceRecord:
    """One month of the backtest, on today's data."""
    result = BacktestResult(
        label="paper", settings=backtester.settings, split=None,
        book=state.book, config_fingerprint=backtester.config.fingerprint,
    )
    backtester._ballast_level = state.ballast_level
    fills_before = len(state.book.fills)
    value_before = state.book.snapshots[-1].total_value if state.book.snapshots else None
    record = backtester._rebalance(state.book, today, result)
    state.ballast_level = backtester._ballast_level
    state.last_rebalance = today
    total = state.book.snapshots[-1].total_value if state.book.snapshots else value_before
    trades = [
        {
            "side": f.side, "symbol": f.symbol, "shares": round(f.shares, 4),
            "price": round(f.price, 4),
            "share_of_portfolio": round(f.value / total, 4) if total else None,
            "reason": f.reason,
        }
        for f in state.book.fills[fills_before:]
    ]
    state.log.append({
        "crashes": dict(getattr(backtester, "crashes", {})),
        "trades": trades,
        "date": today.isoformat(),
        "evaluated": record.evaluated,
        "buy_signals": record.passers,
        "buys": list(record.buys),
        "sells": [list(s) for s in record.sells],
        "single_anchor": record.single_anchor_candidates,
        "dual_anchor": record.dual_anchor_candidates,
    })
    return record


def mark_to_market(backtester: Backtester, state: PaperState, today: date) -> None:
    """Between rebalances: move the ballast with the index and price the
    holdings, without screening or trading."""
    backtester._ballast_level = state.ballast_level
    if backtester.settings.ballast_in_index:
        backtester._grow_ballast(state.book, today)
    state.ballast_level = backtester._ballast_level
    backtester.credit_dividends(state.book, today)
    prices = backtester._prices_on(list(state.book.positions), today)
    state.book.mark(prices)
    benchmark = backtester._price_on(backtester.settings.benchmark_symbol, today)
    state.book.snapshot(today, prices, benchmark)


#: docs/FACTOR_STRATEGY.md, stage 2.
STAGE_TWO_REBALANCES = 3
STAGE_TWO_MAX_BEHIND = 0.15


def stage_two_status(state: PaperState) -> list[str]:
    """The registered paper-trading conditions, checked every week."""
    done = len(state.log)
    crashes = sum(sum((e.get("crashes") or {}).values()) for e in state.log)
    unpriced = [s for s, p in state.book.positions.items() if p.missed_marks > 1]
    snaps = state.book.snapshots
    behind = None
    if snaps and snaps[0].benchmark_level and snaps[-1].benchmark_level:
        mine = snaps[-1].total_value / snaps[0].total_value - 1
        index = snaps[-1].benchmark_level / snaps[0].benchmark_level - 1
        behind = index - mine
    ok_runs = done >= STAGE_TWO_REBALANCES
    ok_errors = crashes == 0 and not unpriced
    ok_gap = behind is None or behind <= STAGE_TWO_MAX_BEHIND
    verdict = (
        "PASSED — stage 3 (real money, 10-20% to start) may begin"
        if ok_runs and ok_errors and ok_gap else
        "REVIEW — more than 15% behind the index" if not ok_gap else
        "FIX — errors or unpriced holdings; the three months restart once fixed"
        if not ok_errors else
        "in progress"
    )
    return [
        "STAGE 2 STATUS (docs/FACTOR_STRATEGY.md)",
        f"  monthly rebalances completed: {done} of {STAGE_TWO_REBALANCES}",
        f"  evaluation errors: {crashes} · holdings unpriced for over a month: "
        f"{len(unpriced)}",
        "  behind the S&P 500 since the start: "
        + (f"{behind:+.1%} (limit {STAGE_TWO_MAX_BEHIND:.0%})" if behind is not None else "n/a"),
        f"  status: {verdict}",
    ]


def report(
    state: PaperState, today: date, *, rebalanced: bool, note: str = "",
    label: str = "GCFP v4", ticker_of=None,
) -> str:
    book = state.book
    snaps = book.snapshots

    def name(symbol: str) -> str:
        ticker = ticker_of(symbol) if ticker_of else ""
        return f"{ticker} ({symbol})" if ticker and ticker != symbol else symbol

    lines = [
        "=" * 78,
        f"{label} — PAPER TRADING (§13.11) · {today.isoformat()}",
        "=" * 78,
        f"started {state.started_on.isoformat()} · "
        f"{(today - state.started_on).days / 30.44:.1f} months of the 2-3 required",
        f"companies screened each month: {len(state.symbols)} (fixed at the start)",
        f"this run: {'monthly rebalance' if rebalanced else 'marked to market only'}"
        + (f" · last rebalance {state.last_rebalance.isoformat()}" if state.last_rebalance else ""),
    ]
    if note:
        lines.append(f"NOTE: {note}")

    if snaps:
        first, last = snaps[0], snaps[-1]
        lines.append("")
        lines.append(f"value: {first.total_value:,.0f} -> {last.total_value:,.0f} "
                     f"({last.total_value / first.total_value - 1:+.2%})")
        if first.benchmark_level and last.benchmark_level:
            kind = ("dividends reinvested" if state.benchmark_symbol == "^SP500TR"
                    else "price only")
            lines.append(f"S&P 500 ({kind}) over the same dates: "
                         f"{last.benchmark_level / first.benchmark_level - 1:+.2%}")
        lines.append(f"in stock picks: {last.invested:,.0f} "
                     f"({last.invested / last.total_value:.1%}); "
                     f"following the index: {last.cash:,.0f}")

    lines.extend(["", "OPEN PAPER POSITIONS"])
    if not book.positions:
        lines.append("  none")
    for symbol, p in sorted(book.positions.items()):
        entry = p.cost_basis / p.shares if p.shares else 0.0
        change = p.last_price / entry - 1 if entry and p.last_price else 0.0
        lines.append(
            f"  {name(symbol)}: {p.classification.value} · bought {p.opened_on.isoformat()} "
            f"at {entry:,.2f} · last {p.last_price:,.2f} ({change:+.1%}) · "
            f"conviction {p.conviction_at_purchase:.0f} · {p.anchor_mode}"
            + (f" · price missing {p.missed_marks} run(s)" if p.missed_marks else "")
        )

    if book.closed:
        lines.extend(["", "CLOSED PAPER POSITIONS"])
        for c in book.closed:
            lines.append(f"  {name(c.symbol)}: {c.opened_on.isoformat()} -> "
                         f"{c.closed_on.isoformat()} · {c.total_return:+.1%} · {c.exit_reason}")
        curve = [(s.as_of, s.benchmark_level) for s in snaps if s.benchmark_level]
        versus = picks_vs_index(book.closed, curve, snaps)
        if versus is not None:
            lines.extend(versus.as_report_lines())

    latest = state.log[-1] if state.log else None
    if rebalanced and latest and latest.get("trades"):
        lines.extend([
            "",
            f"THIS MONTH'S TRADES ({latest['date']}) — to copy with real money,",
            "scale each by your own portfolio's size:",
        ])
        for t in sorted(latest["trades"], key=lambda t: t["side"] != "sell"):
            share = t.get("share_of_portfolio")
            lines.append(
                f"  {t['side'].upper():4s} {name(t['symbol'])}"
                + (f" · {share:.1%} of the portfolio" if share is not None else "")
                + f" · about ${t['price']:,.2f} a share · {t['reason']}"
            )

    lines.extend(["", *stage_two_status(state)])

    lines.extend(["", "MONTHLY REBALANCES"])
    if not state.log:
        lines.append("  none yet")
    for entry in state.log:
        lines.append(
            f"  {entry['date']}: {entry['evaluated']} screened · "
            f"{entry['buy_signals']} BUY signal(s) · bought {', '.join(entry['buys']) or 'nothing'}"
            + (" · sold " + ", ".join(f"{s} ({r})" for s, r in entry["sells"])
               if entry["sells"] else "")
        )
    lines.extend([
        "",
        "Simulated only. Fills assume the latest close; money not in picks",
        "follows the S&P 500 and holdings are credited their dividends, as in",
        "the backtest.",
        "=" * 78,
    ])
    return "\n".join(lines) + "\n"


__all__ = [
    "PaperState",
    "stage_two_status",
    "book_from_dict",
    "book_to_dict",
    "load_state",
    "mark_to_market",
    "rebalance",
    "report",
    "save_state",
]
