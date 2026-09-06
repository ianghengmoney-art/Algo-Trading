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

---

## Part 4 — the first live run, and what it actually measured

**2026-09-06.** The probe was run against real EDGAR for the first time, on a
machine with egress. Targets: CAT, GE, NVDA, TSM, PGR, O, JPM, RIVN, TPL.

It tripped four of the stop conditions in §18. **Three of the four were
defects in this parser, not properties of the data.** They are listed here
because the distinction is the entire point of a feasibility probe: a stop
condition that fires on a bug tells you to disable a working strategy path.

| What the run reported | Cause | Status |
|---|---|---|
| TTM operating cash flow missing on every target | 10-Q cash-flow statements are cumulative within the fiscal year. Q3 reports nine months, not three. The parser read them as quarters, then failed the duration check. | Fixed — `xbrl._to_quarterly` differences consecutive facts sharing a period start, and respects the fiscal-year boundary. |
| Every target had 0 peers → 78% routed to SINGLE-ANCHOR | The probe asked the adapter for a vendor peer list. EDGAR has none. C2 was never given the universe-built peers the screen actually uses. | Fixed — `run_probe(peer_sample=N)` builds a universe sample first. Re-run with `--peer-sample 200`. |
| A4 read a 3-for-1 split as 73%/yr dilution | Share counts are as-reported, so a split is a step change in the series. | Fixed — `a_health.normalise_share_counts` walks the series oldest-first, detects ratio jumps outside 0.80–1.20, and carries a cumulative factor. Genuine ~19%/yr issuance still trips the flag. |
| TSM: 21% coverage, almost nothing computable | **Real.** Foreign private issuers file 20-F under the `ifrs-full` namespace; the parser only read `us-gaap`. Partly fixed — `NAMESPACES` now covers `ifrs-full` and `dei`, and the tag chains carry IFRS names. Coverage will improve but will not reach a domestic filer's: IFRS statements genuinely differ. |
| REIT AFFO and insurer combined ratio absent | **Real, and permanent.** Both are custom XBRL extensions, not `us-gaap` tags. These two paths cannot be valued from EDGAR alone. |
| GICS sub-industry unavailable | **Real, and permanent.** SIC is the finest official rung. Every A2 and C2 result that uses it is logged `VENDOR-SUBSTITUTE`. |

Two further problems the run exposed, neither of which had tripped a stop
condition:

- **Zero debt and unreadable debt looked identical.** `total_debt` returned
  `None` for both a genuinely debt-free filer (TPL) and one whose debt tags
  this parser could not resolve. A2 refused in both cases. `_total_debt` now
  returns a value *and a basis* — `tagged`, `summed`, `long_term_only`, or
  `inferred_zero`. A zero is inferred only when the balance sheet reads
  cleanly *and* no debt tag, nor any evidence of debt activity (interest paid,
  borrowings repaid), appears anywhere in the filing history visible at the
  as-of date. The inference is logged in A2 and named in the probe, so it is
  never mistaken for a measured figure.
- **`LongTermDebt` was being added to its own current portion.** The `us-gaap`
  concept already includes current maturities. Summing it with
  `LongTermDebtCurrent` double-counted that portion and overstated leverage,
  which fails A2 on companies that should pass. The three debt buckets are now
  disjoint by construction and `LongTermDebt` is used only as a whole.

### What this means for the §18 BUILD DIRECTIVES

The directives the first run emitted should be **ignored**. They were
measuring the parser. Re-run after pulling these fixes:

```
python scripts/data_feasibility_probe.py --source edgar \
    --user-agent "your@email.com" --peer-sample 200
```

Only the second run's stop conditions are evidence about the data.

---

## Part 5 — the second live run: one serious bug, and better questions

**2026-09-06, second run**, with `--peer-sample 200`. Coverage rose (CAT 68% →
82%, NVDA 71% → 82%), TPL's A2 leverage now computes, and 8-K corporate
actions are being read (TPL 2, GE 8, where the first run saw none). Four stop
conditions still tripped, and the reasons had changed — which is the point of
re-running.

### The fourth quarter was missing from every company, every year

Not a coverage gap. A correctness bug, and the worst one found so far.

A 10-K states the fiscal year. **It does not state Q4** — no filer reports
that quarter on its own; it exists only as the year minus the nine months
already reported. Nothing in EDGAR fills the gap, and this parser did not
derive it, so Q4 was absent from every company in every year.

The damage was not a sparse series. `trailing_quarters(4)` reaches back past
the hole, so every TTM figure was **Q1+Q2+Q3 of one year plus Q3 of the year
before** — one quarter double-counted, another dropped. Trailing earnings,
trailing cash flow, EBITDA, and every multiple built on them were wrong by
that difference, for every company, in both anchors.

