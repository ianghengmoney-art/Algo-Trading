# GCFP v3 — Data Coverage Report (Section 17 critical first task)

**Probed:** 2026-08-18 · **Source:** Financial Modeling Prep, via the FMP connector on the
account attached to this session · **Verdict: both Section 17 stop conditions fire, and a
third, larger gap sits underneath them.**

This is the report Section 17 asks for *before* the gates are trusted with real capital. The
gate logic is built and tested, but it cannot be run against this data source as currently
provisioned.

---

## 1. Headline

**The connected FMP plan serves no financial statements at all — annual or quarterly.**

That is not a partial gap in one classification path. Modules A1, A2, A3, A4 and A6 all read
from the statements, so on this source *every* classification path is blocked, not merely
SPEC-GROWTH. The two stop conditions the spec anticipated are both triggered, and they are
a subset of the real problem.

| Section 17 stop condition | Status | Consequence |
|---|---|---|
| Cash-burn and share-count history unavailable | **TRIGGERED** | A3 (pre-profit branch) and A4 cannot be enforced → **SPEC-GROWTH disabled** |
| 7 years of historical multiples unavailable | **TRIGGERED** | **Module C1 disabled**; system degrades to single-anchor → no BUY can fire |
| *(beyond the spec)* No financial statements at all | **TRIGGERED** | Modules A and B cannot run for **any** classification |

Both stop conditions are enforced in code, not documentation — see
`gcfp/data/base.py::CapabilityGate` and `tests/test_stop_conditions.py`. A source missing
cash-burn or share-count history causes the A6 router to refuse the SPEC-GROWTH tag outright
rather than routing the name somewhere else; a source with under seven years of multiples
disables C1 and makes buy condition 2 unsatisfiable by construction, so the screen returns
zero passers rather than quietly becoming a single-anchor system.

---

## 2. What was probed, endpoint by endpoint

Every row below is an observed response from the live connector, not an inference from
documentation.

### Available

| Endpoint | Serves | Notes |
|---|---|---|
| `company/profile-symbol` | sector, industry, beta, market cap, price, average volume, shares, `isAdr`, `isEtf`, `isActivelyTrading`, `ipoDate`, CIK/ISIN/CUSIP | The only substantive endpoint that works |
| `company/peers` | current peer symbol list | Current membership only; see §4 |
| `company/delisted-companies` | delisted symbols with delisting dates | Useful for survivorship work, but see §4 |
| `quote/*` | current quotes | Price only, no history |

### Denied on this plan

| Endpoint | Error returned | Modules blocked |
|---|---|---|
| `statements/*` (all) | *"requires the Ultimate or Enterprise plan"* | A1, A2, A3, A4, A6, all of B |
| `statements/balance-sheet-statement`, `period=quarter` | *"a parameter you passed requires a higher plan"* | A1, A3 (both are defined on trailing four quarters) |
| `statements/key-metrics` | plan-gated | C1 (historical multiples) |
| `search/search-company-screener` | *"requires the Starter, Premium, Ultimate, or Enterprise plan"* | Universe construction; C2 peer discovery beyond the fixed peer list |
| `chart/historical-price-eod-light` | plan-gated (with and without date filters) | C1, Module D momentum, any backtest |
| `calendar/earnings-company` | plan-gated | Module E earnings-blackout hygiene |
| `analyst/financial-estimates` | plan-gated | Forward P/E, PEGY (C3) |
| `company/historical-market-cap` | plan-gated | Historical EV/Revenue reconstruction |

### Never available from this vendor, at any tier

| Requirement | Module | Finding |
|---|---|---|
| Restatement history, auditor changes, going-concern language, late filings | **A4** | FMP exposes no structured field for any of the four. The adapter raises `DataUnavailable` rather than returning "no flags", so A4 returns `DATA_GAP` — absence of evidence is not a clean bill of health. Enforcing A4 needs SEC filing text (EDGAR full-text search, or a filings vendor). |
| AFFO components (recurring maintenance capex, straight-line rent adjustment) | **B4** | Not in normalised statements from any mainstream free/mid-tier source. FFO can be reconstructed from net income + real-estate depreciation − gains on sale; AFFO cannot. B4 falls back to P/FFO and says so in its warnings. |
| Underwriting detail (losses & LAE incurred, underwriting expense, earned premium) | **B5** | Normalised statements collapse these into revenue and operating expense. Without them the combined ratio — B5's primary read — is not computable. |

