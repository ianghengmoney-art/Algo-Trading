# §18 Critical First Task — Findings

> *"Before writing any strategy code, verify the data source can supply what
> each classification path requires. ... Report all findings before proceeding
> to build the gates."* — GCFP v4 §18

This document reports the findings. It has two parts, and the distinction
between them matters: what was established about **real data sources**, and
what was established by running the probe against a **fixture** that models
one.

---

## Part 1 — Findings about real data sources

These were established by querying Financial Modeling Prep directly during
development, and they hold regardless of the probe.

### 1.1 FMP does not publish GICS codes at any tier

`/profile` returns FMP's own `sector` and `industry` strings. Caterpillar comes
back as:

```json
{ "sector": "Industrials", "industry": "Agricultural - Machinery" }
```

There is no sub-industry rung and no GICS code. This is not a plan-tier
limitation — GICS is separately licensed from MSCI/S&P and FMP does not
redistribute it.

**Consequence.** The spec writes A2 and C2 against GICS sub-industry. Neither
can run as literally specified. This trips **stop condition 3**, whose
directive is to report which fallback level is available before building.

The finest rung available is **industry**, and it is a vendor taxonomy, not a
GICS sub-industry. The two are not interchangeable:

- FMP's `industry` is coarser and differently drawn. "Agricultural - Machinery"
  groups Caterpillar with agricultural equipment makers; GICS places CAT in
  *Construction Machinery & Heavy Transportation Equipment*.
- A coarser grouping means a **wider** A2 leverage median, so A2 becomes more
  permissive than the spec intends, in a direction that is not visible from the
  gate's output alone.

**What was built in response.** Every grouping resolves to a `Grouping` object
carrying its rung and whether that rung is genuinely GICS. Non-GICS groupings
are logged `VENDOR-SUBSTITUTE` on every gate that uses them. The A2 fallback
ladder escalates on *computable members*, not nominal membership, and refuses
outright when no rung reaches the minimum rather than falling back on the very
median the escalation existed to avoid.

**Bearing on Module I.** The peer-gameability break criterion — *"if two
reasonable analysts produce peer sets yielding >30% different fair values, C2
is not a method, it is an opinion"* — applies with **more** force under a
substitute taxonomy, not less. A coarser grouping admits more candidates, which
widens the range of defensible peer sets.

### 1.2 Endpoint access is plan-tiered, and the account tested had none of it

On the API key available during development, these families returned
403 "requires a higher plan":

| Endpoint family | Status | Gates it would serve |
|---|---|---|
| `statements` (income, balance, cash flow, key metrics, ratios) | **DENIED** | every Module A gate; B1–B5 |
| `quote` | **DENIED** | A5 current price; all discount calculations |
| `chart` (historical prices) | **DENIED** | ADV; D5 momentum; C1 price-derived multiples |
| `search` / screener | **DENIED** | the universe itself; A2's grouping population |
| `company` (profile, peers) | available | A6 structure flags; C2 candidate list |

**Consequence.** On that tier, not one Module A gate can be computed. This is
an *entitlement* gap, not a *data* gap — a different problem with a different
fix — and the FMP adapter's `capabilities()` reports the two separately so the
coverage report does not conflate "this vendor lacks the data" with "this key
lacks the entitlement."

### 1.3 The vendor peer list is not a peer group

FMP's peers for CAT:

```
AGCO, ASTE, DE, ETN, GE, HY, PCAR, PH, REVG, RTX
```

RTX is aerospace and defence. ETN is electrical components. GE post-breakup is
an aero-engine business. None is within C2's ±10pp growth band of CAT, and
several fall outside its 0.3×–3× size band.

**What was built in response.** `get_peer_symbols()` returns *candidates*. C2
applies the real screen and logs a decision — inclusion or exclusion with a
reason — for **every** candidate considered. Treating the vendor list as a peer
group would be precisely the "single most gameable step" the spec warns about,
outsourced to a vendor.

### 1.4 Point-in-time data is unavailable

FMP serves current constituents and current peer lists. There is no historical
membership snapshot.

**Consequence.** §13.8 requires that when point-in-time data is unavailable,
the report header must state that results are inflated by an unknown material
amount — *"do not footnote it."* The probe emits this as a build directive
rather than a note.

### 1.5 This environment cannot reach market-data providers at all

Direct HTTPS to `financialmodelingprep.com` and `query1.finance.yahoo.com` is
refused by the execution environment's egress policy:

```
curl: (56) CONNECT tunnel failed, response 403
```

**So a live probe run against real data has not been performed.** The findings
in 1.1–1.4 come from querying FMP through a separate connector during
development; they are about the vendor, not about a probe run. Section 2 below
reports what the probe itself established.

---

## Part 2 — Findings from running the probe

The probe was run against `gcfp/fixtures_probe.py`, a fixture set built to
model a retail data source's coverage profile. **Its numbers are properties of
that fixture, not measurements of any vendor.** Its value is that it verifies
the probe works — including that each stop condition can both fire and not
fire — before the probe's verdict is trusted against live data.

Regenerate with:

