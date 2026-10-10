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
import threading
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gcfp.backtest.accuracy import measure_accuracy
from gcfp.backtest.engine import Backtester, BacktestSettings, WalkForwardSplit
from gcfp.backtest.metrics import summarise
from gcfp.backtest.report import ValidationReport
from gcfp.backtest.sweep import run_sweep
from gcfp.backtest.timebudget import TimeBudgetExceeded, time_limit
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
        "--peers-per-name", type=int, default=150, metavar="N",
        help=(
            "same-industry companies (by SIC code, from EDGAR) offered to C2 "
            "as peers for each candidate. 0 restricts peers to the sample."
        ),
    )
    parser.add_argument(
        "--c2-options", action=argparse.BooleanOptionalAction, default=False,
        help=(
            "also run the C2 peer-rule options (wider bands; wider industry "
            "group) on the same data and compare them in the report"
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
    parser.add_argument(
        "--strategy", choices=("gcfp", "factor"), default=default_strategy(),
        help=(
            "gcfp: the GCFP v4 rules. factor: the strategy pre-registered in "
            "docs/FACTOR_STRATEGY.md. Defaults to scripts/backtest_defaults.json, "
            "so the workflow, which passes no such option, runs what the branch "
            "says."
        ),
    )
    parser.add_argument(
        "--variant", type=int, choices=(1, 2, 3, 4),
        default=int(_defaults().get("variant", 1)),
        help="which registered factor variant (docs/FACTOR_STRATEGY.md)",
    )
    return parser.parse_args()


def _defaults() -> dict:
    path = Path(__file__).resolve().parent / "backtest_defaults.json"
    try:
        import json

        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def default_strategy() -> str:
    return _defaults().get("strategy", "gcfp")


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


#: Set once a report file is on disk; the watchdog's exit code depends on it.
_REPORT_WRITTEN = threading.Event()

#: Minutes past the time budget at which the watchdog ends the process. The
#: runner kills the step at 330 minutes and a killed step commits nothing.
WATCHDOG_GRACE_MIN = 5


def start_watchdog(budget_min: float | None) -> None:
    """End the process cleanly if everything else failed to stop in time.

    Every phase has its own limit, but a limit can only interrupt Python
    code. This is the last line: past the budget, exit with success if a
    report is already written — so the workflow commits it — and with an
    error if not.
    """
    if budget_min is None:
        return

    def fire() -> None:
        written = _REPORT_WRITTEN.is_set()
        print(
            f"\nWATCHDOG: {budget_min + WATCHDOG_GRACE_MIN:.0f} minutes reached; "
            + ("ending with the report already written." if written
               else "no report was written."),
            file=sys.stderr, flush=True,
        )
        sys.stdout.flush()
        os._exit(0 if written else 3)

    timer = threading.Timer((budget_min + WATCHDOG_GRACE_MIN) * 60, fire)
    timer.daemon = True
    timer.start()


def run_factor(
    args, adapter, config, settings, split, symbols, eligibility, peer_pool,
    member_cache, benchmark_text, minutes, until_end_less, survivorship_lines,
) -> int:
    """The pre-registered factor strategy (docs/FACTOR_STRATEGY.md)."""
    from gcfp.backtest.factor import (
        REGISTERED_VARIANTS, FactorBacktester, edge_statistics, judge, rules_for,
        write_run_data,
    )

    VARIANT = args.variant
    from gcfp.backtest.metrics import max_drawdown, picks_vs_index, summarise

    # The registered long-history premise check: a few small downloads, run
    # first so a slow backtest cannot crowd it out.
    long_history = "not run"
    try:
        from gcfp.backtest import longhistory
        from run_long_history import DEFAULT_OUT, http_get

        text_lh, verdict_lh = longhistory.run(http_get, args.cache_dir)
        DEFAULT_OUT.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_OUT.write_text(text_lh)
        long_history = (
            "SUPPORTS the premise" if verdict_lh else
            "DOES NOT SUPPORT the premise" if verdict_lh is False else
            "not determined"
        ) + f" (full report: {DEFAULT_OUT})"
        print(text_lh, file=sys.stderr)
    except Exception as exc:
        long_history = f"failed: {type(exc).__name__}: {exc}"

    rules = rules_for(VARIANT)
    backtester = FactorBacktester(
        adapter, config, settings, symbols, label="Factor strategy",
        split=split, eligibility=eligibility, member_cache=member_cache,
        rules=rules,
    )
    run_started = time.monotonic()
    run = backtester.run(progress=args.progress, deadline=until_end_less(8))
    run_minutes = (time.monotonic() - run_started) / 60

    # Factor attribution of this run (docs/FACTOR_STRATEGY.md, variant 4's
    # fifth criterion; reported for every variant).
    alpha = None
    attribution_lines: list[str] = []
    try:
        from gcfp.backtest import attribution
        from run_long_history import http_get

        factors = attribution.load_factors(http_get, args.cache_dir)
        fits = attribution.attribute(
            attribution.monthly_from_curve(run.equity_curve), factors
        )
        model, fit = fits[-1]
        alpha = (fit.alpha_per_year, fit.t(0))
        attribution_lines = ["FACTOR ATTRIBUTION (Kenneth French's US factors)"]
        for model, fit in fits:
            attribution_lines.append(f"  {model}")
            attribution_lines += attribution._fit_lines(fit)
    except Exception as exc:
        attribution_lines = [f"FACTOR ATTRIBUTION: not run ({type(exc).__name__}: {exc})"]
    verdict = judge(run, backtester.universe_curve, split, VARIANT, alpha=alpha)
    summary = summarise("Factor strategy", run.equity_curve, run.book.closed,
                        run.benchmark_curve)
    versus = picks_vs_index(run.book.closed, run.benchmark_curve, run.book.snapshots)
    counts = backtester.eligible_counts
    held = [s.position_count for s in run.book.snapshots]
    sells = sum(len(r.sells) for r in run.rebalances)
    years = max((settings.end - settings.start).days / 365.25, 0.1)
    turnover = sells / years / max(backtester.rules.holdings, 1)
    stats = edge_statistics(run.equity_curve, run.benchmark_curve,
                            backtester.universe_curve)
    from gcfp.backtest.metrics import annualised_return

    gross_cagr = annualised_return(backtester.gross_benchmark_curve)

    def ticker_of(symbol: str) -> str:
        resolve = getattr(adapter, "_price_symbol", None)
        try:
            resolved = resolve(symbol) if resolve else None
        except Exception:
            resolved = None
        return resolved[0] if resolved else ""

    data_files: list[str] = []
    if args.out and run.book.snapshots:
        try:
            data_files = write_run_data(
                Path("reports") / f"factor-v{VARIANT}", run,
                backtester.universe_curve, ticker_of,
            )
        except Exception as exc:  # the report matters more than the CSVs
            data_files = [f"(run data not written: {type(exc).__name__}: {exc})"]

    def lines(extra: list[str]) -> list[str]:
        out = [
            "=" * 78,
            f"FACTOR STRATEGY — variant {VARIANT} of {REGISTERED_VARIANTS} in docs/FACTOR_STRATEGY.md "
            f"(sample of {len(symbols)} companies)",
            "=" * 78,
            f"period: {settings.start.isoformat()}..{settings.end.isoformat()} · "
            f"earlier half to {split.train_end.isoformat()}, later half from "
            f"{split.test_start.isoformat()}",
            f"spare money and benchmark: {benchmark_text}"
            + (f", after {rules.dividend_withholding:.0%} dividend withholding "
               "(the book's dividends lose the same share)"
               if rules.dividend_withholding else ""),
            f"long-history premise check (1963 onward): {long_history}",
            "",
            "RETURNS (per year)",
            *[f"  {l}" for l in verdict.lines],
            "",
            *stats,
            "",
            *attribution_lines,
            "",
            *[l for l in summary.as_report_lines() if "Module I" not in l],
        ]
        if versus is not None:
            out += ["", *versus.as_report_lines()]
        drawdown = max_drawdown(run.benchmark_curve)
        out += [
            "",
            f"S&P 500 max drawdown over the same months: "
            + (drawdown.as_report_line() if drawdown else "n/a"),
            f"holdings per month: median {sorted(held)[len(held) // 2] if held else 0}"
            f" · eligible companies ranked per month: median "
            f"{sorted(counts)[len(counts) // 2] if counts else 0}",
            f"positions sold over the period: {sells} · about "
            f"{turnover:.0%} of the portfolio replaced per year",
        ]
        if run.stopped_early_at is not None:
            out.append(f"STOPPED EARLY at {run.stopped_early_at.isoformat()}: "
                       "the verdict covers only the months completed.")
        out += [
            "",
            "NOTES",
            ("  Fills at the rebalance-day close; cost per fill by liquidity "
             "(0.10% at $50M+/day, 0.25% at $10-50M, 0.50% below); "
             f"{1 - rules.dividend_withholding:.0%} of dividends credited on ex-dates."
             if rules.cost_tiers else
             "  Fills at the month-end close, 0.1% cost per fill; dividends credited "
             "on ex-dates."),
            *([f"  Holdings trimmed back to {1 / rules.holdings:.0%} after passing "
               f"{rules.max_weight:.0%}: {backtester.trims} times; at most "
               f"{rules.max_per_industry} holdings per SIC major group."]
              if rules.max_weight else []),
            *([f"  The S&P 500 before dividend tax returned {gross_cagr:+.2%}/yr."]
              if rules.dividend_withholding and gross_cagr is not None else []),
            "  The equal-weight universe benchmark excludes trading costs, which "
            "flatters it.",
            *(["  EVALUATIONS THAT RAISED (a code fault, not a data gap):"]
              + [f"    {n:5d}  {c}" for c, n in sorted(run.crashes.items(), key=lambda kv: -kv[1])[:10]]
              if run.crashes else ["  No evaluation raised an error."]),
            f"  Holdings closed as delisted at {settings.delisting_return:+.0%}: "
            f"{run.assumed_delistings}.",
            *[f"  {l}" for l in extra],
            *[f"  full data: {f}" for f in data_files],
            f"  run time: {minutes():.0f} min",
            "=" * 78,
        ]
        return out

    def write(extra: list[str]) -> str:
        text = "\n".join(lines(extra)) + "\n"
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text)
            _REPORT_WRITTEN.set()
        return text

    text = write(["SURVIVORSHIP COVERAGE: not yet computed."] if survivorship_lines else [])
    surv: list[str] = []
    if survivorship_lines is not None:
        surv = survivorship_lines()
        text = write(surv)
    print(text, flush=True)

    # Sensitivity (docs/FACTOR_STRATEGY.md, variant 4): the same rules with
    # another rebalance day or holding count. Reported as a range, never used
    # to choose. Each run is skipped when the job has too little time left.
    configs = _defaults().get("sensitivity") or []
    if configs and VARIANT >= 4:
        import dataclasses

        from gcfp.backtest.factor import _monthly_returns

        rows = ["SENSITIVITY — same rules, one thing changed (reported, never used to choose)",
                f"  {'change':34s} {'CAGR':>8s} {'index':>8s} {'excess':>8s} {'t':>6s} {'max DD':>7s}"]

        def row(label: str, result, bench) -> str:
            s_c, b_c = annualised_return(result.equity_curve), annualised_return(bench)
            strat, idx = _monthly_returns(result.equity_curve), _monthly_returns(bench)
            days = sorted(set(strat) & set(idx))
            excess = [strat[d] - idx[d] for d in days]
            t = float("nan")
            if len(excess) > 12:
                import statistics as st

                sd = st.stdev(excess)
                t = st.fmean(excess) / (sd / len(excess) ** 0.5) if sd > 0 else t
            dd = max_drawdown(result.equity_curve)
            pct = lambda x: f"{x:+.2%}" if x is not None else "n/a"
            diff = s_c - b_c if s_c is not None and b_c is not None else None
            return (f"  {label:34s} {pct(s_c):>8s} {pct(b_c):>8s} {pct(diff):>8s} "
                    f"{t:+6.2f} {(f'{dd.depth:.0%}' if dd else 'n/a'):>7s}")

        rows.append(row(f"as registered ({rules.holdings} names)", run,
                        run.benchmark_curve))
        for cfg in configs:
            label = cfg.get("label") or ", ".join(f"{k} {v}" for k, v in cfg.items())
            deadline = until_end_less(8)
            if deadline is not None and deadline - time.monotonic() < run_minutes * 60 * 1.25 + 300:
                rows.append(f"  {label:34s} skipped: not enough time left in the job")
                continue
            s_rules = dataclasses.replace(
                rules, **{k: cfg[k] for k in ("holdings", "buffer_rank") if k in cfg})
            s_settings = dataclasses.replace(settings, rebalance_day=cfg.get("rebalance_day"))
            print(f"sensitivity: {label}", file=sys.stderr, flush=True)
            try:
                bt = FactorBacktester(
                    adapter, config, s_settings, symbols, label=f"sensitivity {label}",
                    split=split, eligibility=eligibility, member_cache=member_cache,
                    rules=s_rules,
                )
                result = bt.run(progress=False, deadline=deadline)
                rows.append(row(label, result, result.benchmark_curve)
                            + (" (stopped early)" if result.stopped_early_at else ""))
            except Exception as exc:
                rows.append(f"  {label:34s} failed: {type(exc).__name__}: {exc}")
            text = write(surv + ["", *rows])
        text = write(surv + ["", *rows])
        print("\n".join(rows), flush=True)
    return 0


def main() -> int:
    args = parse_args()
    if _defaults().get("leverage_only"):
        # The leveraged trend test (docs/LEVERAGE_STRATEGY.md): monthly
        # French data only, no stock data, a few minutes.
        from run_leverage import main as leverage_main

        code = leverage_main(["--cache-dir", str(args.cache_dir)])
        if args.out:
            args.out.write_text(Path("reports/leverage.txt").read_text()
                                + "\n" + Path("reports/leverage-daily.txt").read_text()
                                + "\n" + Path("reports/leverage-kelly.txt").read_text()
                                + "\n" + Path("reports/leverage-improve.txt").read_text()
                                + "\n" + Path("reports/leverage-multi-asset.txt").read_text())
        return code
    if _defaults().get("attribution_only"):
        # A diagnostic job: regress the saved runs on the French factors and
        # stop. The workflow's backtest job is how it reaches GitHub Actions.
        from run_attribution import main as attribution_main

        code = attribution_main(["--cache-dir", str(args.cache_dir)])
        if args.out:
            args.out.write_text(Path("reports/attribution.txt").read_text())
        return code
    started = time.monotonic()
    start_watchdog(args.time_budget_min)

    def minutes() -> float:
        return (time.monotonic() - started) / 60
    filer_index = None
    eligibility = None
    peer_pool = None
    member_cache: dict = {}

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

    from gcfp.backtest.engine import choose_benchmark, month_ends

    rebalance_dates = month_ends(args.start, args.end) or [args.end]
    benchmark_symbol, benchmark_text = choose_benchmark(
        adapter, rebalance_dates[0], min(rebalance_dates[-1], date.today())
    )
    print(f"index: {benchmark_text}", file=sys.stderr)
    settings = BacktestSettings(
        start=args.start, end=args.end, initial_capital=args.capital,
        benchmark_symbol=benchmark_symbol,
    )
    split = WalkForwardSplit.by_fraction(args.start, args.end)

    print(f"running {len(symbols)} symbols over "
          f"{args.start.isoformat()}..{args.end.isoformat()}", file=sys.stderr)

    primary_started = minutes()
    # One end time for the whole job. Each phase gets it less a margin for
    # the phases after it, and every phase stops at its own limit and says
    # so — the job is never killed with nothing reported.
    hard_end = (
        time.monotonic() + (args.time_budget_min - minutes()) * 60
        if args.time_budget_min is not None
        else None
    )

    def until_end_less(margin_min: float) -> float | None:
        return None if hard_end is None else hard_end - margin_min * 60

    def survivorship_lines() -> list[str]:
        from gcfp.data.pit_universe import survivorship_coverage

        coverage_deadline = until_end_less(4)
        unchecked: list[str] = []

        checked: list[str] = []

        def has_prices(symbol: str) -> bool:
            if coverage_deadline is not None and time.monotonic() > coverage_deadline:
                unchecked.append(symbol)
                return False
            checked.append(symbol)
            if len(checked) % 25 == 0:
                print(f"    checked {len(checked)} ({minutes():.0f} min elapsed)",
                      file=sys.stderr, flush=True)
            try:
                return bool(adapter.get_prices(symbol, args.start, args.end))
            except Exception:
                return False

        print(f"  survivorship coverage ({minutes():.0f} min elapsed)", file=sys.stderr)
        try:
            with time_limit(until_end_less(3)):
                coverage = survivorship_coverage(
                    filer_index, symbols, args.end, has_prices
                )
            lines = coverage.lines()
        except TimeBudgetExceeded:
            return [
                "SURVIVORSHIP COVERAGE not computed: the run reached its time "
                "budget. Treat the CAGR as an upper bound."
            ]
        if unchecked:
            lines.append(
                f"  {len(unchecked)} companies were not checked for prices before "
                "the time budget ran out and are counted as unpriced above."
            )
        return lines

    if args.strategy == "factor":
        return run_factor(
            args, adapter, config, settings, split, symbols, eligibility,
            peer_pool, member_cache, benchmark_text, minutes, until_end_less,
            survivorship_lines if filer_index is not None else None,
        )

    deadline = until_end_less(25)
    primary = Backtester(
        adapter, config, settings, symbols, label="GCFP v4", split=split,
        eligibility=eligibility, peer_pool=peer_pool,
        member_cache=member_cache,
    ).run(progress=args.progress, deadline=deadline)
    # Each benchmark replays the same months over the same companies, so the
    # primary run is a fair estimate of how long one takes.
    run_minutes = max(minutes() - primary_started, 0.1)
    phase_minutes: dict[str, float] = {
        "setup (filing index, sample)": primary_started,
        "main backtest": run_minutes,
    }
    benchmarks_started = minutes()
    print(f"  main backtest done in {run_minutes:.0f} min", file=sys.stderr)

    # C2 options, run on the same companies and months: what changes if the
    # peer rule is loosened (option 2) or allowed to widen to the industry
    # group (option 3). Neither is the spec; they are measured, not adopted.
    option_lines: list[str] = []
    if args.c2_options and primary.stopped_early_at is None:
        import dataclasses

        def with_anchors(**changes):
            return dataclasses.replace(
                config, anchors=dataclasses.replace(config.anchors, **changes)
            )

        wider_bands = dict(
            peer_market_cap_low=0.2, peer_market_cap_high=5.0, peer_growth_band=0.25
        )
        options = [
            ("Option 2: wider peer bands (0.2-5x size, +/-25pt growth)",
             with_anchors(**wider_bands)),
            ("Option 3: wider industry group when fewer than 4 peers",
             with_anchors(peer_group_fallback=True)),
            ("Options 2 + 3 together",
             with_anchors(peer_group_fallback=True, **wider_bands)),
        ]

        def describe(name: str, run) -> str:
            summary = summarise(name, run.equity_curve, run.book.closed,
                                run.benchmark_curve)
            single = run.single_anchor_rate
            diverge = run.divergence_rate
            return (
                f"  {name}: "
                + (f"{summary.annualised:+.2%}/yr" if summary.annualised is not None
                   else "return not measurable")
                + f" · {len(run.book.closed)} trades"
                + (f" · single-anchor {single:.0%}" if single is not None else "")
                + (f" · anchors disagree {diverge:.0%}" if diverge is not None else "")
            )

        option_lines = [
            "C2 OPTIONS COMPARED (same companies and months; only the peer rule differs)",
            describe("As specified, all same-industry peers offered", primary),
        ]
        for name, option_config in options:
            print(f"  {name} ({minutes():.0f} min elapsed)", file=sys.stderr)
            run = Backtester(
                adapter, option_config, settings, symbols, label=name,
                split=split, eligibility=eligibility, peer_pool=peer_pool,
                member_cache=member_cache,
            ).run(progress=args.progress, deadline=until_end_less(15))
            if run.stopped_early_at is not None:
                option_lines.append(f"  {name}: not finished inside the time budget")
                break
            option_lines.append(describe(name, run))

    # §13.4 benchmarks.
    benchmarks = []
    skipped: list[str] = []
    if not args.no_benchmarks:
        for variant in build_variants(config, settings.benchmark_symbol):
            if not variant.is_passive and (
                # A benchmark over the full period would not be comparable
                # with a main run that stopped early.
                primary.stopped_early_at is not None
                or (
                    args.time_budget_min is not None
                    and minutes() + run_minutes > args.time_budget_min
                )
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
                member_cache=member_cache,
            ).run(progress=args.progress, deadline=until_end_less(15))
            if run.stopped_early_at is not None:
                # Some benchmarks trade far more than the main run, so the
                # main run's duration does not bound theirs. A benchmark cut
                # short covers a different period and is not comparable.
                skipped.append(variant.name)
                continue
            benchmarks.append(
                (variant.name, variant.proves,
                 summarise(variant.name, run.equity_curve, run.book.closed,
                           run.benchmark_curve))
            )

    phase_minutes["benchmarks"] = minutes() - benchmarks_started
    accuracy_started = minutes()
    # §13.5 classification accuracy, over every routing the run recorded.
    routings = [
        (symbol, Classification(tag), record.as_of)
        for record in primary.rebalances
        for symbol, tag in record.classifications.items()
    ]
    print(f"  classification accuracy: {len(routings)} routings "
          f"({minutes():.0f} min elapsed)", file=sys.stderr)
    accuracy_note = None
    try:
        with time_limit(until_end_less(7)):
            accuracy = measure_accuracy(
                adapter, config, routings, horizon_end=args.end,
                deadline=until_end_less(8),
            )
    except TimeBudgetExceeded:
        accuracy = None
        accuracy_note = (
            "§13.5 classification accuracy NOT measured: the run reached its "
            "time budget during the check."
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
                member_cache=member_cache,
            ).run(),
            config,
            split=split,
            period_start=split.train_start,
            period_end=split.train_end,
            progress=args.progress,
        )

    if skipped:
        print(f"  skipped for time: {', '.join(skipped)}", file=sys.stderr)

    phase_minutes["accuracy check"] = minutes() - accuracy_started
    gap_totals: dict[str, int] = {}
    c1_missing_by_year: dict[int, list[int]] = {}
    for record in primary.rebalances:
        evaluated_with_anchors = (
            record.single_anchor_candidates + record.dual_anchor_candidates
        )
        year = record.as_of.year
        c1_gaps = sum(n for g, n in record.anchor_gaps.items() if g.startswith("C1"))
        tally = c1_missing_by_year.setdefault(year, [0, 0])
        tally[0] += c1_gaps
        tally[1] += evaluated_with_anchors
        for gap, count in record.anchor_gaps.items():
            gap_totals[gap] = gap_totals.get(gap, 0) + count
    anchor_lines = []
    if gap_totals:
        anchor_lines.append("WHY CANDIDATES WERE NOT VALUED BOTH WAYS (counts over all months)")
        anchor_lines += [
            f"  {count:5d}  {gap}"
            for gap, count in sorted(gap_totals.items(), key=lambda kv: -kv[1])[:12]
        ]
        anchor_lines.append("  C1 (own history) missing, by year:")
        anchor_lines += [
            f"    {year}: {missing} of {total} candidates"
            for year, (missing, total) in sorted(c1_missing_by_year.items())
            if total
        ]
    funnel_totals: dict[str, int] = {}
    for record in primary.rebalances:
        for step, count in record.funnel.items():
            funnel_totals[step] = funnel_totals.get(step, 0) + count
    screened_total = sum(r.evaluated for r in primary.rebalances)
    funnel_lines = []
    if funnel_totals:
        funnel_lines = [
            f"WHERE CANDIDATES DROPPED OUT ({screened_total} screenings over "
            f"{len(primary.rebalances)} months; first blocking cause)"
        ] + [
            f"  {count:6d}  {step}"
            for step, count in sorted(funnel_totals.items(), key=lambda kv: (kv[0][:1], -kv[1]))
            if count >= 3
        ] + [
            f"  BUY signals: {sum(r.passers for r in primary.rebalances)} · "
            f"positions actually bought: {sum(len(r.buys) for r in primary.rebalances)}"
        ]
    timing_lines = funnel_lines + anchor_lines + ["RUN TIMING (minutes)"] + [
        f"  {name}: {value:.1f}" for name, value in phase_minutes.items()
    ] + [
        f"    main backtest, {stage}: {seconds / 60:.1f}"
        for stage, seconds in primary.timing.items()
    ]
    notes = list(primary.notes) + option_lines + ([accuracy_note] if accuracy_note else []) + [
        "Simulated fills assume the operator transacts at the rebalance "
        "close. That is optimistic about liquidity and is stated rather "
        "than modelled away.",
        "Money not in stock picks (the BALLAST bucket, plus any sleeve F3 "
        f"leaves unfilled) follows {benchmark_text}; the index benchmark is "
        "the same index.",
        "Holdings are credited their cash dividends on each ex-date, from the "
        "price feed's dividend history. A holding whose dividends could not "
        "be fetched is counted price-only.",
        *(
            ["EVALUATIONS THAT RAISED (each was skipped; a code fault, not a data gap):"]
            + [f"  {count:5d}  {cause}" for cause, count in sorted(
                primary.crashes.items(), key=lambda kv: -kv[1])[:15]]
            if primary.crashes else
            ["No evaluation raised an error."]
        ),
        f"Holdings whose price stopped and were closed as delisted at "
        f"{settings.delisting_return:+.0%}: {primary.assumed_delistings}.",
    ]
    if skipped:
        notes.append(
            "Benchmarks NOT run or cut short, to finish inside the time budget: "
            + ", ".join(skipped)
            + ". Their comparisons are missing from this report, not passed."
        )
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

    def write_report(extra: list[str]) -> str:
        report = ValidationReport(
            primary=primary,
            benchmarks=benchmarks,
            accuracy=accuracy,
            sweep=sweep,
            # EDGAR is point-in-time by construction; the synthetic fixture
            # models the same filing-date discipline.
            point_in_time=True,
            paper_traded_months=0.0,
            notes=notes + extra + timing_lines
            + [f"  report written at: {minutes():.1f}"],
        )
        text = report.render(config)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text)
            _REPORT_WRITTEN.set()
            print(f"  report written to {args.out} ({minutes():.0f} min elapsed)",
                  file=sys.stderr, flush=True)
        return text

    # Written before the survivorship check and rewritten after it, so the
    # results are on disk even if that check is what runs out of time.
    text = write_report(
        ["SURVIVORSHIP COVERAGE: not yet computed when this report was written."]
        if filer_index is not None else []
    )
    if filer_index is not None:
        coverage_lines = survivorship_lines()
        text = write_report(coverage_lines)
    print(text, flush=True)

    integrity = evaluate_break_criteria(
        config,
        closed_positions=len(primary.book.closed),
        classification_accuracy=accuracy.accuracy if accuracy else None,
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
