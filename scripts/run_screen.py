#!/usr/bin/env python3
"""Run the weekly screen and write the new-passer report.

    # free stack — SEC EDGAR + a free price feed, no key, no subscription
    python scripts/run_screen.py --user-agent "your-name you@example.com"

    # limit the universe while trying it out; a full build touches every filer
    python scripts/run_screen.py --user-agent "..." --limit 200

    # offline demo against fixtures
    python scripts/run_screen.py --source fixture

Nothing here executes a trade. The output is a file and a console dump, which
is the only supported mode.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gcfp.config import DEFAULT_CONFIG
from gcfp.modules.f_sizing import Bucket, PortfolioState
from gcfp.pipeline import CandidateInputs, run_screen, sleeve_passer_counts
from gcfp.report import new_passer_report, write_report
from gcfp.runner import (
    assemble,
    build_free_adapter,
    build_inputs,
    free_stack_config,
    load_or_build_universe,
)
from gcfp.storage import Store
from gcfp.universe import build_universe


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default="edgar", choices=("edgar", "fixture"))
    parser.add_argument(
        "--user-agent",
        help='Required for EDGAR. SEC policy: identify yourself, e.g. "jane jane@example.com"',
    )
    parser.add_argument(
        "--limit", type=int,
        help=(
            "screen a random sample of N filers instead of all of them. The "
            "sample is seeded, so two runs of the same size cover the same "
            "names. Omit it for the whole market — slower, and the only way "
            "to read the rejection rates as describing the market."
        ),
    )
    parser.add_argument("--symbols", nargs="*", help="screen only these tickers")
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--universe-cache", type=Path, default=Path(".cache/universe.json"))
    parser.add_argument("--rebuild-universe", action="store_true")
    parser.add_argument("--db", type=Path, default=Path("gcfp.sqlite"))
    parser.add_argument("--reports", type=Path, default=Path("reports"))
    parser.add_argument("--portfolio-value", type=float, default=100_000.0)
    parser.add_argument("--usdsgd", type=float, default=None,
                        help="USD/SGD spot; omit to report in listing currency only")
    parser.add_argument(
        "--symbols-out", type=Path, metavar="FILE",
        help=(
            "write every screened ticker here, one per line, for "
            "run_backtest.py --symbols-from. The file carries a survivorship "
            "warning as a comment, because a list drawn today holds only "
            "companies that still exist."
        ),
    )
    parser.add_argument("--progress", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.source == "edgar":
        if not args.user_agent:
            raise SystemExit(
                "EDGAR requires --user-agent identifying you with contact "
                'details, e.g. --user-agent "jane jane@example.com". '
                "The SEC blocks anonymous scrapers."
            )
        adapter = build_free_adapter(args.user_agent, cache_dir=args.cache_dir)
        config = free_stack_config(DEFAULT_CONFIG)
        universe = load_or_build_universe(
            adapter, config, args.universe_cache,
            limit=args.limit, force=args.rebuild_universe, progress=args.progress,
        )
    else:
        from gcfp.fixtures_probe import build_probe_fixture

        adapter = build_probe_fixture()
        config = DEFAULT_CONFIG
        universe = build_universe(adapter, sorted(adapter.companies), config)

    # A fresh book: everything in ballast until something passes.
    portfolio = PortfolioState(
        total_value=args.portfolio_value,
        bucket_values={
            Bucket.BALLAST: args.portfolio_value,
            Bucket.CORE_PICKS: 0.0,
            Bucket.GROWTH_PICKS: 0.0,
        },
    )

    context = assemble(
        adapter, config, universe, portfolio,
        fx_rates={"USDSGD": args.usdsgd} if args.usdsgd else None,
    )

    symbols = args.symbols or context.candidate_symbols
    if args.progress:
        print(f"screening {len(symbols)} names...", file=sys.stderr)

    inputs = build_inputs(adapter, universe, config, symbols)
    evaluations = run_screen(
        adapter, symbols, context.market, config, inputs, portfolio, context.fx
    )

    # Instrumentation: without it, "zero passers" is ambiguous between a
    # disciplined screen and a broken parser.
    from gcfp.diagnostics import ScreenDiagnostics
    from gcfp.sensitivity import SensitivitySummary, analyse

    diagnostics = ScreenDiagnostics()
    if args.limit:
        # Say what fraction of the market this run actually looked at, so a
        # sample's rejection rates are never read as the market's.
        try:
            from gcfp.universe import default_symbol_list

            diagnostics.rejections.universe_total = len(
                default_symbol_list(adapter)
            )
        except Exception:
            pass
    for evaluation in evaluations:
        diagnostics.record(evaluation, evaluation.peer_decisions, config)
    for symbol in universe.unreachable:
        diagnostics.rejections.record_unreachable(
            symbol.split(":")[0], symbol
        )

    # Stress-test the valuations that actually produced a verdict.
    sensitivity = SensitivitySummary()
    for evaluation in evaluations:
        if evaluation.fair_value is None or evaluation.classification is None:
            continue
        price = (
            evaluation.signal.values.get("price") if evaluation.signal else None
        )
        try:
            data = adapter.load_company(evaluation.symbol)
        except Exception:
            continue
        sensitivity.record(
            analyse(
                data, context.market, config, evaluation.classification,
                evaluation.fair_value, price=price,
                valuation_kwargs={
                    "sector_price_to_book": inputs.get(
                        evaluation.symbol, CandidateInputs()
                    ).sector_price_to_book,
                    "peer_price_to_affo": inputs.get(
                        evaluation.symbol, CandidateInputs()
                    ).peer_price_to_affo,
                },
            )
        )

    from gcfp.modules.k_currency import exposure_report

    # K4 exposure over the names that passed, weighted by intended size so the
    # report shows the currency mix the operator would actually take on.
    passers = [
        (adapter.get_profile(e.symbol), e.sizing.amount_base_currency)
        for e in evaluations
        if e.is_buy and e.sizing is not None and e.sizing.qualified
    ]
    exposure = exposure_report(passers, config) if passers else None

    text = new_passer_report(
        evaluations, portfolio, context.fx, config, exposure,
        diagnostics=diagnostics, sensitivity=sensitivity,
    )
    print(text)

    path = write_report(text, args.reports, "new-passers")
    print(f"\nwritten to {path}", file=sys.stderr)

    if args.symbols_out:
        args.symbols_out.parent.mkdir(parents=True, exist_ok=True)
        args.symbols_out.write_text(
            "# Screened by GCFP v4 on "
            f"{date.today().isoformat()} — {len(symbols)} names.\n"
            "#\n"
            "# SURVIVORSHIP WARNING. Every ticker here exists today. A\n"
            "# backtest over this list cannot lose money in a company that\n"
            "# went to zero, because no such company is in it, and it will\n"
            "# therefore report the returns of survival rather than of the\n"
            "# strategy. Add delisted names by CIK (EdgarAdapter accepts\n"
            "# 'CIK0000719739' and a cik_overrides map) before believing any\n"
            "# number a backtest over this file produces.\n"
            + "\n".join(sorted(symbols))
            + "\n"
        )
        print(f"symbol list written to {args.symbols_out}", file=sys.stderr)

    # F3: a thin sleeve holds the gap in ballast rather than concentrating.
    from gcfp.modules.f_sizing import below_minimum_handling

    for regime, count in sleeve_passer_counts(evaluations).items():
        message = below_minimum_handling(count, regime, config)
        if message:
            print(f"\n{message}", file=sys.stderr)

    store = Store(args.db)
    for evaluation in evaluations:
        store.record_audit(evaluation.ledger)
        if evaluation.conviction is not None:
            store.record_conviction(
                evaluation.conviction, evaluation.anchor_mode.value, config.fingerprint
            )
        if evaluation.signal is not None:
            store.record_alert(evaluation.signal, config.fingerprint)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