```bash
python scripts/data_feasibility_probe.py --source fixture --out docs/COVERAGE_REPORT.txt
```

### Coverage by target

| Target | Role | Coverage | A6 routed to | C1 years | C2 peers | Mode |
|---|---|---|---|---|---|---|
| CAT | mature industrial | 100% | CORE-STABLE | 7.0 | 6 | dual |
| NVDA | profitable fast-grower | 93% | CORE-GROWTH | 7.0 | 0 | **single** |
| RIVN | unprofitable grower | 64% | SPEC-GROWTH | 1.7 | 0 | **both uncomputable** |
| JPM | bank | 96% | FINANCIAL-BANK | 7.0 | 5 | dual |
| O | REIT | 86% | REIT | 7.0 | 0 | **single** |
| PGR | insurer | 96% | INSURER | 7.0 | 5 | dual |
| TSM | foreign ADR | 93% | CORE-GROWTH | 7.0 | 0 | **single** |
| GE | spinoff in last 7y | 96% | CORE-STABLE | 1.2 (from 7.0) | 4 | **single** |
| TPL | fewer than 4 peers | 93% | CORE-STABLE | 7.0 | 0 | **single** |
| SIVBQ | delisted | — | — | — | — | **unreachable** |

A6 routed all six classification targets to their expected tags.

### What each special case demonstrated

- **GE (spinoff).** C1.1 detected the event and truncated the series from 7.0
  usable years to 1.2, dropping C1 below its three-year minimum and routing the
  name to C5. This is the gate working, and the probe reports it as distinct
  from a source that simply lacks history.
- **TPL (near-monopoly).** Both named peers are an order of magnitude smaller
  and grow at a different rate; neither survives C2's screen. Zero genuine
  peers, SINGLE-ANCHOR MODE. This is the condition C5 was added for — under v3
  this name would have failed BUY permanently for reasons unrelated to its
  quality.
- **O (REIT).** No FFO or AFFO. B4 cannot run and P/E is banned on this path,
  so the name cannot be valued at all. The peer set also collapses, because
  peers cannot be compared on a multiple none of them supplies.
- **NVDA and TSM.** Both fast growers, and neither finds four peers inside C2's ±10pp growth band — a company growing 50% has few genuine size-and-growth matches. This is the single largest driver of the SINGLE-ANCHOR rate, and it is a property of the screen rather than of the data source.
- **TSM (ADR).** The underlying currency is unmapped. K5 reports
  `UNDERLYING EXPOSURE: UNMAPPED` rather than crediting the exposure to USD —
  an operator who believes they hold one currency may hold two.
- **SIVBQ (delisted).** Absent entirely. This is survivorship bias arriving as
  a 404 rather than as a caveat. The probe records it as *unknown*, not as
  passing.

### Stop conditions

| # | Condition | Result |
|---|---|---|
| 1 | cash-burn and share-count history computable | **TRIPPED** (RIVN) |
| 2 | seven years of historical multiples sourceable | **TRIPPED** (RIVN) |
| 3 | GICS sub-industry codes available | **TRIPPED** |
| 4 | SINGLE-ANCHOR MODE below 40% of test companies | **TRIPPED** (56%) |

### Build directives these imply

1. **Disable SPEC-GROWTH entirely** rather than building around the gap.
   Without A3's pre-profit branch and A4's dilution flag, SPEC-GROWTH is not a
   strategy — it is a way to buy companies shortly before they run out of money.
2. **Report the shortened C1 window; do not silently shorten it.** The system
   degrades toward single-anchor operation, which C5 penalises but does not
   make free.
3. **Report the fallback taxonomy level before building.** A2 and C2 run at
   industry level against a vendor taxonomy, logged `VENDOR-SUBSTITUTE`.
4. **The dual-anchor premise does not hold at the assumed rate.** At 56%,
   SINGLE-ANCHOR MODE is well above the 40% threshold. §18 is explicit that
   this is a design-level finding, not a data gap to work around. The dominant
   cause here is C2: a genuine peer set of four names matched on grouping,
   size *and* growth is harder to assemble than the spec assumes.
5. **State the point-in-time limitation in the report header,** not as a
   footnote: backtest results are inflated by an unknown material amount.

---

## Two bugs the probe found in itself

Both would have produced a confidently wrong headline finding, which is the
argument for running a probe against known data before trusting it against
unknown data.

1. **Every target looked like SINGLE-ANCHOR MODE.** The probe passed `None` as
   each peer's multiple into C2's screen, so every peer was rejected for
   "multiple not computable" and the SINGLE-ANCHOR rate was 100% — an artefact
   of the probe, not a property of any source. Fixed by adding
   `compute_current_multiple()` so peers are built the same way the subject is,
   and by separating peers the source *names but cannot describe* from peers
   that genuinely failed the screen.

2. **Stop condition 2 tripped for every source.** It compared the usable window
   against a full 7.0 years, but a series fetched for a 7-year window has its
   newest and oldest observations *inside* that window and so can never span
   quite 7.0 years. A condition that always fires carries no information. Fixed
   by applying the same tolerance C1 uses, and by distinguishing a source that
   lacks history from a series C1.1 correctly truncated at a corporate action.

