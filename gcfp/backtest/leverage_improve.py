"""Improvements to L2, as registered in docs/LEVERAGE_STRATEGY.md on 2026-10-10.

1. The futures cost scenario: the same rule at futures costs.
2. L2-E, the ensemble signal: leverage 2.0x times the share of the 3-, 6-,
   9- and 12-month averages the index is above.

All runs start on the same day (once 12 month-ends exist), so they are
compared over identical periods.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .leverage_daily import (
    CRASHES, MAINTENANCE, POST_PUBLICATION, _pct, _ymd, cagr, drawdown, load_daily,
    simulate, window_return, worst_month,
)

ENSEMBLE = (3, 6, 9, 12)
#: Futures: implied financing over T-bills, commissions, per-switch cost.
FUTURES = dict(borrow_spread=0.003, running_cost=0.0005, switch_cost=0.0005)


def evaluate(market: dict[int, float], rf: dict[int, float]) -> tuple[str, bool]:
    month_ends = sorted({d // 100 for d in market})
    first = min(d for d in market if d // 100 >= month_ends[max(ENSEMBLE)])
    last = max(market)
    days = [d for d in sorted(market) if d >= first]
    mid = days[len(days) // 2]
    periods = [("full", first, last), ("1st half", first, days[len(days) // 2 - 1]),
               ("2nd half", mid, last), ("since 1990", 19900101, last),
               ("since 2008", POST_PUBLICATION, last)]

    runs = {
        "L2": simulate(market, rf, "L2 (10-month, margin-loan costs)"),
        "L2-E": simulate(market, rf, "L2-E (3/6/9/12 ensemble, loan costs)",
                         lookbacks=ENSEMBLE),
        "L2-fut": simulate(market, rf, "L2 at futures costs", **FUTURES),
        "L2-E-fut": simulate(market, rf, "L2-E at futures costs", lookbacks=ENSEMBLE,
                             **FUTURES),
        "market": simulate(market, rf, "market, buy and hold", trend=False,
                           max_leverage=1.0, costs=False),
    }

    lines = [
        "=" * 78,
        "IMPROVING L2 — futures costs and the ensemble signal (daily data)",
        "=" * 78,
        f"Kenneth French daily US market, {_ymd(first)} to {_ymd(last)}: every run from "
        "the same day.",
        "",
        "COMPOUND RETURN PER YEAR (deepest fall on daily closes)",
        f"  {'':38s}" + "".join(f"{p[0]:>17s}" for p in periods),
    ]
    for run in runs.values():
        cells = [f"{_pct(cagr(run, a, b))} ({drawdown(run, a, b)[0]:.0%})".rjust(17)
                 for _, a, b in periods]
        lines.append(f"  {run.name[:38]:38s}" + "".join(cells))

    lines += ["", "THE CRASHES (return over each window)",
              f"  {'':24s}" + "".join(f"{k:>11s}" for k in runs)]
    for name, a, b in CRASHES:
        if a >= first:
            lines.append(f"  {name[:24]:24s}" + "".join(
                f"{_pct(window_return(r, a, b)):>11s}" for r in runs.values()))

    lines += ["", "WORST MOMENTS"]
    for key, run in runs.items():
        eq = [run.equity[d] for d in days]
        worst_day = min((y / x - 1) for x, y in zip(eq, eq[1:]) if x > 0)
        wm = worst_month(run, first, last)[0]
        extra = ""
        if key != "market":
            extra = (f" · lowest equity/exposure {run.min_ratio:.0%} · margin calls at "
                     f"{MAINTENANCE:.0%}: {len(run.margin_calls)} · switches {run.switches}")
        lines.append(f"  {key:9s} worst day {worst_day:+.0%} · worst month {wm:+.0%}" + extra)

    # Registered criterion for L2-E: against L2, both at the loan costs.
    l2, l2e = runs["L2"], runs["L2-E"]
    checks = []
    for label, a, b in periods[:3]:
        x, y = cagr(l2e, a, b), cagr(l2, a, b)
        checks.append((f"higher CAGR than L2, {label} ({_pct(x)} vs {_pct(y)})",
                       x is not None and y is not None and x > y))
    dd_e, dd_2 = drawdown(l2e, first, last)[0], drawdown(l2, first, last)[0]
    checks.append((f"deepest fall no deeper than L2's ({dd_e:.0%} vs {dd_2:.0%})",
                   dd_e >= dd_2))
    passed = all(ok for _, ok in checks)
    lines += ["", "REGISTERED CRITERION — does L2-E replace L2?"]
    lines += [f"  [{'PASS' if ok else 'FAIL'}] {t}" for t, ok in checks]
    lines.append("  VERDICT: " + ("L2-E REPLACES L2" if passed else "L2 STAYS"))

    gain = {label: (cagr(runs["L2-fut"], a, b) or 0) - (cagr(l2, a, b) or 0)
            for label, a, b in periods}
    lines += ["", "FUTURES INSTEAD OF A MARGIN LOAN (same rule, L2)",
              "  extra return per year: " + " · ".join(
                  f"{k} {v:+.2%}" for k, v in gain.items()),
              "  plus: no 30% US dividend withholding (futures prices carry dividends),",
              "  and a margin call needs a ~46% fall instead of ~33%.",
              "=" * 78]
    return "\n".join(lines) + "\n", passed


def run(get: Callable[[str], bytes], cache_dir: Path | None = None) -> tuple[str, bool]:
    market, rf = load_daily(get, cache_dir)
    return evaluate(market, rf)
