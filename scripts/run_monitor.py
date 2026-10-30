#!/usr/bin/env python3
"""Re-run the holdings monitor and write the health report.

    python scripts/run_monitor.py --user-agent "your-name you@example.com"

Cadence follows each holding's own reporting frequency (H1), so running this
daily is cheap: positions with nothing newly filed are checked and skipped.
Run it daily and it will do the right thing on the day each holding reports.

Nothing here executes a trade.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gcfp.config import DEFAULT_CONFIG
from gcfp.modules.f_sizing import Bucket, PortfolioState
from gcfp.monitor import discipline_report, load_holdings, run_monitor
from gcfp.report import holdings_health_report, write_report
from gcfp.runner import assemble, build_free_adapter, build_inputs, free_stack_config, load_or_build_universe
from gcfp.storage import Store
from gcfp.universe import build_universe


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default="edgar", choices=("edgar", "fixture"))
    parser.add_argument("--user-agent", help="required for EDGAR; SEC policy")
    parser.add_argument("--db", type=Path, default=Path("gcfp.sqlite"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--universe-cache", type=Path, default=Path(".cache/universe.json"))
    parser.add_argument("--reports", type=Path, default=Path("reports"))
    parser.add_argument("--portfolio-value", type=float, default=100_000.0)
    parser.add_argument("--usdsgd", type=float, default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="review without writing conviction or audit rows")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    store = Store(args.db)

    holdings = load_holdings(store)
    if not holdings:
        print(
            "No open positions in the store. Nothing to monitor.\n"
            "Positions are added when you act on a recommendation — see "
            "Module G's record in the README.",
            file=sys.stderr,
        )
        return 0

    if args.source == "edgar":
        if not args.user_agent:
            raise SystemExit(
                'EDGAR requires --user-agent, e.g. "jane jane@example.com"'
            )
        adapter = build_free_adapter(args.user_agent, cache_dir=args.cache_dir)
        config = free_stack_config(DEFAULT_CONFIG)
        universe = load_or_build_universe(adapter, config, args.universe_cache)
    else:
        from gcfp.fixtures_probe import build_probe_fixture

        adapter = build_probe_fixture()
        config = DEFAULT_CONFIG
        universe = build_universe(adapter, sorted(adapter.companies), config)

    # The book, as the operator holds it.  Positions carry their cost until a
    # live valuation replaces it, which is enough for the caps F and G enforce.
    position_values = {h.symbol: h.cost_base_currency for h in holdings}
    invested = sum(position_values.values())
    portfolio = PortfolioState(
        total_value=max(args.portfolio_value, invested),
        bucket_values={
            Bucket.BALLAST: max(args.portfolio_value - invested, 0.0),
            Bucket.CORE_PICKS: invested,
            Bucket.GROWTH_PICKS: 0.0,
        },
        position_values=position_values,
        position_regimes={
            h.symbol: __import__(
                "gcfp.classification", fromlist=["regime_of"]
            ).regime_of(h.classification)
            for h in holdings
        },
    )

    context = assemble(
        adapter, config, universe, portfolio,
        fx_rates={"USDSGD": args.usdsgd} if args.usdsgd else None,
    )
    inputs = build_inputs(
        adapter, universe, config, [h.symbol for h in holdings]
    )

    reviews = run_monitor(
        store, adapter, context.market, config, portfolio, context.fx, inputs,
        persist=not args.dry_run,
    )
    discipline = discipline_report(reviews, portfolio, config)

    text = holdings_health_report(
        [r.verdict for r in reviews], discipline, portfolio, context.fx, config
    )
    print(text)

    # Per-holding detail below the summary, so cadence and conviction change
    # are visible without opening the database.
    print("\nPER-HOLDING DETAIL")
    for review in reviews:
        for line in review.as_report_lines():
            print(f"  {line}")

    path = write_report(text, args.reports, "holdings-health")
    print(f"\nwritten to {path}", file=sys.stderr)

    attention = [r for r in reviews if r.needs_attention]
    if attention:
        print(
            f"\n{len(attention)} position(s) need attention: "
            + ", ".join(r.holding.symbol for r in attention),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