---

---

## Part 3 — The free stack, and what it changes

Sections 1 and 2 were written when the assumed data source was a paid vendor.
The system now defaults to a free stack — **SEC EDGAR** for fundamentals and
**Stooq/Yahoo** for prices — which changes several findings, mostly for the
better.

### 3.1 Point-in-time data is now available, free

This was the ~$250/month line item. EDGAR serves data **as filed, with filing
dates**, because it is the source of record — it is where the vendors get it.

The composite adapter uses this properly: C1's multiple series observes each
quarter on the date it was **filed**, not the date the period ended. A
December quarter is not knowable until it is filed in February, so the trailing
multiple on 15 January uses the September quarter. Matching on period end
instead would hand the model months of information it could not have had, and
would flatter every backtest in a way invisible in the output.

So §13.8's warning — *state in the report header that results are inflated by
an unknown material amount* — **no longer applies** on the free stack. That is
the single largest quality gain from the change, and it came from dropping the
paid vendor rather than adding one.

### 3.2 GICS is unavailable from *any* source at this scale

Not an FMP limitation. GICS is licensed by MSCI/S&P at institutional rates and
is not sold to individuals at all. EDGAR provides **SIC codes**, which are
official but older and broader than GICS.

The consequence is unchanged and now permanent: A2's leverage median is
computed over a wider grouping than the spec assumes, making the gate **more
permissive than designed**. Every grouping is logged `VENDOR-SUBSTITUTE`, and
Module I's peer-gameability break criterion should be monitored from year one
rather than year five.

### 3.3 Two paths remain blocked, and paying does not fix them

| Path | Needs | EDGAR | Typical retail vendor |
|---|---|---|---|
| B4 REIT | AFFO / FFO | ✗ custom XBRL extensions | ✗ usually absent |
| B5 Insurer | combined ratio | ✗ not a us-gaap tag | ✗ usually absent |
| B3 Bank | tangible book | ✓ derivable from standard tags | ✓ |

REIT and insurer valuation cannot run on either stack. §18's stop-condition-1
reasoning applies: an unenforceable path should be **disabled**, not built
around. That leaves four working classifications — CORE-STABLE, CORE-GROWTH,
SPEC-GROWTH (data permitting), and FINANCIAL-BANK.

### 3.4 Analyst estimates are unavailable

C3's PEGY reports `n/a`, which the spec explicitly permits (*"Where undefined,
log 'PEGY n/a.' Never impute"*).

CORE-GROWTH's C1 anchor is specified as forward P/E, which needs estimates.
Rather than let a forward series quietly fill with trailing values, the free
stack sets `anchors.core_growth_multiple = "trailing_pe"` explicitly, C1 logs
the substitution on every name, and the change shows up in the config
fingerprint. An earlier version of `compute_current_multiple` performed exactly
the silent fallback this avoids; that was a genuine imputation bug and is fixed.

### 3.5 Corporate actions are only half-covered

Splits come from the price feed. **Spinoffs do not** — EDGAR publishes no
corporate-action feed, and spinoff announcements live in 8-K narrative text
this system does not parse.

C1.1 therefore sees splits but not separations, which is the case it exists
for: *"A spinoff in particular changes what the company is."* The adapter
reports `corporate_actions` as unsupported rather than returning an empty list,
because an empty list reads as "no spinoffs occurred" and would silently defeat
the gate. A missed spinoff leaves a step change in the series that C1.2 then
reports as a possible re-rating — the right flag for the wrong reason, and
worth knowing about when reading one.

### 3.6 Revised cost

| | Free stack | Paid vendor |
|---|---|---|
| Monthly | **$0** | ~$100 |
| Point-in-time | ✓ built in | ✗ +$250/mo |
| Working classifications | 4 of 6 | 4 of 6 |
| Setup effort | higher | lower |
| Reliability | scrappier | steadier |

The free stack is not merely cheaper. On point-in-time correctness — the axis
that decides whether a backtest means anything — it is **better**.

---

## What is still unverified

- **No live probe run against any real source.** Blocked by this environment's
  egress policy, which refuses EDGAR, Stooq and Yahoo alike (`CONNECT tunnel
  failed, response 403`). The probe and the full screen both run end to end
  against fixtures; neither has met live data. **Run the probe on your own
  machine and treat its output, not this document, as the finding for your
  source.**
- **XBRL tag coverage across the real filer population.** The tag fallback
  chains cover the common cases and are tested against realistic payloads, but
  filers tag inconsistently and some will need chain extensions.
  `EdgarAdapter.field_coverage(symbol)` reports which tag supplied each field
  for one filer, which is the tool for diagnosing a surprising gate value. The
  fix for a miss is to extend the chain — never to guess a number.
- **Whether the price feeds hold up.** Stooq and Yahoo are both unofficial for
  this purpose. The fallback source records which one answered; two sources
  disagreeing is a thing worth being able to notice.
- **Whether SPEC-GROWTH survives.** Stop condition 1 depends on cash-burn and
  share-count history being computable across real filers. If it is not, the
  spec is explicit: disable the path rather than build around the gap.
