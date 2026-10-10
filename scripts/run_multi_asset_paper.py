#!/usr/bin/env python3
"""Paper trading of MA, the multi-asset trend portfolio, with the margin alarm.

    python scripts/run_multi_asset_paper.py

Fetches daily closes for ^SP500TR, EFA, IEF and GLD (dividend-adjusted
where they pay) and the T-bill yield, steps the paper book
(gcfp/backtest/multi_asset_paper.py) and writes:

  reports/paper/multi-asset-book.json     the book, carried run to run
  reports/paper/multi-asset-report.txt    sleeves, value and margin
  reports/paper/leverage-alert.txt        first line OK, WARNING, URGENT,
                                          MARGIN CALL LEVEL or CHECK FAILED;
                                          the daily check emails anything not OK

Nothing here places an order.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gcfp.backtest import multi_asset_paper as map_

CONFIG = Path(__file__).resolve().parent / "leverage_config.json"
BOOK = Path("reports/paper/multi-asset-book.json")
REPORT = Path("reports/paper/multi-asset-report.txt")
ALERT = Path("reports/paper/leverage-alert.txt")


def fetch(today: date):
    from gcfp.data.prices import YahooPriceSource

    source = YahooPriceSource()
    still_open = datetime.utcnow().hour < 21  # today's bar is a live quote
    closes = {}
    for name, symbol in map_.ASSETS:
        points = source.get_prices(symbol, today - timedelta(days=430), today)
        closes[name] = [(p.price_date, p.adjusted_close or p.close) for p in points
                        if not (still_open and p.price_date >= today)]
    try:
        rf = source.get_prices("^IRX", today - timedelta(days=14), today)[0].close / 100
    except Exception:
        rf = 0.04
    return closes, rf


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--today", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    args = parser.parse_args(argv)
    today = args.today or date.today()
    config = json.loads(CONFIG.read_text())["multi_asset"]
    closes, rf = fetch(today)
    state = map_.load(BOOK)
    state, changes = map_.step(state, closes, rf, today, float(config["leverage"]),
                               float(config["paper_capital"]))
    map_.save(state, BOOK)
    text, alert = map_.report(state, closes, rf, today,
                              float(config["maintenance_margin"]), changes)
    REPORT.write_text(text)
    ALERT.write_text(alert)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