`xbrl._with_derived_fourth_quarter` now recovers it. The regression test that
matters: **the four quarters must sum to the reported fiscal year.** Derived
quarters are dated by the 10-K's filing date, not the year end, so a backtest
does not get six weeks of advance notice; a filer that does tag its own Q4
keeps it.

This is also most of stop condition 2. Losing one quarter in four cost the C1
reconstruction about a year of usable span at the front, which is why eight of
nine targets came back under 6.75 years.

### The peer pool was built by alphabetical slice

`0 same-grouping names in a 72-name universe sample` — the peer failure was
real, but it belonged to the sampler. `default_symbol_list` returns tickers
sorted alphabetically, so `--peer-sample 200` took the first 200 tickers in
the alphabet, 72 of which survived the size and liquidity filters. The chance
that four of them are oil royalty traders, or construction-machinery makers,
is approximately zero.

Peers are now drawn from each target's **own SIC code** via EDGAR's
company-by-SIC listing (`EdgarAdapter.symbols_by_sic`), not sampled from the
market and hoped over. Until this run, stop condition 4 was measuring the
sampler.

### Stop condition 1 was asking the wrong companies

It disabled SPEC-GROWTH because cash runway was not computable for **CAT and
TSM** — a profitable industrial and a profitable chipmaker, neither of which
could ever route to SPEC-GROWTH. A profitable company has no cash runway to
compute. The condition is now assessed on the targets that could actually take
that path; gaps elsewhere are reported as the A3 coverage gaps they are,
rather than as grounds for deleting a strategy.

### Delisted companies are invisible, and that is a survivorship problem

`SIVBQ not in the SEC ticker index` is correct and important.
`company_tickers.json` lists **currently registered** tickers only. A company
that delisted is absent from it while its filings remain on EDGAR in full. A
universe built from that index therefore contains survivors and nothing else,
and a backtest over survivors reports the returns of the companies that made
it — the most flattering error a backtest can make.

Filings stay addressable by CIK forever, so `ticker_to_cik` now accepts
`CIK0000719739` directly and takes a `cik_overrides` map. The escape hatch
exists; **populating it for the delisted names in a historical universe is
still manual work, and until it is done the backtest is survivor-biased.**
That is a live limitation, not a fixed one.

### Still real, still unfixed

- **GICS** — permanent. SIC is the finest official rung.
- **REIT AFFO, insurer combined ratio** — permanent. Custom XBRL extensions.
- **NVDA's interest expense** — the chain covers eight tags and still misses
  it. `EdgarAdapter.field_coverage("NVDA")` will name the tag NVDA actually
  uses; until then this is an open gap, not a diagnosis.
- **TSM** — improved by IFRS support, and will not reach a domestic filer's
  coverage. IFRS statements genuinely differ.

---

## Part 6 — the rest of run 2: a misclassification that mattered

The remaining targets (TSM, PGR, O) surfaced one bug with real consequences
and three structural facts about non-industrial filers.

### TSM was classified SPEC-GROWTH

A profitable, trillion-dollar chipmaker was routed to the single riskiest
classification the system has. The cause was two lines deep.

TSM files 20-F, not 10-Q, so it has **no quarterly facts at all**.
`trailing_quarters(4)` therefore returned an empty list — and `_sum` of an
empty list is `0.0`. Zero is a number. TTM net income read as zero, `0 > 0` is
false, so the company read as *unprofitable* rather than *unmeasured*, and the
SPEC-GROWTH branch is the one that wants an unprofitable grower.

Two things were wrong and both are fixed:

- **`_sum` now refuses an empty or short sequence.** It also takes an
  `expected` count, and every trailing-twelve-month call site goes through a
  `_ttm` helper that demands exactly four quarters. Nine of the eleven call
  sites had no count check at all, so a three-quarter total could be presented
  as a year — the precise failure the function's own docstring warned about.
- **The SPEC-GROWTH branch is asymmetrically easy to reach**, and now says so
  in a comment that explains why it must stay that way. CORE-GROWTH and
  CORE-STABLE each demand multi-year evidence; SPEC-GROWTH needs one year of
  revenue and one negative signal. A company with thin data can satisfy that
  test and no other, and so lands in the riskiest bucket for want of
  information rather than on its merits. Both negative signals are now
  required to be *measured*, never inferred from absence.

A6's refusal message also names the inputs that were unavailable. "Satisfied
no classification's criteria" reads as a verdict on the company when it is
usually a verdict on the data, and the two call for different responses.

