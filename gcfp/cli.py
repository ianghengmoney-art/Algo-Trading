"""Command-line entry point.

Runs manually or on a schedule. Generates reports; a human reads them and
decides. That is the intended and only supported mode.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Sequence

from .config import DEFAULT_PARAMS, Params
from .data.base import DataUnavailable
from .data.coverage import (
    DEFAULT_PROBE_TARGETS,
    FIXTURE_PROBE_TARGETS,
    render_coverage_report,
    run_coverage_probe,
)
from .data.registry import available, build, capability_gate
from .engine import screen
from .modules.f_sizing import Portfolio, Position, allocate
from .modules.g_execution import RecommendationRecord, log_execution
from .reports.render import weekly_report, write_report
from .state.db import Store


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _load_portfolio(path: Optional[str]) -> Optional[Portfolio]:
    """Portfolio JSON: {total_value, ballast_value, positions: [...]}"""
    if not path:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Portfolio(
        total_value=float(data["total_value"]),
        ballast_value=float(data.get("ballast_value", 0.0)),
        positions=[
            Position(
                symbol=p["symbol"],
                classification=p["classification"],
                sector=p.get("sector"),
                cost_value=float(p["cost_value"]),
                current_value=float(p.get("current_value", p["cost_value"])),
            )
            for p in data.get("positions", [])
        ],
    )


def cmd_capabilities(args: argparse.Namespace, params: Params) -> int:
    adapter = build(args.adapter)
    gate = capability_gate(adapter)
    print(f"adapter: {adapter.name}")
    print("capabilities:")
    for capability in sorted(c.value for c in adapter.capabilities()):
        print(f"  - {capability}")
    notices = gate.blocking_notices()
    if notices:
        print("\nblocking notices:")
        for notice in notices:
            print(f"  !! {notice}")
    return 0


def cmd_coverage(args: argparse.Namespace, params: Params) -> int:
    adapter = build(args.adapter)
    targets = FIXTURE_PROBE_TARGETS if args.adapter == "fixtures" else DEFAULT_PROBE_TARGETS
    if args.symbols:
        from .data.coverage import ProbeTarget

        targets = tuple(
            ProbeTarget(*pair.split(":", 1)) for pair in args.symbols
        )
    report = run_coverage_probe(adapter, params, targets, as_of=_parse_date(args.as_of))
    text = render_coverage_report(report)
    print(text)
    if args.out:
        path = write_report(text, args.out, f"coverage-{report.generated_on.isoformat()}.txt")
        print(f"\nwritten to {path}")
    return 0 if not report.missing() else 2


def cmd_screen(args: argparse.Namespace, params: Params) -> int:
    adapter = build(args.adapter)
    symbols = args.symbols
    if not symbols:
        if hasattr(adapter, "symbols"):
            symbols = [s for s in adapter.symbols() if not s[:3] in {"IND", "TCH", "BNK", "RET", "INS", "SML", "MEG"}]
        else:
            print("no symbols supplied; pass --symbols", file=sys.stderr)
            return 1

    gate = capability_gate(
        adapter, multiple_history_years=args.multiple_history_years, symbols=symbols
    )
    as_of = _parse_date(args.as_of) or date.today()
    result = screen(adapter, symbols, params, gate, as_of=as_of)

    portfolio = _load_portfolio(args.portfolio)
    allocator = None
    if portfolio is not None:
        allocator = allocate(
            [
                (a.symbol, a.classification, a.candidate.profile.sector, a.conviction.total)
                for a in result.passers()
                if a.classification and a.conviction and a.candidate
            ],
            portfolio,
            params,
        )

    text = weekly_report(result, params, allocator, portfolio)
    print(text)

    if args.out:
        path = write_report(text, args.out, f"weekly-{as_of.isoformat()}.txt")
        print(f"\nwritten to {path}")

    if args.db:
        store = Store(args.db)
        store.record_run(
            as_of, adapter.name, params.revision, len(symbols), len(result.passers()),
            result.header_notices,
        )
        for assessment in result.assessments:
            if assessment.conviction:
                store.record_conviction(
                    assessment.symbol, as_of, assessment.conviction.total,
                    assessment.conviction.breakdown(),
                )
            if assessment.trigger and assessment.trigger.action != "NO ACTION":
                store.record_alert(
                    as_of, assessment.symbol, assessment.trigger.action,
                    assessment.classification,
                    assessment.conviction.total if assessment.conviction else None,
                    assessment.fair_value,
                    assessment.candidate.profile.price if assessment.candidate else None,
                    deferred=assessment.trigger.deferred,
                    detail=assessment.trigger.conditions,
                )
        store.close()
    return 0


def cmd_record(args: argparse.Namespace, params: Params) -> int:
    """Write the immutable recommendation record, thesis included."""
    store = Store(args.db)
    record = RecommendationRecord(
        symbol=args.symbol,
        recommended_on=_parse_date(args.on) or date.today(),
        classification=args.classification,
        conviction_score=args.conviction,
        fair_value_per_share=args.fair_value,
        valuation_method=args.method,
        own_history_reading_pct=args.own_history,
        peer_reading_pct=args.peer,
        intended_sleeve_pct=args.sleeve_pct,
        intended_value=args.intended_value,
        currency=args.currency,
        thesis_invalidation=args.thesis,
    )
    store.record_recommendation(record)
    store.close()
    print(f"recorded intent for {record.symbol}: {record.thesis_invalidation}")
    return 0


def cmd_log_execution(args: argparse.Namespace, params: Params) -> int:
    store = Store(args.db)
    rows = store.recommendations(args.symbol)
    if not rows:
        print(f"no recommendation on file for {args.symbol}", file=sys.stderr)
        store.close()
        return 1
    row = rows[0]
    record = RecommendationRecord(
        symbol=row["symbol"],
        recommended_on=_parse_date(row["recommended_on"]),
        classification=row["classification"],
        conviction_score=row["conviction_score"],
        fair_value_per_share=row["fair_value_per_share"],
        valuation_method=row["valuation_method"],
        own_history_reading_pct=row["own_history_reading_pct"],
        peer_reading_pct=row["peer_reading_pct"],
        intended_sleeve_pct=row["intended_sleeve_pct"],
        intended_value=row["intended_value"],
        currency=row["currency"],
        thesis_invalidation=row["thesis_invalidation"],
    )
    try:
        execution = log_execution(
            record, _parse_date(args.on) or date.today(), args.actual_value, params, args.reason
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        store.close()
        return 1
    store.record_execution(record.recommended_on, execution)
    total = store.size_deviation_count()
    store.close()
    marker = " -- SIZE DEVIATION (permanent)" if execution.size_deviation else ""
    print(
        f"{record.symbol}: intended {record.intended_value:,.0f} -> actual "
        f"{execution.actual_value:,.0f} ({execution.deviation_pct:+.1f}%){marker}"
    )
    print(f"portfolio SIZE DEVIATION count: {total}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gcfp",
        description="GCFP v3 -- unified adaptive value framework. Alert only; never executes.",
    )
    parser.add_argument("--adapter", default="fixtures", choices=available())
    parser.add_argument("--db", default=None, help="path to the SQLite state file")

    # The same two options are accepted after the subcommand as well, which is
    # where a reader of the examples will naturally type them. SUPPRESS keeps
    # the subparser from clobbering a value given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--adapter", choices=available(), default=argparse.SUPPRESS,
    )
    common.add_argument("--db", default=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "capabilities", parents=[common],
        help="show what the data source can actually serve",
    )
    p.set_defaults(func=cmd_capabilities)

    p = sub.add_parser("coverage", parents=[common], help="run the Section 17 data coverage audit")
    p.add_argument("--symbols", nargs="*", help="SYMBOL:CLASSIFICATION pairs to probe")
    p.add_argument("--as-of", default=None)
    p.add_argument("--out", default=None, help="directory to write the report into")
    p.set_defaults(func=cmd_coverage)

    p = sub.add_parser("screen", parents=[common], help="run the weekly new-passer screen")
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--as-of", default=None)
    p.add_argument("--portfolio", default=None, help="portfolio JSON for sizing and headroom")
    p.add_argument("--out", default=None)
    p.add_argument(
        "--multiple-history-years",
        type=float,
        default=None,
        help="override the measured depth of the C1 multiple history",
    )
    p.set_defaults(func=cmd_screen)

    p = sub.add_parser("record", parents=[common], help="write the immutable recommendation record")
    for name in ("symbol", "classification", "method", "thesis"):
        p.add_argument(f"--{name}", required=True)
    p.add_argument("--conviction", type=float, required=True)
    p.add_argument("--fair-value", type=float, required=True)
    p.add_argument("--sleeve-pct", type=float, required=True)
    p.add_argument("--intended-value", type=float, required=True)
    p.add_argument("--own-history", type=float, default=None)
    p.add_argument("--peer", type=float, default=None)
    p.add_argument("--currency", default="USD")
    p.add_argument("--on", default=None)
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("log-execution", parents=[common], help="log the actual executed size")
    p.add_argument("--symbol", required=True)
    p.add_argument("--actual-value", type=float, required=True)
    p.add_argument("--reason", default=None)
    p.add_argument("--on", default=None)
    p.set_defaults(func=cmd_log_execution)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    params = DEFAULT_PARAMS
    if args.command in {"record", "log-execution"} and not args.db:
        parser.error("--db is required for state-writing commands")
    try:
        return args.func(args, params)
    except DataUnavailable as exc:
        print(f"data unavailable: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
