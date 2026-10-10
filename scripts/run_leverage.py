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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