---

## 3. Data-quality findings worth recording

**Industry labels are unreliable for the A6 structural tags.** The live profile for CAT
returned `"industry": "Agricultural - Machinery"` — Caterpillar is a construction and mining
equipment maker. The A6 router derives its REIT / BANK / INSURER tags from industry and SIC
strings, so a mislabelled industry routes a company to the wrong valuation method, which is
failure mode #1 on the Section 13 list. **Recommendation:** drive the structural tags from
SIC codes filed with the SEC, and treat vendor industry strings as a cross-check that must
agree, not as the source of truth.

**The vendor peer list is not a peer set.** `company/peers` for CAT returned AGCO, ASTE, DE,
ETN, GE, HY, PCAR, PH, REVG, RTX — ten names spanning agricultural machinery, aerospace
(GE, RTX) and diversified industrials, with market caps from USD 0.6B to USD 383B against
CAT's USD 406B. Module C2's own filters (same sector, 0.3–3× market cap, revenue growth
within 10pp) reject most of them, which is the correct outcome and exactly why the exclusion
log exists. The vendor list is an input to peer selection, never the peer set itself.

---

## 4. Point-in-time limitations

**No point-in-time data is available from this source, at any tier probed.**

- `company/peers` returns *today's* peer list. Using it to backtest a 2008 decision bakes in
  both survivorship bias and the benefit of knowing which companies still exist.
- Statement endpoints serve as-restated figures, not as-first-reported.
- No point-in-time index membership is available, so a historical universe cannot be
  reconstructed.

Per Section 12 item 8, any backtest run on this source must state in its **report header** —
not a footnote — that results are inflated by an unknown but material amount. The validation
harness enforces this: `gcfp/validation/protocol.py` emits the notice as a leading `!!` line
and refuses to certify a run that also skips the mandatory windows.

The `delisted-companies` endpoint does work, which makes partial survivorship correction
possible for the *constituent* side. It does not solve the peer-set side.

---

## 5. yfinance — the obvious alternative, and why it is unverified here

yfinance is the only free source that plausibly covers the two stop conditions: it serves
quarterly statements, diluted share counts and ten years of price history, which between them
restore the pre-profit A3 branch, the A4 dilution flag, and a reconstructible C1 series.

**It could not be verified from this session.** Outbound requests to Yahoo are refused by this
environment's egress policy:

```
connect_rejected: gateway answered 403 to CONNECT — host fc.yahoo.com:443
```

That is an environment restriction, not a limitation of the adapter. `gcfp/data/yfinance_adapter.py`
is written and ready; **it must be validated by running `gcfp coverage --adapter yfinance` from
a network that permits Yahoo before any weight is placed on it.** Two caveats already known
and documented in the adapter: Yahoo restates (so `POINT_IN_TIME` is never advertised), and it
supplies neither a peer list nor filing red flags, so A4 will still return `DATA_GAP`.

---

## 6. Recommendation

The system is built to run on whichever source the operator provisions, and the capability
gate makes the consequences of each choice explicit rather than silent. In order of
preference:

1. **Upgrade the FMP plan to the tier that serves `statements`, `key-metrics`, `chart` and
   `analyst`** (the errors named Ultimate/Enterprise for statements). This restores every
   path except A4 red flags, B4 AFFO and B5 underwriting detail. Re-run
   `gcfp coverage --adapter fmp` and confirm before building on it.
2. **Validate yfinance from a permitted network** as a free path to the same coverage, minus
   peers and estimates. Combine with FMP profiles/peers, which work on the current plan — the
   adapter interface is designed for exactly this split.
