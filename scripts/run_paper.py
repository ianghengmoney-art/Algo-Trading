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
    return parser.parse_args(argv)


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
        return Backtester(adapter, config, settings, symbols, label="paper"), symbols

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

    backtester = Backtester(
        adapter, config, settings, symbols, label="paper",
        eligibility=eligibility,
        peer_pool=industry_peer_pool(adapter, symbols, args.peers_per_name),
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

    paper.save_state(state, args.book)
    text = paper.report(state, today, rebalanced=rebalanced, note=note)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    print(text)
    return 0


def main(argv=None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
