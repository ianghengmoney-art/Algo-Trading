# GCFP v3 — Unified Adaptive Value Framework

An alert-only screening, valuation and portfolio-discipline system for a concentrated
buy-and-hold book. It confirms a business is healthy, classifies what kind of business it is,
values it by the method that fits that kind, cross-checks that value against two independent
anchors, scores conviction, sizes by sleeve regime, and monitors on fundamentals.

**The decision engine never executes.** `gcfp/` has no broker client, no order hook and no
trading library anywhere in its dependency tree.

The QuantConnect deployment (`qc_algorithm/`) *does* place simulated paper-trading orders —
a deliberate, operator-chosen departure from the original alert-only spec. The guarantee is
narrowed rather than dropped: `qc_algorithm/execution.py` is the single permitted execution
boundary, and `tests/test_prime_directives.py` fails the build if an order call appears
anywhere else, if `gcfp/` imports the execution package, or if the shipped config stops
defaulting to dry run. See [docs/QUANTCONNECT.md](docs/QUANTCONNECT.md).

---

## Read this first

Section 17 of the spec requires a data coverage audit before the gates are trusted. It has
been run: **[`docs/DATA_COVERAGE_REPORT.md`](docs/DATA_COVERAGE_REPORT.md)**.

Both of the spec's stop conditions fire on the currently connected data source, and a larger
gap sits underneath them — the connected FMP plan serves no financial statements at all, so
no classification path can run on live data today. The system is built, tested against
fixtures, and **not yet runnable on live data**. The coverage report says exactly what to
provision to change that.

The stop conditions are enforced in code, not prose: a source that cannot supply cash-burn and
share-count history causes the router to refuse the SPEC-GROWTH tag, and a source with under
seven years of multiple history disables Module C1 and makes the buy trigger unsatisfiable.
Both print at the top of every report.

## Deploying to QuantConnect

QC is the first source that clears both Section 17 stop conditions and supplies point-in-time
data, which makes the Section 12 backtest certifiable for the first time. The step-by-step —
including why the first deploy runs in dry run — is in
**[docs/QUANTCONNECT.md](docs/QUANTCONNECT.md)**.

## Quick start

```bash
pip install -e ".[dev]"

gcfp capabilities --adapter fixtures      # what a source can actually serve
gcfp capabilities --adapter quantconnect  # ...and what QC clears
gcfp coverage     --adapter fixtures      # the Section 17 probe
gcfp screen       --adapter fixtures --as-of 2026-06-30
pytest -q
```

The `fixtures` adapter is a deterministic offline dataset with one archetype per
classification plus deliberate failures. It exists so the whole pipeline can be exercised
without a subscription and so tests assert on known answers. Its companies are synthetic:
useful for testing, useless for investing.

To run against real data, provision a source per the coverage report and swap the adapter:

```bash
export FMP_API_KEY=...
gcfp coverage --adapter fmp --out docs/    # confirm coverage before trusting a screen
gcfp screen   --adapter fmp --portfolio my_portfolio.json --db state.sqlite3
```

## Architecture