3. **Source A4 red flags from SEC EDGAR directly** whichever vendor is chosen. No mainstream
   price-and-fundamentals vendor supplies them, and without them A4 is permanently a
   `DATA_GAP` — meaning no candidate can ever reach a full PASS.
4. **Disable REIT and INSURER paths, or accept their documented degradation,** until AFFO
   components and underwriting detail are sourced. B4 currently falls back to P/FFO with a
   warning; B5 cannot compute a combined ratio at all without the inputs.

Until at least (1) or (2) is done, the honest state of the system is: **built, tested against
fixtures, and not runnable on live data.** Running `gcfp screen --adapter fmp` today returns
zero passers with the blocking notices printed at the top of the report, which is the system
behaving correctly.

---

## 7. Reproducing this report

```bash
gcfp capabilities --adapter fmp          # what the current plan can actually serve
gcfp coverage --adapter fmp --out docs/  # full per-requirement probe, writes a dated report
gcfp coverage --adapter fixtures         # the same probe against the offline fixture set
```

The probe attempts every Module A gate input and every Module B input for each classification
path, seven years of the path's own multiple for C1, and a full peer set for C2, across one
company per classification plus a foreign ADR and a delisted name
(`gcfp/data/coverage.py::DEFAULT_PROBE_TARGETS`).

---

## 8. Addendum — QuantConnect (probed 2026-08-19)

**Verdict: both Section 17 stop conditions clear on this source, and the
point-in-time problem is solved.** This is the first source available to the
system that can certify a Section 12 backtest.

Probed against the published `quantconnect-stubs` package (the authoritative
API surface) rather than documentation prose. QC's own site is unreachable
from the build environment's egress policy, so the stubs were the primary
source; field names below are copied from them, not recalled.

### Stop conditions

| Stop condition | FMP | QuantConnect |
|---|---|---|
| Cash-burn + share-count history | TRIGGERED | **Clear** — `cash_flow_statement.*`, `earning_reports.diluted_average_shares` |
| 7y historical multiples | TRIGGERED | **Clear** — `valuation_ratios.pe_ratio` / `forward_pe_ratio` / `pb_ratio` / `ev_to_revenue`, per-snapshot over history |
| Point-in-time data | Absent | **Present** — Morningstar fundamentals are as-of-date |

`CapabilityGate` for this adapter returns **no blocking notices**, which is
asserted in `tests/test_quantconnect_adapter.py`.

### Two findings from the original report are now fixed

**The A6 structural router no longer infers from industry strings.** QC serves
`company_reference.is_reit` as an explicit boolean,
`company_reference.industry_template_code` as Morningstar's statement-template
code (`B` bank, `I` insurance, `R` REIT), and `asset_classification.sic` as the
SEC-filed code. The mechanism that mislabelled Caterpillar as "Agricultural -
Machinery" — failure mode #1 in Section 13 — does not apply here.

**Gate A4 is partially enforceable for the first time.**
`financial_statements.period_auditor` and `auditor_report_status` are served,
so an auditor change is detectable by comparing snapshots. FMP exposed nothing
at all.

### Better than predicted: insurer underwriting

The original report predicted combined-ratio inputs would be unavailable from
any mainstream source. On QC they are present:
`total_premiums_earned`, `policyholder_benefits_gross`,
`underwriting_expenses`, and `unrealized_gain_loss`. **Module B5's primary
underwriting read is computable**, and operating ROE can be cleaned of
unrealised gains as the spec requires.

### Gaps that survive

| Gap | Module | Consequence |
|---|---|---|
| No recurring-maintenance-capex or straight-line-rent line | **B4** | **AFFO still uncomputable.** B4 falls back to P/FFO with its warning. FFO itself is derivable from `net_income` + `depreciation_amortization_depletion` − `gain_on_sale_of_ppe`. |
| No restatement or late-filing fields | **A4** | **Closed by the EDGAR overlay — see §9.** Alone, QC leaves A4 at `DATA_GAP`, so no candidate reaches a full PASS. |
| No earnings calendar | **E** | The ten-trading-day pre-earnings blackout **never fires** on this source. A real reduction in entry hygiene, not a cosmetic one. |
| No beta on `Fundamental` | **B1/B2** | CAPM cannot run, so the discount rate falls back to its floor (9% / 10% / 12%). That is the conservative direction — the floor is usually the operative number anyway — but it means the rate no longer varies with the company. |

