#!/usr/bin/env python3
"""Paper trading (§13.11): the backtest's monthly step, run on live data.

    python scripts/run_paper.py --user-agent "you you@example.com"

The first run fixes a seeded sample of companies filing today and opens a
paper book with 100,000 in the index. Each later run marks the book to
market, and the first run in each calendar month also screens and trades on
paper, exactly as a backtest month would. The weekly screen calls this too,
so paper trading runs without anyone remembering to.

The book lives in ``reports/paper/book.json`` and the report beside it. The
workflow commits ``reports/``, which is what carries the book from one week
to the next. Nothing here places an order.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gcfp.backtest import paper
from gcfp.backtest.engine import Backtester, BacktestSettings
from gcfp.backtest.portfolio import BacktestBook
from gcfp.backtest.timebudget import TimeBudgetExceeded, time_limit
from gcfp.config import DEFAULT_CONFIG
from gcfp.runner import free_stack_config

DEFAULT_BOOK = Path("reports/paper/book.json")
DEFAULT_REPORT = Path("reports/paper/report.txt")


def _paper_defaults() -> tuple[str, int]:
    """Which strategy paper-trades, from scripts/backtest_defaults.json
    ("paper_strategy", "paper_variant"). GCFP until a factor variant passes
    its pre-registered test; then that variant, by changing the file."""
    import json

    path = Path(__file__).resolve().parent / "backtest_defaults.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        raw = {}
    return raw.get("paper_strategy", "gcfp"), int(raw.get("paper_variant", 1))


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default="edgar", choices=("edgar", "synthetic"))
    parser.add_argument("--user-agent")
    parser.add_argument("--sample", type=int, default=1000,
                        help="companies to screen each month, fixed at the first run")
    parser.add_argument("--peers-per-name", type=int, default=150)
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--book", type=Path, default=DEFAULT_BOOK)
    parser.add_argument("--out", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--today", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    parser.add_argument("--force-rebalance", action="store_true")
    parser.add_argument("--time-budget-min", type=float, default=None,
                        help="give up on this month's rebalance after this long; "
                             "the book is then left exactly as it was")
    parser.add_argument("--progress", action="store_true")
    strategy, variant = _paper_defaults()
    parser.add_argument("--strategy", choices=("gcfp", "factor"), default=strategy)
    parser.add_argument("--variant", type=int, choices=(1, 2, 3), default=variant)
    args = parser.parse_args(argv)
    if args.strategy == "factor":
        # Each strategy keeps its own book: the GCFP one is not overwritten.
        if args.book == DEFAULT_BOOK:
            args.book = Path(f"reports/paper/factor-v{args.variant}-book.json")
        if args.out == DEFAULT_REPORT:
            args.out = Path(f"reports/paper/factor-v{args.variant}-report.txt")
    return args


def make_backtester(args, adapter, config, settings, symbols, **kwargs):
    if args.strategy == "factor":
        from gcfp.backtest.factor import FactorBacktester, rules_for

        kwargs.pop("peer_pool", None)  # the factor strategy uses no peers
        return FactorBacktester(
            adapter, config, settings, symbols, label=f"paper factor v{args.variant}",
            rules=rules_for(args.variant), **kwargs,
        )
    return Backtester(adapter, config, settings, symbols, label="paper", **kwargs)


def build(args, state: paper.PaperState | None, today: date):
    """The adapter, symbols and Backtester, set up as the backtest sets them."""
    config = free_stack_config(DEFAULT_CONFIG)
    settings = BacktestSettings(start=today, end=today, initial_capital=args.capital)

    if args.source == "synthetic":
        from gcfp.backtest.fixtures import build_synthetic_market

        adapter = build_synthetic_market(date(today.year - 8, 1, 1), today)
        symbols = state.symbols if state else [
            s for s in sorted(adapter.companies) if not s.startswith("^")
        ]
        return make_backtester(args, adapter, config, settings, symbols), symbols

    from gcfp.data.pit_universe import load_filer_index, sample_symbols, symbol_cik
    from gcfp.runner import build_free_adapter
    from run_backtest import industry_peer_pool

    if not args.user_agent:
        raise SystemExit('EDGAR requires --user-agent "you you@example.com"')
    adapter = build_free_adapter(args.user_agent, cache_dir=args.cache_dir)
    index = load_filer_index(
        adapter.fundamentals._get_text, today, today, cache_dir=args.cache_dir,
    )
    symbols = state.symbols if state else sample_symbols(index, today, today, args.sample)

    def eligibility(symbol: str, as_of: date) -> bool:
        cik = symbol_cik(symbol)
        return cik is None or index.is_live(cik, as_of)

    backtester = make_backtester(
        args, adapter, config, settings, symbols,
        eligibility=eligibility,
        peer_pool=(
            industry_peer_pool(adapter, symbols, args.peers_per_name)
            if args.strategy == "gcfp" else None
        ),
        member_cache={},
    )
    return backtester, symbols


def run(args) -> int:
    started = time.monotonic()
    today = args.today or date.today()
    state = paper.load_state(args.book)
    backtester, symbols = build(args, state, today)
    if state is None:
        from gcfp.backtest.engine import choose_benchmark

        index_symbol, index_text = choose_benchmark(backtester.adapter, today, today)
        state = paper.PaperState(
            started_on=today, symbols=list(symbols),
            book=BacktestBook(cash=args.capital),
            benchmark_symbol=index_symbol,
        )
        print(f"paper trading starts: {len(symbols)} companies, "
              f"{args.capital:,.0f} following {index_text}", file=sys.stderr)
    backtester.settings = dataclasses.replace(
        backtester.settings, benchmark_symbol=state.benchmark_symbol
    )

    rebalanced, note = False, ""
    prices_before = {s: p.last_price for s, p in state.book.positions.items()}
    if state.due(today) or args.force_rebalance:
        deadline = (
            started + args.time_budget_min * 60 if args.time_budget_min else None
        )
        before = state.to_json()
        print(f"paper rebalance for {today.isoformat()} "
              f"({len(state.symbols)} companies)", file=sys.stderr)
        try:
            with time_limit(deadline):
                record = paper.rebalance(backtester, state, today)
            rebalanced = True
            print(f"  {record.evaluated} screened, {record.passers} BUY signal(s), "
                  f"bought {record.buys or 'nothing'}, sold {record.sells or 'nothing'}",
                  file=sys.stderr)
        except TimeBudgetExceeded:
            # Leave the book exactly as it was; next week's run tries again.
            state = paper.PaperState.from_json(before)
            note = ("this month's rebalance did not finish inside its time "
                    "budget; the book is unchanged and the next run retries")
            print(f"  {note}", file=sys.stderr)
    if not rebalanced:
        paper.mark_to_market(backtester, state, today)

    flags = paper.suspicious_moves(prices_before, state)
    if flags:
        # Flag, never act: the book is saved as computed, and the weekly
        # check looks at each flagged price by hand.
        note = "; ".join(filter(None, [note, "CHECK PRICES: " + "; ".join(flags)]))
        print(f"  {note}", file=sys.stderr)
    paper.save_state(state, args.book)
    def ticker_of(symbol: str) -> str:
        resolve = getattr(backtester.adapter, "_price_symbol", None)
        try:
            resolved = resolve(symbol) if resolve else None
        except Exception:
            resolved = None
        return resolved[0] if resolved else ""

    label = f"FACTOR STRATEGY v{args.variant}" if args.strategy == "factor" else "GCFP v4"
    text = paper.report(state, today, rebalanced=rebalanced, note=note,
                        label=label, ticker_of=ticker_of)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    print(text)
    return 0


def main(argv=None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
