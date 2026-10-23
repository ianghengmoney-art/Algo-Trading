#!/usr/bin/env python3
"""Factor attribution of every saved factor-strategy run.

    python scripts/run_attribution.py

Regresses each run's monthly returns on the Fama-French five factors plus
momentum (gcfp/backtest/attribution.py) and writes reports/attribution.txt.
A diagnostic, not a variant: it changes no rule. The backtest job runs it
when scripts/backtest_defaults.json sets "attribution_only": true, because
Kenneth French's site is reachable from GitHub Actions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gcfp.backtest import attribution

DEFAULT_OUT = Path("reports/attribution.txt")

#: Each saved run: a label (with its RUNS.md number) and its curves file.
RUNS = [
    ("Variant 1, 1,000 companies, 2015-2025 (#19/#21)", "reports/factor-v1-1000-curves.csv"),
    ("Variant 2, 1,000 companies, 2015-2025 (#20) — SELECTED", "reports/factor-v2-1000-curves.csv"),
    ("Variant 2, 1,000 companies, from 2012 (#23)", "reports/factor-v2-from-2012-curves.csv"),
    ("Variant 2, 4,000 companies, 2015-2025 (#24)", "reports/factor-v2-4000-curves.csv"),
    ("Variant 3, $2bn+, 4,000 companies (#22)", "reports/factor-v3-4000-curves.csv"),
]


def extra_runs() -> list[tuple[str, Path]]:
    """Runs added after this list was written: any reports/factor-v*-curves.csv
    for a variant above 3."""
    out = []
    for path in sorted(Path("reports").glob("factor-v*-curves.csv")):
        stem = path.name[len("factor-v"):].split("-")[0]
        if stem.isdigit() and int(stem) > 3:
            out.append((f"{path.name.replace('-curves.csv', '')}", path))
    return out


def main(argv=None) -> int:
    from run_long_history import http_get

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    args = parser.parse_args(argv)
    runs = [(label, Path(p)) for label, p in RUNS] + extra_runs()
    text = attribution.run(runs, http_get, args.cache_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
