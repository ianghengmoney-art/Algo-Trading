# GCFP v4 — Unified Adaptive Value Framework

Alert-only equity screening and valuation, implementing the GCFP v4 build spec.

**This system cannot place an order.** No broker hooks, no trading library in
the dependency tree, no execution surface on any object. It produces reports; a
human reads them and decides. That is the intended and only supported mode, and
`tests/test_prime_directives.py` asserts it against every import in the package.

---

## Running it costs nothing

The default data stack is free and needs no API key:

| Need | Source |
|---|---|
| Financial statements | **SEC EDGAR** XBRL company facts — official, no key |
| Prices | **Stooq**, falling back to **Yahoo** |
| Beta | **computed here** by regression, not bought |
| Industry grouping | **SIC codes** from EDGAR |
| Risk-free rate | 10-year Treasury yield via the price source |

EDGAR is where the paid vendors get their fundamentals, and it carries one
property none of them sell at retail: every fact arrives with the date it was
**filed**. That satisfies §13.8's point-in-time requirement for free — and a
backtest built on it is not quietly flattered by companies that have since
delisted.

```bash
# SEC policy: identify yourself with contact details. They block anonymous scrapers.
python scripts/run_screen.py --user-agent "your-name you@example.com" --limit 200
```

**What free costs instead of money:** the XBRL normalisation is fiddly (filers
tag the same concept differently across eras), the price feeds are unofficial
and will break eventually, and two paths stay blocked — REIT AFFO and insurer
combined ratios are almost always custom XBRL extensions, so B4 and B5 cannot
value from this source alone. Paying a vendor does not reliably fix those either.

An `FMPAdapter` is included if you would rather pay for convenience.

## Read this first

§18 of the spec makes the ordering explicit: **verify the data source can
supply what each classification path requires before building the gates.**

That verification is [`docs/DATA_FEASIBILITY_FINDINGS.md`](docs/DATA_FEASIBILITY_FINDINGS.md).
Its headline findings:

- **GICS sub-industry codes are not available from any free or retail source.**
  A2 and C2 cannot run as literally specified; they run at industry level
  against a substitute taxonomy, logged `VENDOR-SUBSTITUTE` everywhere.
- **No live probe run has been performed** — the development environment's
  egress policy blocks every market-data host, EDGAR included. The probe is
  ready and needs a machine with network access.
- Four stop conditions trip against a fixture modelled on a retail source's
  coverage. Three of them change what should be built.

Run the probe before trusting any output:

```bash
python scripts/data_feasibility_probe.py --source fixture   # offline demo
```

It exits non-zero when a stop condition trips, so it can gate a build step.

---

## Architecture

> Confirm the business is healthy → classify what kind of business it is →
> value it by the method that fits → cross-check against two independent
> anchors → score conviction on information the gate did not already consume →
> size by regime → monitor on fundamentals → never execute automatically.

| Module | File | What it does |
|---|---|---|
| A | `modules/a_health.py` | Health gates A1–A5 and the A6 classification router |
| B | `modules/b_valuation.py` | Sector-adaptive fair value (B1–B5) and the B2.1 TAM ceiling |
| — | `modules/b_discount.py` | The fully pinned discount-rate build |
| C | `modules/c_anchors.py` | Dual triangulation: C1, C1.1, C1.2, C2, C3, C4, C5 |
| D | `modules/d_conviction.py` | Conviction score, measuring only what the gate did not consume |
| E | `modules/e_triggers.py` | Buy / sell triggers. Alerts only |
| F | `modules/f_sizing.py` | Sizing, sleeve regimes, and the three buckets |
| G | `modules/g_execution.py` | Execution discipline: intended vs actual |
| H | `modules/h_monitor.py` | Holdings monitor and the H3 reclassification protocol |
| I | `modules/i_expectations.py` | Expectation statement and strategy-break criteria |
| J | `modules/j_tax.py` | Tax and jurisdiction (default: Singapore resident) |
| K | `modules/k_currency.py` | Currency layer: dual reporting and ADR exposure |

