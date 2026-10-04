#!/usr/bin/env python3
"""Run the §13 validation protocol.

    # against the synthetic market, offline
    python scripts/run_backtest.py --source synthetic

    # against real filings (slow: it touches every filer at every rebalance)
    python scripts/run_backtest.py --source edgar \
        --user-agent "you you@example.com" \
        --symbols AAPL MSFT CAT DE --start 2010-01-01 --end 2024-12-31

    # survivorship-aware: a random sample of everyone who filed during the
    # period, dead companies included
    python scripts/run_backtest.py --source edgar \
        --user-agent "you you@example.com" --pit-sample 300

Exits non-zero when a Module I strategy-break criterion trips, so a broken
system fails the run rather than producing a report nobody reads to the end.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
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
    parser.add_argument(
        "--symbols-from", type=Path, metavar="FILE",
        help=(
            "read tickers from a file, one per line; '#' comments and blank "
            "lines ignored. run_screen.py --symbols-out writes one."
        ),
    )
    parser.add_argument(
        "--pit-sample", type=int, metavar="N",
        help=(
            "draw N companies at random from the SEC filing index over the "
            "backtest period, including companies that later died, and "
            "screen each one only while it was filing. Fixes the "
            "survivors-only bias of --symbols."
        ),
    )
    parser.add_argument("--start", type=_date, default=date(2012, 1, 1))
    parser.add_argument("--end", type=_date, default=date(2020, 12, 31))
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--out", type=Path, help="write the report here")
    parser.add_argument("--sweep", action="store_true",
                        help="run the §13.7 parameter sweep (slow)")
    parser.add_argument("--no-benchmarks", action="store_true")
    parser.add_argument("--progress", action="store_true")
    parser.add_argument(
        "--peers-per-name", type=int, default=25, metavar="N",
        help=(
            "same-industry companies (by SIC code, from EDGAR) offered to C2 "
            "as peers for each candidate. 0 restricts peers to the sample."
        ),
    )
    parser.add_argument(
        "--time-budget-min", type=float,
        default=290.0 if os.environ.get("GITHUB_ACTIONS") == "true" else None,
        help=(
            "stop starting new benchmark runs once this many minutes would be "
            "exceeded, and write the report with what finished. Defaults to "
            "290 on GitHub Actions, whose job is killed at 330 with no report."
        ),
    )
    return parser.parse_args()


def read_symbol_file(path: Path) -> list[str]:
    """Tickers from a file, one per line, ignoring comments and blanks."""
    if not path.exists():
        raise SystemExit(f"no such symbol file: {path}")
    out: list[str] = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line.upper())
    if not out:
        raise SystemExit(f"{path} contained no tickers")
    return out


def industry_peer_pool(adapter, sample: list[str], per_name: int):
    """Same-industry tickers for each candidate, looked up once per name.

    The SEC lists *current* filers by SIC code, so these peers are survivors.
    That biases the peer multiple, not which companies get bought — peers are
    reference points only — and the report says so.
    """
    if per_name <= 0:
        return None
    edgar = getattr(adapter, "fundamentals", adapter)
    lookup = getattr(edgar, "symbols_by_sic", None)
    if lookup is None:
        return None

    def cik(symbol: str):
        try:
            return edgar.ticker_to_cik(symbol)
        except Exception:
            return None

    sample_ciks = {c for c in (cik(s) for s in sample) if c is not None}
    memo: dict[str, list[str]] = {}

    def pool(symbol: str) -> list[str]:
        if symbol not in memo:
            peers: list[str] = []
            try:
                code = edgar.get_profile(symbol).industry_code
                if code:
                    # Ask for extra: some will be the sample's own companies.
                    for peer in lookup(code, limit=per_name * 2):
                        if cik(peer) not in sample_ciks and len(peers) < per_name:
                            peers.append(peer)
            except Exception:
                peers = []
            memo[symbol] = peers
        return memo[symbol]

    return pool


def main() -> int:
    args = parse_args()
    started = time.monotonic()

    def minutes() -> float:
        return (time.monotonic() - started) / 60
    filer_index = None
    eligibility = None
    peer_pool = None
    peer_member_cache: dict = {}

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
        symbols = list(args.symbols or ())
        if args.symbols_from:
            symbols.extend(read_symbol_file(args.symbols_from))
        symbols = list(dict.fromkeys(s.upper() for s in symbols))
        adapter = build_free_adapter(args.user_agent, cache_dir=args.cache_dir)
        if args.pit_sample:
            from gcfp.data.pit_universe import (
                load_filer_index, sample_symbols, symbol_cik,
            )

            print("reading the SEC filing index (includes dead companies)",
                  file=sys.stderr)
            filer_index = load_filer_index(
                adapter.fundamentals._get_text, args.start, args.end,
                cache_dir=args.cache_dir,
                progress=(lambda m: print(m, file=sys.stderr)) if args.progress else None,
            )
            sampled = sample_symbols(filer_index, args.start, args.end, args.pit_sample)
            symbols.extend(s for s in sampled if s not in symbols)

            def eligibility(symbol: str, as_of: date) -> bool:
                cik = symbol_cik(symbol)
                return cik is None or filer_index.is_live(cik, as_of)
        peer_pool = industry_peer_pool(adapter, symbols, args.peers_per_name)
        if not symbols:
            raise SystemExit(
                "--pit-sample, --symbols or --symbols-from is required for an EDGAR "
                "backtest: screening every filer at every rebalance over 15 "
                "years is not tractable.\n\n"
                "  --symbols-from FILE   one ticker per line; '#' comments and "
                "blank lines ignored.\n"
                "                        run_screen.py --symbols-out FILE "
                "writes one.\n\n"
                "Whichever you use, read the survivorship warning the report "
                "prints: a list chosen today contains only companies that\n"
                "still exist, and backtesting it measures survival rather "
                "than the strategy."
            )
        config = free_stack_config(DEFAULT_CONFIG)

    settings = BacktestSettings(
        start=args.start, end=args.end, initial_capital=args.capital
    )
    split = WalkForwardSplit.by_fraction(args.start, args.end)

    print(f"running {len(symbols)} symbols over "
          f"{args.start.isoformat()}..{args.end.isoformat()}", file=sys.stderr)

    primary_started = minutes()
    primary = Backtester(
        adapter, config, settings, symbols, label="GCFP v4", split=split,
        eligibility=eligibility, peer_pool=peer_pool,
        peer_member_cache=peer_member_cache,
    ).run(progress=args.progress)
    # Each benchmark replays the same months over the same companies, so the
    # primary run is a fair estimate of how long one takes.
    run_minutes = max(minutes() - primary_started, 0.1)
    print(f"  main backtest done in {run_minutes:.0f} min", file=sys.stderr)

    # §13.4 benchmarks.
    benchmarks = []
    skipped: list[str] = []
    if not args.no_benchmarks:
        for variant in build_variants(config, settings.benchmark_symbol):
            if (
                not variant.is_passive
                and args.time_budget_min is not None
                and minutes() + run_minutes > args.time_budget_min
            ):
                skipped.append(variant.name)
                continue
            print(f"  benchmark: {variant.name} ({minutes():.0f} min elapsed)",
                  file=sys.stderr)
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
                eligibility=eligibility, peer_pool=peer_pool,
                peer_member_cache=peer_member_cache,
            ).run(progress=args.progress)
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
                adapter, cfg, train_settings, symbols, label="sweep",
                eligibility=eligibility, peer_pool=peer_pool,
                peer_member_cache=peer_member_cache,
            ).run(),
            config,
            split=split,
            period_start=split.train_start,
            period_end=split.train_end,
            progress=args.progress,
        )

    if skipped:
        print(f"  skipped for time: {', '.join(skipped)}", file=sys.stderr)

    notes = [
        "Simulated fills assume the operator transacts at the rebalance "
        "close. That is optimistic about liquidity and is stated rather "
        "than modelled away.",
        f"Holdings whose price stopped and were closed as delisted at "
        f"{settings.delisting_return:+.0%}: {primary.assumed_delistings}.",
    ]
    if skipped:
        notes.append(
            "Benchmarks NOT run, to finish inside the time budget: "
            + ", ".join(skipped)
            + ". Their comparisons are missing from this report, not passed."
        )
    if filer_index is not None:
        from gcfp.data.pit_universe import survivorship_coverage

        def has_prices(symbol: str) -> bool:
            try:
                return bool(adapter.get_prices(symbol, args.start, args.end))
            except Exception:
                return False

        coverage = survivorship_coverage(filer_index, symbols, args.end, has_prices)
        notes.extend(coverage.lines())
    if peer_pool is not None:
        notes.append(
            f"C2 peers: up to {args.peers_per_name} same-industry companies per "
            "candidate from EDGAR's SIC listing. That listing holds today's "
            "filers, so peers are survivors; they set the comparison multiple "
            "and are never bought."
        )
    elif args.source == "edgar":
        notes.append(
            "C2 peers were drawn from the sample only, which rarely holds two "
            "companies in one industry; expect SINGLE-ANCHOR MODE throughout."
        )
    if filer_index is None and args.source == "edgar":
        notes.append(
            "SURVIVORSHIP: the symbol list was chosen today, so it holds only "
            "companies that survived. The CAGR is an upper bound; rerun with "
            "--pit-sample for a survivorship-aware result."
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
        notes=notes,
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
