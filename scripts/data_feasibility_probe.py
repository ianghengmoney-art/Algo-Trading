#!/usr/bin/env python3
"""Run §18's critical first task and print the coverage report.

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


def build_adapter(source: str, api_key: str | None):
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
    parser.add_argument("--source", default="fixture", choices=("fmp", "fixture"))
    parser.add_argument("--api-key", default=os.environ.get("FMP_API_KEY"))
    parser.add_argument("--out", type=Path, help="also write the report here")
    args = parser.parse_args()

    adapter = build_adapter(args.source, args.api_key)
    report = run_probe(adapter, DEFAULT_CONFIG, DEFAULT_TARGETS)
    text = report.render()
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
