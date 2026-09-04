# GCFP v4 — Unified Adaptive Value Framework

Alert-only equity screening and valuation, implementing the GCFP v4 build spec.

**This system cannot place an order.** No broker hooks, no trading library in
the dependency tree, no execution surface on any object. It produces reports; a
human reads them and decides. That is the intended and only supported mode, and
`tests/test_prime_directives.py` asserts it against every import in the package.

---

## Read this first

§18 of the spec makes the ordering explicit: **verify the data source can
supply what each classification path requires before building the gates.**

That verification is [`docs/DATA_FEASIBILITY_FINDINGS.md`](docs/DATA_FEASIBILITY_FINDINGS.md).
Its headline findings:

- **GICS sub-industry codes are not available from FMP at any tier.** A2 and C2
  cannot run as literally specified; they run at industry level against a
  vendor taxonomy, logged `VENDOR-SUBSTITUTE` everywhere.
- **No live probe run has been performed against a real provider** — this
  environment's egress policy blocks market-data hosts. The probe is ready and
  needs an environment with network access and an entitled API key.
- Four stop conditions trip against a fixture modelled on a retail source's
  coverage. Three of them change what should be built.

Run the probe yourself before trusting any output:

```bash
python scripts/data_feasibility_probe.py --source fmp --api-key $FMP_API_KEY
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

Supporting: `config.py` (every tunable parameter, in one place), `ledger.py`
(the audit trail), `pipeline.py` (A→K in the order the spec fixes),
`storage.py` (SQLite), `report.py`, `probe.py` (§18).

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

**Every gate logs its arithmetic.** `GateResult` carries the computed value,
the threshold, and which branch of a multi-branch rule applied. A bare
`PASS`/`FAIL` is not an acceptable output.

**Conviction measures only the margin.** A name sitting exactly on its buy gate
scores 0 on D2 and near 0 on D3. `tests/test_modules.py` asserts D2 against the
spec's own table.

**Two peer concepts, never substituted.** A2's universe-wide grouping median
and C2's curated 4–8 peer set are different objects computed at different
stages. Both are logged; neither stands in for the other.

**Parameters live in one file with a fingerprint.** Every report and stored
recommendation carries `Config.fingerprint`, so a result ties back to the
parameters that produced it. Mid-year change requests go through a logged
cooling-off period — 30 days ordinarily, 90 for a sleeve cap increase, because
raising the cap after a good run is the most likely way this system does real
damage.

---

## Usage

```python
from datetime import date
from gcfp.config import DEFAULT_CONFIG
from gcfp.data.fmp import FMPAdapter
from gcfp.modules.f_sizing import PortfolioState
from gcfp.modules.k_currency import FxTable
from gcfp.pipeline import CandidateInputs, evaluate_candidate
from gcfp.report import new_passer_report

adapter = FMPAdapter(api_key="...")
data = adapter.load_company("CAT", multiple="trailing_pe")
market = adapter.get_market_data()

evaluation = evaluate_candidate(
    data, market, DEFAULT_CONFIG,
    CandidateInputs(
        current_multiple=13.0,
        peer_candidates=[...],      # C2 screens and logs every one
        subject_group="Machinery",
        tam_sources=[...],          # B2.1 accepts no uncited figure
        thesis_invalidation="ROIC-WACC spread below zero for two years",
    ),
    PortfolioState(total_value=1_000_000.0),
    FxTable({"USDSGD": 1.29}, date.today()),
)

print(new_passer_report([evaluation], portfolio, fx, DEFAULT_CONFIG))
print(evaluation.ledger.render())   # the full audit trail
```

Swapping data sources means implementing `DataAdapter`. No gate module imports
a provider.

---

## Testing

```bash
pip install -e ".[dev]"
pytest
```

126 tests. The prime directives get their own file; where the spec states a
number, the test asserts that number rather than whatever the code produces.

---

## What this system cannot do

Repeated from §17 because it belongs at the front, not the back:

- It **cannot detect a business changing in kind** rather than degree until
  enough post-change data exists. A6, C1.1 and C1.2 all lag a genuine
  transformation by several quarters minimum.
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