Backtest (`backtest/`): `engine.py` (the point-in-time walk-forward loop),
`portfolio.py`, `metrics.py` (§13.6's distribution), `variants.py` (the
benchmark suite), `accuracy.py` (§13.5), `sweep.py` (§13.7 with plateau
detection), `report.py`, `fixtures.py` (a synthetic market for testing).

Instrumentation: `diagnostics.py` (rejection taxonomy, peer availability,
conviction independence), `sensitivity.py` (valuation stress test with the
FRAGILE flag), `boundaries.py` (classification-cliff proximity, round-trip
costs).

Supporting: `config.py` (every tunable parameter, in one place), `ledger.py`
(the audit trail), `pipeline.py` (A→K in the order the spec fixes),
`universe.py` (screening, group medians, peer finding), `monitor.py` (the
holdings loop), `runner.py` (run assembly), `storage.py` (SQLite), `report.py`,
`analytics.py` (beta), `probe.py` (§18).

Data layer: `data/edgar.py` + `data/xbrl.py` (SEC filings), `data/prices.py`
(Stooq/Yahoo), `data/composite.py` (pairs them, and reconstructs C1's multiple
series on filing dates so it carries no lookahead), `data/fmp.py` (if you would
rather pay), `data/fixtures.py` (offline testing).

---

## Design decisions worth knowing

**Missing is never imputed.** Every field is `None` when the source could not
supply it, all the way from adapter to gate. A5 turns a gap into an explicit
failure, and a `NOT_COMPUTABLE` verdict blocks a name exactly as a `FAIL` does
— reported distinctly, so you can tell a sick company from an unreadable one.

**Refuses rather than approximates.** A P/E on negative earnings returns `None`,
not a large number. A DCF on negative cash flow raises `MethodRefused`. A
missing beta raises rather than defaulting to 1.0 — that absence is an A5 data
gap, and assuming 1.0 is the specific temptation the spec names.

**No lookahead in the reconstructed history.** A company's December quarter is
not knowable until it is filed in February, so C1's series observes each
quarter on its filing date. Matching on period end instead would hand the model
months of information it could not have had and flatter every backtest
invisibly.

**Every gate logs its arithmetic.** `GateResult` carries the computed value,
the threshold, and which branch of a multi-branch rule applied. A bare
`PASS`/`FAIL` is not an acceptable output.

**Conviction measures only the margin.** A name sitting exactly on its buy gate
scores 0 on D2 and near 0 on D3. `tests/test_modules.py` asserts D2 against the
spec's own table.

**Two peer concepts, never substituted.** A2's universe-wide grouping median
and C2's curated 4–8 peer set are different objects computed at different
stages. Both are logged; neither stands in for the other. A2's ladder escalates
on *computable* members and refuses outright when no rung reaches the minimum,
rather than falling back on the very median the escalation existed to avoid.

**"Zero passers" is never ambiguous.** A screen that finds nothing means either
the market is expensive and the gates are correctly refusing, or one unmapped
XBRL tag killed four thousand companies before any of them reached a valuation.
Those look identical without instrumentation and call for opposite responses, so
every screen reports why each name died, aggregated by cause:

```
REJECTION TAXONOMY
  screened 4,812 · passed 3 · rejected 4,809
  A5             2,104  (43.7%)
        1,890  missing: revenue
  E                 14  (0.3%)
           14  discount not met

  DOMINANT CAUSE: A5 / missing: revenue — 1,890 names (39.3%)
  ** More than a quarter of the universe died on a data problem, not a health
     or valuation judgement. Fix this before reading anything else. **
```

**Fair values are labelled by how much they can be trusted.** Every valuation is
re-run across defensible input changes — discount rate ±1pp, ERP ±0.5pp,
terminal growth, projection length. A name whose buy case flips on any of them
is flagged `VERDICT DOES NOT SURVIVE`; one that swings more than 25% on a 1pp
discount-rate change is `FRAGILE`.

**Classification cliffs are visible before you fall off one.** A company at
19.9% growth is CORE-STABLE; at 20.1% it is CORE-GROWTH, with a different
valuation method, a 35% gate instead of 25%, and different sizing. Any name
within 10% of a threshold is flagged with what would change.