| Module | File | What it does |
|---|---|---|
| **A** | `modules/a_health.py` | Health gate A1–A5 and the A6 classification router. Nothing reaches valuation without a PASS. Assigns exactly one tag or **none** — a company matching no row is rejected, never routed to the nearest method. |
| **B** | `modules/b_valuation.py` | One method per classification: two-stage DCF, three-phase scenario DCF, bank P/TBV, REIT P/AFFO, insurer combined ratio + P/B. Mismatched combinations raise `MethodMismatch`; DCF-on-a-bank and P/E-on-a-REIT are refused, not approximated. |
| **C** | `modules/c_triangulation.py` | Own-history anchor (z-score, percentile, re-rating step-change scan) and peer anchor (median, full exclusion log), computed independently and **never averaged**. Divergence past 30pp refuses a combined verdict. |
| **D** | `modules/d_conviction.py` | 0–100 conviction score. Momentum is applied only across names that already passed everything else, and its 10-point maximum is below the 60-point buy floor, so it can order passers but never admit one. |
| **E** | `modules/e_triggers.py` | Buy needs all three conditions; sell needs a Module A failure or both anchors confirming overvaluation. Earnings blackout defers the alert without changing the verdict. |
| **F** | `modules/f_sizing.py` | Two sleeve regimes that never blend, portfolio allocator, per-name and sector caps, `QUALIFIED — NO HEADROOM`, and the thin-screen rule that holds cash instead of concentrating. |
| **G** | `modules/g_execution.py` | Immutable recommendation records with a mandatory falsifiable thesis, `SIZE DEVIATION` tracking, and `TRIM-TO-CAP` measured against sleeve capacity. |
| **H** | `modules/h_monitor.py` | Quarterly re-run producing SELL/REVIEW flags. Nothing computable from price history alone can trigger. |
| **I** | `modules/i_expectations.py` | Expectation statement printed verbatim, pre-registered performance bands, strategy-break criteria, and the 30/90-day cooling-off ledger. |
| **J** | `modules/j_tax.py` | Singapore-resident default: 0% capital gains, 30% non-recoverable US dividend withholding, SGX REIT exemption, after-withholding yields. |

Supporting layers: `data/` (capability-aware adapters — fixtures, FMP, yfinance),
`engine.py` (A→E orchestration), `state/db.py` (SQLite audit trail), `reports/render.py`
(weekly and quarterly reports), `validation/` (Section 12 protocol, distribution metrics,
parameter sweep with plateau detection).

## Design decisions worth knowing

**Adapters advertise capabilities.** Section 15 asks for swappable sources; that is only half
of what is needed. An adapter must also say what it *cannot* do, or the pipeline discovers a
hole three modules deep and the tempting fix is to approximate. `Capability` and
`CapabilityGate` make the consequence explicit and refuse the affected path.

**Missing means missing.** Every numeric field is `Optional` and nothing is imputed — no
zeros, no sector averages, no carried-forward prior periods. A5 fails a candidate on absent
inputs and A4 returns `DATA_GAP` rather than a clean bill of health when filing status is
unknown.

**Gates log the values that earned them.** Every `GateOutcome` carries the numbers that
produced it, so a human can audit exactly which points were earned rather than seeing a bare
pass.

**Trailing windows name their basis.** Gates defined on "trailing four quarters" prefer
quarterly data and fall back to the latest fiscal year — a fiscal year is literally four
quarters — but always record which one answered, because the annual basis can be up to twelve
months stale where the quarterly basis cannot.

**Parameters live in one frozen place.** `config.py` holds every threshold, so a diff to that
file is self-evidently a strategy change. Prime Directive 8 allows changes only at scheduled
annual review; mid-year requests go through the cooling-off ledger.

## What this system cannot do

It cannot detect a business changing in kind rather than degree until enough post-change data
exists — A6 and the C1 step-change scan lag a genuine transformation by several quarters.

It cannot protect against an entire sector being mispriced together. Both anchors can move in
the same wrong direction at once. This is the most important limitation in the design and the
reason human review of a divergence flag matters more than the automated math around it.

It cannot tell you *why* something is cheap — only *that* it is, by two independent measures.

It cannot fix execution. Module G measures the gap between intended and actual sizing; closing
it remains a human decision, every time.

## Before real capital

Per Section 12, in order: a ≥15-year backtest reported separately per classification and
covering 2000–2002, 2008–2009 and 2022; a walk-forward split with no tuning on the holdout;
five benchmarks; classification accuracy as its own metric; the full return distribution; a
parameter sweep read for plateaus rather than peaks; point-in-time data throughout; then two
to three months of paper trading. `gcfp/validation/protocol.py` refuses to certify a run that
skips any of the structural requirements, and prints the survivorship-inflation notice in the
header when point-in-time data is absent.

None of that has been run — no source available to this build supplies the history it needs.
