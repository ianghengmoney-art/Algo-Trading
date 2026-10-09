#!/usr/bin/env python3
"""Paper trading of the leveraged trend strategy, with the margin alarm.

    python scripts/run_leverage_paper.py

Fetches S&P 500 total-return closes and the T-bill yield from Yahoo, steps
the paper book (gcfp/backtest/leverage_paper.py), and writes:

  reports/paper/leverage-2x-book.json    the book, carried run to run
  reports/paper/leverage-2x-report.txt   the position, value and margin
  reports/paper/leverage-alert.txt       first line OK, WARNING, URGENT or
                                         MARGIN CALL LEVEL; read by the daily
                                         check, which emails anything not OK

Run by the screen job (daily and weekly). Nothing here places an order.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gcfp.backtest import leverage_paper as lp

CONFIG = Path(__file__).resolve().parent / "leverage_config.json"
BOOK = Path("reports/paper/leverage-2x-book.json")
REPORT = Path("reports/paper/leverage-2x-report.txt")
ALERT = Path("reports/paper/leverage-alert.txt")
INDEX = "^SP500TR"
TBILL = "^IRX"  # 13-week T-bill yield, in percent


def fetch(today: date) -> tuple[list[tuple[date, float]], float]:
    from gcfp.data.prices import YahooPriceSource

    source = YahooPriceSource()
    points = source.get_prices(INDEX, today - timedelta(days=420), today)
    closes = [(p.price_date, p.close) for p in points]
    try:
        bills = source.get_prices(TBILL, today - timedelta(days=14), today)
        rf = bills[0].close / 100.0  # newest first
    except Exception:
        rf = 0.04  # stated fallback; financing is a small part of a month
    return closes, rf


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--today", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    args = parser.parse_args(argv)
    today = args.today or date.today()
    config = json.loads(CONFIG.read_text())
    closes, rf = fetch(today)
    state = lp.load(BOOK)
    state, instruction = lp.step(state, closes, rf, today, float(config["leverage"]),
                                 float(config["paper_capital"]))
    lp.save(state, BOOK)
    text, alert = lp.report(state, closes, rf, today,
                            float(config["maintenance_margin"]), instruction)
    REPORT.write_text(text)
    ALERT.write_text(alert)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