**Parameters live in one file with a fingerprint.** Every report and stored
recommendation carries `Config.fingerprint`, so a result ties back to the
parameters that produced it. Mid-year change requests go through a logged
cooling-off period — 30 days ordinarily, 90 for a sleeve cap increase, because
raising the cap after a good run is the most likely way this system does real
damage.

---

## Usage

```bash
# weekly screen — writes reports/YYYY-MM-DD-new-passers.txt
python scripts/run_screen.py --user-agent "jane jane@example.com"

# holdings monitor — cadence follows each holding's own reporting frequency,
# so running this daily is cheap and correct
python scripts/run_monitor.py --user-agent "jane jane@example.com"

# offline, no network
python scripts/run_screen.py --source fixture

# §13 validation protocol
python scripts/run_backtest.py --source synthetic          # offline demo
python scripts/run_backtest.py --source edgar --sweep \
    --user-agent "jane jane@example.com" \
    --symbols AAPL MSFT CAT DE --start 2010-01-01 --end 2024-12-31
```

The backtest exits non-zero when a Module I strategy-break criterion trips, so
a broken system fails the run rather than producing a report nobody finishes.

The first screen builds the universe, which touches every filer once and is the
slow step. It is cached for a week; `--limit 200` keeps things quick while you
try it out.

Programmatically:

```python
from gcfp.runner import build_free_adapter, free_stack_config, load_or_build_universe

adapter = build_free_adapter("jane jane@example.com", cache_dir=".cache")
config = free_stack_config()          # trailing anchor for CORE-GROWTH, logged
universe = load_or_build_universe(adapter, config, ".cache/universe.json")
```

Swapping data sources means implementing `DataAdapter`. No gate module imports
a provider.

---

## Testing

```bash
pip install -e ".[dev]"
pytest
```

236 tests. The prime directives get their own file; where the spec states a
number, the test asserts that number rather than whatever the code produces.
The XBRL parser gets its own file too, since it is the code most likely to be
quietly wrong and it cannot be checked against live EDGAR from a sandbox.

---

## What this system cannot do

Repeated from §17 because it belongs at the front, not the back:

- It **cannot detect a business changing in kind** rather than degree until
  enough post-change data exists. A6, C1.1 and C1.2 all lag a genuine
  transformation by several quarters minimum. On the free stack this is worse:
  EDGAR carries no spinoff feed, so C1.1 sees splits but not separations.
- It **cannot protect against an entire sector being mispriced together.** Both
  anchors can move the same wrong direction at once. This is the most important
  limitation here, and the reason human review of any divergence flag matters
  more than the automated math around it.
- It **cannot tell you why** something is cheap — only that it is, by two
  independent measures.
- It **cannot fix execution.** Module G measures the intended-vs-actual gap; it
  cannot close it.
- It **cannot hedge currency.** Module K measures FX exposure. A 10% adverse
  move is a real 10% base-currency loss unrelated to business quality.
- **Its fair values are conditional on assumptions, not discovered facts.** B1's
  half-growth re-run and B2's three scenarios expose that sensitivity rather
  than hiding it — but exposure is not elimination.

---

## Status

Built and tested: Modules A–K, the data layer, universe construction, the
screen, the monitor, the §18 probe, and the §13 backtest.

**The backtest has not been run on real data.** It runs end to end against a
synthetic market — six benchmarks, walk-forward split, parameter sweep,
classification accuracy, and the full return distribution — but this
environment cannot reach EDGAR or any price feed. Run it on your own machine
before believing any number it produces.

Two §13 requirements this build cannot satisfy:

- **§13.4's "vs old GCFP v2 rules"** — the v2 specification was not supplied.
  v4 supersedes v2 without restating it, and a reconstructed v2 would be a
  strawman. The report declares the gap rather than showing six of seven
  benchmarks as though that were the full set.
- **§13.11's paper trading** — 2–3 months of calendar time, which no amount of
  code shortens. Backtests catch strategy flaws; paper trading catches pipeline
  flaws. A clean backtest does not substitute.