### Realty Income's debt read as $0.8bn

O's A2 passed on `net_debt = 8.474e+08`. Realty Income carries roughly $20bn.

REITs present an **unclassified balance sheet** — debt is split by security
(secured / unsecured), not by maturity — so none of the three maturity buckets
resolved and only a fragment was picked up. A fourth bucket now reads debt by
security, used when the maturity buckets come up empty.

More importantly: **a partial debt read is more dangerous than a missing one.**
A chain that reads nothing refuses the gate. A chain that reads a fragment
hands A2 a plausible number and nothing downstream can tell. There is now a
plausibility check — a company whose liabilities are a real share of its
assets, but almost none of which reads as debt, is flagged in the ledger and
in A2's detail. It reports rather than blocks, because the figure may be
right; a genuinely debt-free company has small liabilities too and does not
trip it.

### Three structural facts, not bugs

- **20-F filers have no quarterly data at all.** Every TTM gate and the whole
  C1 series are structurally unavailable for them, not merely unread, and no
  tag-widening changes that. The adapter now says this explicitly instead of
  reporting it as a parser gap. TSM's `C1 usable history: 0.0 years` is
  correct and not fixable from this source.
- **Insurers have no current ratio and no meaningful EBITDA.** PGR shows both
  missing. Insurers do not present a classified balance sheet, and EBITDA is
  not a measure anyone applies to an underwriter. A1 and A2 are asking
  questions that do not apply to this classification — a Module A design
  question, not a data gap, and it is **open**.
- **O's share count grew 14.8% a year.** That is real: issuing equity is how a
  REIT funds acquisitions. A4's dilution flag will fire on essentially every
  REIT, which makes it noise on that path rather than a signal. Also **open**.

PGR reached 6.7 years of C1 history — the closest any target has come to the
full seven-year window.

---

## Part 7 — JPMorgan, and gates that answer the wrong question

The bank target produced three numbers that were read correctly and mean
nothing:

```
A1  ttm_operating_cash_flow = -2.53e+11
A2  net_debt                = -2.37e+11
A3  cash_runway_months      = 14.71
```

None of these is a parsing error. A bank's operating cash flow swings by
hundreds of billions with loan origination and the trading book; deposits are
its raw material, not its leverage; and EBITDA is not a measure anyone applies
to it. The last line is the dangerous one — **the system calculated that
JPMorgan has 14.7 months before it runs out of money.** That is not a finding,
it is an artefact of running a pre-profit startup test on the largest bank in
America, and it looks exactly like a finding.

Module A is built for operating companies. There is now a third answer
alongside pass and fail:

- **`NOT_COMPUTABLE`** — the input was missing. Go and find better data.
- **`NOT_APPLICABLE`** — the *question* is wrong for this business. No amount
  of better data will produce an answer.

Conflating the two sends someone hunting for a tag that will never exist. A1,
A2 and A3 now return `NOT_APPLICABLE` for banks and insurers, each naming what
the right test would be (capital adequacy — tier-1, RBC — which EDGAR does not
tag reliably).

A not-applicable gate does not block: refusing every bank and insurer outright
is not what the spec asks for. But it is not a clearance either. `run_module_a`
records which gates did not run, `HealthAssessment.screened_fully` reports
whether all of them did, and the ledger says so plainly:

> Module A ran 4 of 7 gates — A1, A2, A3 do not apply to this business. A pass
> here rests on less screening than a pass on an operating company, and is
> weaker evidence by exactly that much.

**This is a real reduction in safety for financials, not a fix for one.** A
bank that clears Module A has been screened less thoroughly than an
industrial that clears it, and the position sizing does not currently know
that. Whether financials should be in the universe at all on this data source
is a question for the operator, and it is now an informed one.

### The peer lookup was failing silently

The run showed `universe: 0/210 (CAT)` then `ACGCW`, `AEAQ` — an alphabetical
slice, which means the SIC-targeted lookup returned nothing for every target
and the fallback fired without a word.

The cause: EDGAR's company-by-SIC endpoint returns two undocumented shapes. A
single match redirects to a filing list whose links carry `CIK=0000320193`; a
multi-company match returns `<CIK>0000320193</CIK>` elements. The parser read
only the first, so every industry query came back empty.

Both shapes are now read. More importantly, **the fallback announces itself**:

> PEER POOL IS NOT INDUSTRY-MATCHED. No industry listing came back for any
> target, so candidates are an alphabetical slice of the ticker index. Peer
> findings below — and stop condition 4 — describe that slice, not the market.

A silent fallback produced a whole run of peer findings that looked like
evidence about the market. That is the failure mode worth engineering against,
more than the regex itself.
