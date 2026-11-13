#!/usr/bin/env python3
"""The long-history premise check (docs/FACTOR_STRATEGY.md).

    python scripts/run_long_history.py

Downloads Kenneth French's portfolio returns (1963 onward), forms the
registered proxies, and writes reports/long-history.txt. The factor backtest
job runs this first, so it needs no workflow change.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gcfp.backtest import longhistory

DEFAULT_OUT = Path("reports/long-history.txt")


def http_get(url: str) -> bytes:
    import requests

    resp = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0 (gcfp research)"})
    resp.raise_for_status()
    return resp.content


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    args = parser.parse_args(argv)
    text, _ = longhistory.run(http_get, args.cache_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