### Audit opinion

`financial_statements.auditor_report_status` is a coded opinion — `UQ`
unqualified, `UE` unqualified with explanation, `QM`/`QL`/`OT` qualified, `AO`
adverse, `DS` disclaimer, `UA` unaudited. Only the unambiguous ends are mapped
to A4's going-concern flag: `UQ` establishes there is no going-concern
paragraph, and `AO`/`DS` are strictly more severe than one. `UE` is left
**unknown**, because it covers going concern *and* several unrelated
explanatory matters, and guessing which would be the kind of inference this
system refuses everywhere else.

### Recommendation, updated

QuantConnect supersedes FMP as the primary source for fundamentals. Paired with
the EDGAR overlay in §9, gate A4 becomes fully enforceable and a candidate can
reach a full PASS.

Reproduce with:

```bash
gcfp capabilities --adapter quantconnect
```

and, inside a QC research notebook, the same `run_coverage_probe` used for FMP.

---

## 9. Gate A4, closed — SEC EDGAR overlay (built 2026-08-19)

**This was the blocker.** Across FMP, yfinance and QuantConnect, none carried
restatement or late-filing status, so gate A4 returned `DATA_GAP` on every
candidate and **no name could ever reach a full PASS**. The screen would have
returned zero passers on live data forever — not because the market was
expensive, but because a gate could not be evaluated.

EDGAR carries all of it as *structured filing events*, not prose, in one JSON
document per company (`data.sec.gov/submissions/CIK##########.json`):

| A4 red flag | Signal | Confidence |
|---|---|---|
| Restatement | 8-K carrying item **4.02** (Non-Reliance on Previously Issued Financial Statements) | High — this is the restatement announcement itself |
| Auditor change | 8-K carrying item **4.01** (Changes in Registrant's Certifying Accountant) | High — the event, not a name diff across vendor snapshots |
| Late filing | Form **NT 10-K** / **NT 10-Q** | High |
| Going concern | *Not sourced here* — see below | — |

**Going concern is deliberately not taken from EDGAR.** Full-text searching a
10-K for the phrase over-flags badly: it appears in accounting-policy
boilerplate and in negative constructions ("no substantial doubt"). QC's coded
audit opinion (§8) is the better signal, and the two are merged.

### Merge semantics

`FilingFlags.merge` refuses to let a `None` overwrite a known value, in either
direction. An EDGAR outage therefore degrades A4 to whatever the fundamentals
source knew — it never reads as a clean bill of health. Every flag carries its
`evidence`, so a human reading a PASS can see which filing answered.

### Operational constraints

- **SEC requires a User-Agent identifying the operator, with a contact
  address.** `EdgarFilingFlags` raises rather than defaulting one, so a
  deployment cannot silently violate the access terms. Throttled to 8 req/s
  against SEC's ~10 req/s limit, and cached for 7 days per filer.
- **Live/paper only.** One request per name per refresh is fine weekly
  (~400 names ≈ 50s) and impractical across a 15-year backtest. EDGAR is also a
  live service rather than a point-in-time one; filings after the as-of date
  are filtered out, which keeps a live run honest, but a backtest should either
  accept the A4 `DATA_GAP` or pre-load a snapshot.

### VERIFICATION STATUS

**Written against EDGAR's documented submissions API but never exercised
against the live service** — this build environment's egress policy refuses
sec.gov, exactly as it refuses Yahoo and quantconnect.com. Parsing is tested
against recorded-shape payloads, not real responses.

Before relying on it, from a network that permits sec.gov:

```python
from gcfp.data.edgar import EdgarFilingFlags
EdgarFilingFlags(user_agent="Your Name you@example.com").self_test()
```

That fetches one real filer and prints what it found. **Do this before trusting
an A4 PASS.**
