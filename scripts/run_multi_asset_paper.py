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


ETF_BOOK = Path("reports/paper/etf-sgd-book.json")


def run_etf_book(state, cfg: dict, today: date) -> tuple[str, list[str]]:
    """The SGD account's ETF book on MA's signals, and the futures account's
    whole-contract ticket. Returns the report text and any ETF trades."""
    from gcfp.backtest import etf_paper
    from gcfp.backtest.etf_version import ETFS
    from gcfp.data.prices import YahooPriceSource

    source = YahooPriceSource()
    still_open = datetime.utcnow().hour < 21
    symbols = [etf for etf, _, _ in ETFS.values()] + [etf_paper.CASH_ETF]
    closes = {}
    for sym in symbols:
        pts = source.get_prices(sym, today - timedelta(days=120), today)
        closes[sym] = [(p.price_date, p.adjusted_close or p.close) for p in pts
                       if not (still_open and p.price_date >= today)]
    day = min(max(d for d, _ in c) for c in closes.values())
    currency = cfg.get("currency", "SGD")
    fx = source.get_prices(f"{currency}=X", today - timedelta(days=10), today)[0].close
    book = etf_paper.load(ETF_BOOK)
    book, trades = etf_paper.step(book, closes, state.in_trend, state.signal_month, day,
                                  fx, currency, float(cfg["amount"]),
                                  float(cfg.get("order_cost_usd", 1.0)))
    etf_paper.save(book, ETF_BOOK)
    text = etf_paper.report(book, closes, day, fx, trades)

    prices = {}
    for sleeve, (symbol, _, _) in etf_paper.CONTRACTS.items():
        try:
            prices[symbol] = source.get_prices(symbol, today - timedelta(days=10), today)[0].close
        except Exception:
            pass
    equity = state.value  # at the last rebalance, when contracts are sized
    text += etf_paper.contract_ticket(state.in_trend, equity, state.leverage, prices)
    return text, trades


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
    etf_cfg = json.loads(CONFIG.read_text()).get("etf_book")
    if etf_cfg:
        failed = False
        try:
            extra, trades = run_etf_book(state, etf_cfg, today)
        except Exception as exc:
            extra, trades = (f"\nETF BOOK not updated: {type(exc).__name__}: {exc}\n", [])
            failed = True
        text += extra
        alert += extra
        lines = alert.splitlines()
        if failed:
            # The daily check emails anything not OK: a broken ETF book is seen.
            lines[0] = "CHECK FAILED"
        if trades and not changes:
            # An ETF resize with no MA signal change still needs acting on.
            lines.insert(1, "POSITION CHANGE: ETF account trades")
        alert = "\n".join(lines) + "\n"
    REPORT.write_text(text)
    ALERT.write_text(alert)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
