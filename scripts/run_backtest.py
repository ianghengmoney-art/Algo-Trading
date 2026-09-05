#!/usr/bin/env python3
"""Run the §13 validation protocol.

    # against the synthetic market, offline
    python scripts/run_backtest.py --source synthetic

    # against real filings (slow: it touches every filer at every rebalance)
    python scripts/run_backtest.py --source edgar \
        --user-agent "you you@example.com" \
        --symbols AAPL MSFT CAT DE --start 2010-01-01 --end 2024-12-31

Exits non-zero when a Module I strategy-break criterion trips, so a broken
system fails the run rather than producing a report nobody reads to the end.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gcfp.backtest.accuracy import measure_accuracy
from gcfp.backtest.engine import Backtester, BacktestSettings, WalkForwardSplit
from gcfp.backtest.metrics import summarise
from gcfp.backtest.report import ValidationReport
from gcfp.backtest.sweep import run_sweep
from gcfp.backtest.variants import build_variants, passive_curve
from gcfp.classification import Classification
from gcfp.config import DEFAULT_CONFIG
from gcfp.modules.i_expectations import evaluate_break_criteria
from gcfp.runner import build_free_adapter, free_stack_config


def _date(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", default="synthetic", choices=("synthetic", "edgar"))
    parser.add_argument("--user-agent", help="required for EDGAR; SEC policy")
    parser.add_argument("--symbols", nargs="*", help="tickers to include")
    parser.add_argument("--start", type=_date, default=date(2012, 1, 1))
    parser.add_argument("--end", type=_date, default=date(2020, 12, 31))
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--out", type=Path, help="write the report here")
    parser.add_argument("--sweep", action="store_true",
                        help="run the §13.7 parameter sweep (slow)")
    parser.add_argument("--no-benchmarks", action="store_true")
    parser.add_argument("--progress", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.source == "synthetic":
        from gcfp.backtest.fixtures import build_synthetic_market

        # Generate history well before the test window so A6's five-year
        # criteria and B1's six-year growth cap have runway on day one.
        adapter = build_synthetic_market(
            date(args.start.year - 7, 1, 1), args.end
        )
        symbols = args.symbols or [
            s for s in sorted(adapter.companies) if not s.startswith("^")
        ]
        config = free_stack_config(DEFAULT_CONFIG)
    else:
        if not args.user_agent:
            raise SystemExit('EDGAR requires --user-agent "you you@example.com"')
        if not args.symbols:
            raise SystemExit(
                "--symbols is required for an EDGAR backtest: screening every "
                "filer at every rebalance over 15 years is not tractable. Give "
                "the candidate list you want tested."
            )
        adapter = build_free_adapter(args.user_agent, cache_dir=args.cache_dir)
        symbols = args.symbols
        config = free_stack_config(DEFAULT_CONFIG)

    settings = BacktestSettings(
        start=args.start, end=args.end, initial_capital=args.capital
    )
    split = WalkForwardSplit.by_fraction(args.start, args.end)

    print(f"running {len(symbols)} symbols over "
          f"{args.start.isoformat()}..{args.end.isoformat()}", file=sys.stderr)

    primary = Backtester(
        adapter, config, settings, symbols, label="GCFP v4", split=split
    ).run(progress=args.progress)

    # §13.4 benchmarks.
    benchmarks = []
    if not args.no_benchmarks:
        for variant in build_variants(config, settings.benchmark_symbol):
            print(f"  benchmark: {variant.name}", file=sys.stderr)
            if variant.is_passive:
                curve = passive_curve(primary.benchmark_curve, args.capital)
                benchmarks.append(
                    (variant.name, variant.proves,
                     summarise(variant.name, curve, []))
                )
                continue
            run = Backtester(
                adapter, variant.config, settings, symbols,
                label=variant.name, split=split,
                signal_filter=variant.signal_filter,
                sizer=variant.sizer,
                conviction_scorer=variant.conviction_scorer,
            ).run()
            benchmarks.append(
                (variant.name, variant.proves,
                 summarise(variant.name, run.equity_curve, run.book.closed,
                           run.benchmark_curve))
            )

    # §13.5 classification accuracy, over every routing the run recorded.
    routings = [
        (symbol, Classification(tag), record.as_of)
        for record in primary.rebalances
        for symbol, tag in record.classifications.items()
    ]
    accuracy = measure_accuracy(
        adapter, config, routings, horizon_end=args.end
    )

    # §13.7 sweep, on the training period only.
    sweep = None
    if args.sweep:
        print("  parameter sweep (training period only)", file=sys.stderr)
        train_settings = BacktestSettings(
            start=split.train_start, end=split.train_end,
            initial_capital=args.capital,
        )
        sweep = run_sweep(
            lambda cfg: Backtester(
                adapter, cfg, train_settings, symbols, label="sweep"
            ).run(),
            config,
            split=split,
            period_start=split.train_start,
            period_end=split.train_end,
            progress=args.progress,
        )

    report = ValidationReport(
        primary=primary,
        benchmarks=benchmarks,
        accuracy=accuracy,
        sweep=sweep,
        # EDGAR is point-in-time by construction; the synthetic fixture models
        # the same filing-date discipline.
        point_in_time=True,
        paper_traded_months=0.0,
        notes=[
            "Simulated fills assume the operator transacts at the rebalance "
            "close. That is optimistic about liquidity and is stated rather "
            "than modelled away.",
        ],
    )
    text = report.render(config)
    print(text)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        print(f"\nwritten to {args.out}", file=sys.stderr)

    integrity = evaluate_break_criteria(
        config,
        closed_positions=len(primary.book.closed),
        classification_accuracy=accuracy.accuracy,
        single_anchor_rate=primary.single_anchor_rate,
        divergence_rate=primary.divergence_rate,
    )
    if integrity.any_tripped:
        names = ", ".join(c.name for c in integrity.tripped)
        print(f"\nSTRATEGY-BREAK CRITERION TRIPPED: {names}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
