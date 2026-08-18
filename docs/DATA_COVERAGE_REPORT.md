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
