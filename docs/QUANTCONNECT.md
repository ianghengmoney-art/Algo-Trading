# Running GCFP v3 on QuantConnect

The path from this repository to a running paper-trading algorithm, in order.

**Read first:** deploying to QC paper trading is a deliberate departure from the
original spec. GCFP v3 was alert-only by design — Prime Directive 1 required
the system to be *incapable* of placing an order. A paper algorithm places
simulated orders with no human in the loop. That reverses the directive, and
the reversal is scoped rather than diffuse: see [The execution
boundary](#the-execution-boundary) below.

No real capital is at risk in paper mode. Section 12 still requires **two to
three months of paper trading** before that changes.

---

## Phase 0 — Account and tooling

1. Create a QuantConnect account. Backtesting is free; **live/paper deployment
   requires a paid tier** — check current pricing on quantconnect.com, it is not
   quoted here because it changes.
2. Confirm your tier includes **US Equity Fundamental data** (Morningstar).
   Everything in Phase 1 depends on it.
3. Install the LEAN CLI locally (`pip install lean`, Docker required). The
   browser IDE also works, but the CLI matches this repository's layout and
   lets the same tests run against the algorithm code.

## Phase 1 — Verify coverage before trusting anything

Same discipline the FMP audit used. Do not skip it because the adapter exists.

```bash
gcfp capabilities --adapter quantconnect
```

Then, inside a QC research notebook, run `run_coverage_probe` against a
`QCDataAdapter` built from a real universe selection. Compare against
[`DATA_COVERAGE_REPORT.md` §8](DATA_COVERAGE_REPORT.md), which records what the
published API surface promises. **§8 is a claim about the API, not a
measurement of your subscription** — the notebook run is what confirms your
tier actually serves it.

Expected state: both Section 17 stop conditions clear, point-in-time available,
gate A4 still `DATA_GAP` (so no candidate reaches a full PASS), REIT AFFO
unavailable, no earnings blackout.

## Phase 1b — Enable gate A4

Without this, **the screen returns nothing**. QC carries no restatement or
late-filing status, so gate A4 returns `DATA_GAP` on every candidate and no
name reaches a full PASS. The EDGAR overlay closes it.

First verify EDGAR is reachable and the API shape holds — it has never been run
against the live service:

```python
from gcfp.data.edgar import EdgarFilingFlags
EdgarFilingFlags(user_agent="Your Name you@example.com").self_test()
```

Then set the `edgar_user_agent` algorithm parameter to the same string. SEC
refuses requests that do not identify the operator, which is why there is no
default.

**Leave it off for backtests.** One request per name per refresh is impractical
across a 15-year run, and EDGAR is a live service rather than a point-in-time
one. Filings after the as-of date are filtered out, so a live run is honest,
but a backtest should accept the A4 `DATA_GAP` or pre-load a snapshot.

## Phase 2 — Backtest

Section 12's protocol becomes runnable here for the first time, because QC's
fundamentals are point-in-time.

1. Backtest ≥15 years, reported **separately per classification**.
2. Cover the mandatory windows: 2000–2002, 2008–2009, 2022. Confirm your data
   subscription actually reaches back that far before trusting the result.
3. Walk-forward split, no tuning on the holdout.
4. Five benchmarks, classification accuracy as its own metric, the full return
   distribution, and the parameter sweep read for plateaus rather than peaks.

`gcfp/validation/protocol.py` refuses to certify a run that skips any of the
structural requirements, and prints the survivorship-inflation notice as a
header line when point-in-time data is absent.

## Phase 3 — Deploy in dry run

```bash
lean cloud push --project qc_algorithm
```

The shipped `config.json` sets `live_enabled = "0"`. **Leave it there first.**
The full pipeline runs, every intended order is logged with its reason, and
nothing is submitted. Read a few weeks of those logs before changing it.

Default-off is deliberate: a misconfigured deploy must not start trading, and
Module G's entire argument is that the gap between intent and execution is
where this strategy actually fails.

## Phase 4 — Enable paper orders

Set `live_enabled = "1"` and deploy as a Live Algorithm with brokerage =
QuantConnect Paper Trading. No external brokerage credentials are involved.

Then leave it alone for two to three months. Section 12 again: backtests catch
strategy flaws, paper trading catches pipeline flaws, and they are different
failure classes.

---

## The execution boundary

`qc_algorithm/execution.py` is the **only** file in this repository permitted to
place an order. Four tests in `tests/test_prime_directives.py` enforce it:

| Test | Guarantees |
|---|---|
| `test_decision_engine_cannot_place_orders` | `gcfp/` has no order calls |
| `test_execution_boundary_is_the_only_place_orders_are_placed` | no order calls anywhere else in the repo, including elsewhere in `qc_algorithm/` |
| `test_the_boundary_actually_contains_the_order_calls` | the previous test isn't passing because nothing trades at all |
| `test_decision_engine_never_imports_the_execution_package` | `gcfp/` cannot reach execution even indirectly |

Plus `test_orders_are_off_until_explicitly_enabled`, which fails if the shipped
config stops defaulting to dry run.

The module is split so the guarantee is readable: `plan_orders` is pure and
holds every decision about *what* to do; `apply_orders` is a loop with no
branches that submits them. You can audit this system's trading behaviour by
reading one function.

## What the algorithm does

| Schedule | Action |
|---|---|
| Weekly | Run Modules A→E across the universe, size passers through Module F, plan and submit opens |
| Quarterly | Re-run Modules A→E on holdings; a Module A failure is a SELL on fundamentals, independent of price |

The growth-sleeve cap is enforced **twice**: once in Module F where sizing is
computed, and again at the execution boundary before capital moves. Sizing
drift toward CORE-sized growth positions is failure mode #9, and a cap enforced
in one place is a cap enforced once.

## Known limitations in this deployment

- **Gate A4 needs the EDGAR overlay** (Phase 1b). Without it, A4 returns
  `DATA_GAP` and no candidate reaches a full PASS. With it, A4 is fully
  enforceable — but the overlay is unverified against live EDGAR, so run
  `self_test()` first.
- **No earnings blackout.** QC serves no earnings calendar, so Module E's
  ten-day pre-earnings deferral never fires.
- **REIT AFFO unavailable.** Module B4 falls back to P/FFO and says so.
- **No beta**, so Modules B1/B2 use their discount-rate floors rather than CAPM.
- **Sizing up an existing position is not automated.** The planner opens new
  positions and exits failed ones; adding to a winner stays a human decision.
