#!/usr/bin/env python3
"""The leveraged trend strategy test (docs/LEVERAGE_STRATEGY.md).

    python scripts/run_leverage.py

Downloads Kenneth French's US market and T-bill returns (1926 onward),
applies the registered rules and writes reports/leverage.txt. The backtest
job runs it when scripts/backtest_defaults.json sets "leverage_only": true,
because French's site is reachable from GitHub Actions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gcfp.backtest import leverage

DEFAULT_OUT = Path("reports/leverage.txt")


def main(argv=None) -> int:
    from run_long_history import http_get

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    args = parser.parse_args(argv)
    text, _ = leverage.run(http_get, args.cache_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    print(text)

    # Crash protection on daily data (registered after the monthly result).
    from gcfp.backtest import leverage_daily

    daily_text, _ = leverage_daily.run(http_get, args.cache_dir)
    daily_out = args.out.with_name("leverage-daily.txt")
    daily_out.write_text(daily_text)
    print(daily_text)

    # The growth-optimal (Kelly) leverage: a diagnostic, not a rule.
    from gcfp.backtest import leverage_sweep

    sweep_text = leverage_sweep.run(http_get, args.cache_dir)
    args.out.with_name("leverage-kelly.txt").write_text(sweep_text)
    print(sweep_text)

    # Improvements to L2 (registered 2026-10-10): futures costs, ensemble signal.
    from gcfp.backtest import leverage_improve

    improve_text, _ = leverage_improve.run(http_get, args.cache_dir)
    args.out.with_name("leverage-improve.txt").write_text(improve_text)
    print(improve_text)

    # MA, the multi-asset trend portfolio (registered 2026-10-10).
    from gcfp.backtest import multi_asset

    try:
        ma_text, _ = multi_asset.run(http_get, args.cache_dir)
    except Exception as exc:
        import traceback

        traceback.print_exc()
        ma_text = f"MA multi-asset test could not run: {type(exc).__name__}: {exc}\n"
    args.out.with_name("leverage-multi-asset.txt").write_text(ma_text)
    print(ma_text)

    # MA's daily crash check (registered 2026-10-10).
    from gcfp.backtest import multi_asset_daily

    try:
        daily_ma_text, _ = multi_asset_daily.run(http_get, args.cache_dir)
    except Exception as exc:
        import traceback

        traceback.print_exc()
        daily_ma_text = f"MA daily crash check could not run: {type(exc).__name__}: {exc}\n"
    args.out.with_name("leverage-ma-daily.txt").write_text(daily_ma_text)
    print(daily_ma_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
