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
| NVDA | profitable fast-grower | 100% | CORE-GROWTH | 7.0 | 4 | dual |
| RIVN | unprofitable grower | 64% | SPEC-GROWTH | 1.7 | 0 | **both uncomputable** |
| JPM | bank | 96% | FINANCIAL-BANK | 7.0 | 5 | dual |
| O | REIT | 86% | REIT | 7.0 | 0 | **single** |
| PGR | insurer | 96% | INSURER | 7.0 | 5 | dual |
| TSM | foreign ADR | 96% | CORE-GROWTH | 7.0 | 3 | **single** |
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
| 4 | SINGLE-ANCHOR MODE below 40% of test companies | **TRIPPED** (44%) |

### Build directives these imply

1. **Disable SPEC-GROWTH entirely** rather than building around the gap.
   Without A3's pre-profit branch and A4's dilution flag, SPEC-GROWTH is not a
   strategy — it is a way to buy companies shortly before they run out of money.
2. **Report the shortened C1 window; do not silently shorten it.** The system
   degrades toward single-anchor operation, which C5 penalises but does not
   make free.
3. **Report the fallback taxonomy level before building.** A2 and C2 run at
   industry level against a vendor taxonomy, logged `VENDOR-SUBSTITUTE`.
4. **The dual-anchor premise does not hold at the assumed rate.** At 44%,
   SINGLE-ANCHOR MODE is above the 40% threshold. §18 is explicit that this is
   a design-level finding, not a data gap to work around.
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

## What is still unverified

- **No live probe run against a real provider.** Blocked by this environment's
  egress policy (1.5). The probe is ready; it needs an environment with network
  access and an entitled API key. Run it and treat its output, not this
  document's Part 2, as the finding for that source.
- **Whether any accessible source supplies REIT AFFO, insurer combined ratios,
  and bank tangible book.** These are the three fields most often missing, and
  they are exactly what B3/B4/B5 cannot run without. If none does, those three
  classifications should be disabled on the same reasoning stop condition 1
  applies to SPEC-GROWTH.
- **Whether GICS is obtainable at acceptable cost.** If it is, stop condition 3
  clears and A2/C2 run as specified. If not, the vendor-substitute path is
  permanent and Module I's peer-gameability criterion should be monitored from
  the first year rather than the first cycle.
