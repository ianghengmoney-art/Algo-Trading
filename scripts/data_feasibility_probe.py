#!/usr/bin/env python3
"""Run §18's critical first task and print the coverage report.

    # the free stack — SEC EDGAR plus a free price feed, no key needed
    python scripts/data_feasibility_probe.py --source edgar \
        --user-agent "you@example.com"

    python scripts/data_feasibility_probe.py --source fmp --api-key $FMP_API_KEY
    python scripts/data_feasibility_probe.py --source fixture   # offline demo

Exits non-zero when a stop condition trips, so the probe can gate a build step:
§18 requires reporting all findings *before* proceeding to build the gates, and
an exit code is the bluntest way to keep that ordering honest.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gcfp.config import DEFAULT_CONFIG
from gcfp.probe import DEFAULT_TARGETS, run_probe


def build_adapter(source: str, api_key: str | None, user_agent: str | None,
                  cache_dir: Path | None = None):
    if source == "edgar":
        from gcfp.runner import build_free_adapter

        if not user_agent:
            raise SystemExit(
                "EDGAR requires --user-agent identifying you with a real email "
                'address, e.g. --user-agent "you@example.com". The SEC blocks '
                "anonymous requests."
            )
        return build_free_adapter(user_agent, cache_dir=cache_dir)
    if source == "fmp":
        from gcfp.data.fmp import FMPAdapter

        if not api_key:
            raise SystemExit(
                "FMP requires an API key: pass --api-key or set FMP_API_KEY"
            )
        return FMPAdapter(api_key=api_key)
    if source == "fixture":
        from gcfp.fixtures_probe import build_probe_fixture

        return build_probe_fixture()
    raise SystemExit(f"unknown source {source!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", default="fixture", choices=("edgar", "fmp", "fixture")
    )
    parser.add_argument(
        "--user-agent",
        help='required for EDGAR; SEC policy. e.g. "you@example.com"',
    )
    parser.add_argument("--api-key", default=os.environ.get("FMP_API_KEY"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"),
                        help="cache SEC responses so a re-run is fast")
    parser.add_argument("--out", type=Path, help="also write the report here")
    parser.add_argument(
        "--peer-sample", type=int, default=0, metavar="N",
        help=(
            "build a universe of N extra companies so C2's peer screen can "
            "actually be tested. Without it the probe cannot tell 'no "
            "comparables exist' from 'nobody supplied candidates'. 200 is a "
            "reasonable start; it adds a few minutes."
        ),
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()

    adapter = build_adapter(
        args.source, args.api_key, args.user_agent, args.cache_dir
    )

    # The free stack has no analyst estimates, so CORE-GROWTH anchors on a
    # trailing multiple. Probing with the default config would test a multiple
    # this source cannot compute and report a gap that is really a mismatch.
    config = DEFAULT_CONFIG
    if args.source == "edgar":
        from gcfp.runner import free_stack_config

        config = free_stack_config(DEFAULT_CONFIG)

    try:
        report = run_probe(
            adapter, config, DEFAULT_TARGETS,
            peer_sample=args.peer_sample, progress=args.progress,
        )
        text = report.render()
    except Exception:
        # A crash late in a long run used to leave report.txt untouched, so
        # the old report was pushed as though it were new. Write the failure
        # where the report would have gone instead: it gets pushed and read.
        import traceback

        failure = (
            "GCFP §18 PROBE FAILED — no report was produced.\n"
            f"generated {__import__('datetime').date.today().isoformat()}\n\n"
            + traceback.format_exc()
        )
        print(failure, file=sys.stderr)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(failure)
            print(f"\nfailure written to {args.out}", file=sys.stderr)
        return 3
    print(text)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        print(f"\nwritten to {args.out}", file=sys.stderr)

    if report.any_tripped:
        print(
            "\nA stop condition tripped. §18: report all findings before "
            "proceeding to build the gates.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
