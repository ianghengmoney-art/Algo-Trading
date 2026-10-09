# Factor strategy — pre-registered rules (written 2026-10-08, before any test)

GCFP v4 was tested on 1,000 companies over 2015-2025 with every known data
fault fixed (run #18). Its picks trailed the S&P 500 over their own holding
periods (median -4.4%, 26 of 56 beat it), it held 3.6% of the money in picks,
and two of its own Module I break criteria tripped. This document fixes the
rules of the next strategy **before it is run once**, so that the result
cannot be tuned to the past.

## Why this design

GCFP failed for structural reasons: two valuation anchors that free data
rarely supports at once, gates so strict that almost nothing was bought, and
a tiny invested share. The design below keeps what the evidence supports and
drops what it does not:

- **Cheapness, profitability and momentum** are the three stock
  characteristics with the longest published record of higher average
  returns (Fama-French value and operating profitability; Novy-Marx;
  Jegadeesh-Titman 12-1 momentum). None is guaranteed, and each has had
  multi-year stretches behind the market.
- **Ranks, not thresholds.** Every eligible company is ranked every month,
  so the portfolio is always fully invested in the best-ranked names rather
  than waiting for a rare pass.
- **Only numbers free data supports well**, all already validated in this
  codebase: TTM net income, TTM free cash flow, TTM EBIT, total assets,
  market cap on the as-of price basis, split-adjusted prices.

## Universe (each month-end, point in time)

1. The same seeded point-in-time sample as the GCFP backtest (companies that
   filed during the period, dead ones included), screened only while filing.
2. The same universe screen as GCFP: market cap at least $300M, 3-month
   average daily dollar volume at least $2M, REITs and foreign private
   issuers excluded.
3. Banks and insurers excluded: earnings yield, free cash flow and operating
   profit on assets do not measure the same thing for a balance-sheet lender.

## Scores

Each is a percentile rank (0-1) among the month's eligible companies with
that input available. Higher is better.

| Score | Definition |
|---|---|
| Value | mean of the ranks of earnings yield (TTM net income / market cap) and free-cash-flow yield (TTM operating cash flow + capex, / market cap), over whichever of the two exist |
| Profitability | rank of TTM EBIT / total assets (latest quarter) |
| Momentum | rank of the split-adjusted price return from 12 months to 1 month before the date |

**Composite** = mean of the scores available; a company needs at least two
of the three to be ranked.

## Portfolio

- Hold the **30** highest composite scores, equal weight (1/30 of the
  portfolio's value at purchase).
- **Buffer:** a holding is kept while it ranks in the top **60**; it is sold
  when it falls below 60 or leaves the eligible universe. Empty slots are
  filled from the top of the ranking.
- Rebalanced monthly. Positions are not trimmed back to equal weight.
- Money not invested (fewer than 30 eligible names) follows the S&P 500
  total-return index, as in the GCFP backtest.
- Every fill costs 0.1%. Dividends are credited on ex-dates. A holding whose
  price disappears is closed at -30% after one missed month (as GCFP).

## Test

- Period 2015-01-31 to 2025-09-30, monthly; the same walk-forward split as
  GCFP: **earlier half** to 2021-06-13, **later half** after.
- Benchmarks: the **S&P 500 total-return index**, and an **equal-weight
  portfolio of every eligible company** (rebalanced monthly, same costs),
  which separates stock selection from simply owning smaller companies.

## Pass criteria — all must hold

1. Beats the S&P 500 total return in the earlier half **and** in the later
   half.
2. Beats it by at least **1%/yr** over the full period, after costs.
3. Beats the equal-weight eligible universe over the full period.

If any criterion fails, the strategy has no demonstrated edge on this data,
and the honest recommendation is an index fund.

## What is not allowed

- Changing any number or definition above after seeing a result. A variant
  is a new, separately registered test, and every report states how many
  variants have been tried (this is variant **1**).
- Using the later half to choose anything. Both halves are reported
  together from one run of fixed rules.

## Known limits, stated up front

- Survivorship: most companies that died have no free price history, so they
  could never be bought; results lean optimistic.
- No data before 2009, so the 2000-02 and 2008 crashes are not covered.
- Fills at the month-end close are optimistic about liquidity.

---

# Variants 2 and 3 — registered 2026-10-08, while variant 1 was still running

Registered **before any result of variant 1 was known**, so neither design
was influenced by it. Together with variant 1 this is a family of three,
fixed now; no further variant is added on the strength of these results
without saying so and counting it.

The operator's goal is **5%/yr above the S&P 500**. Published evidence puts
long-run factor premiums nearer 1-3%/yr, smaller since publication, and the
2015-2025 period favoured large companies over small ones by roughly
5%/yr. The criteria below say whether an edge exists at all; whether the
5% goal is met is reported separately, and is not lowered.

## Variant 2 — broader evidence, same universe as variant 1

Same universe, portfolio (30 names, top-60 buffer, equal weight, monthly),
costs and benchmarks as variant 1. The scores change: four **themes**, each
the mean of its available component ranks; the composite is the mean of the
themes, and a company needs at least **three** of the four.

| Theme | Components (each a percentile rank, higher is better) |
|---|---|
| Value | TTM EBIT / enterprise value; TTM free cash flow / enterprise value. Enterprise value = market cap + total debt - cash, latest quarter |
| Quality | TTM gross profit / total assets; low accruals = -(TTM net income - TTM operating cash flow) / total assets |
| Shareholder yield | -(split-adjusted share count growth over the last year) — buybacks rank high, issuance low |
| Momentum | 12-1 month price return; earnings growth = (TTM net income - the TTM net income a year earlier) / total assets |

Evidence: enterprise multiples (Loughran & Wellman 2011), gross profitability
(Novy-Marx 2013), accruals (Sloan 1996), net share issuance (Pontiff &
Woodgate 2008), price momentum (Jegadeesh & Titman 1993), earnings momentum
(Bernard & Thomas 1989; Chan, Jegadeesh & Lakonishok 1996). Each theme is
weighted equally; no weight is fitted to data.

## Variant 3 — variant 2 on larger companies

Identical to variant 2 except the universe keeps only companies with a market
cap of at least **$2 billion** on the date, so the strategy competes with the
S&P 500 on its own ground rather than carrying the small-company headwind.
It needs a larger sample to have enough such companies: the largest sample
that finishes inside the job's time limit, at least 2,000, chosen from run
times (not results).

## Choosing among the three

All three are reported, whatever they show. A variant **passes** on the
variant-1 criteria (beats the S&P 500 total return in both halves, by at
least 1%/yr over the full period, and beats its own equal-weight universe).
If more than one passes, the one with the higher full-period excess return
goes to paper trading, which is the final, out-of-sample test. If none
passes, none goes forward, and the recommendation is an index fund.

---

# After the backtest — paper trading and real money (registered 2026-10-08, before any result)

## Stage 2 — paper trading (the winning variant only; at least 3 monthly rebalances)

Runs automatically with the weekly job: marked to market every week,
rebalanced on the first run of each month, reported in
`reports/paper/factor-vN-report.txt` with that month's trades by ticker and
share of the portfolio.

It **passes** when, over at least three monthly rebalances:

1. every monthly rebalance completed inside the job's time budget (a missed
   month that the next week's run completes counts as completed);
2. no evaluation raised an error, and no holding went unpriced for longer
   than the delisting rule allows without a known reason;
3. it is not more than 15% behind the S&P 500 total return since the start
   (three months is far too short to judge an edge, so this only catches a
   broken pipeline, not bad luck).

A failure on 1 or 2 is fixed and the three months restart. A failure on 3 is
reviewed before anything else happens.

## Stage 3 — real money (only after stage 2 passes)

- Most of the money stays in a low-cost S&P 500 index fund.
- The strategy starts with **10-20%** of the money meant for stocks, and is
  scaled up over 6-12 months only while it behaves as the backtest said
  (within its tracking error).
- The system only alerts. Each month's trades are placed by the operator
  from the report's trade list, scaled to the real portfolio.
- Real-world costs the backtest does not model, stated now: 30% US
  withholding tax on dividends for a Singapore resident, currency
  conversion, and brokerage on roughly 30 names.

## Stage 4 — keeping it honest once live

- Monthly: trade the list. Quarterly: compare with the S&P 500 total return.
- **Stop rule:** if the strategy trails the S&P 500 total return by more than
  5%/yr over any rolling three years, stop adding money and review. Shorter
  stretches behind are expected of factor strategies and are not a reason to
  quit.

---

# Operator goal revised — 2026-10-08

The goal is now an absolute **15-20%/yr compound return** (previously 5%/yr
above the S&P 500). Like the earlier goal, it is reported in every result
and is **not** a pass criterion: it does not lower or raise the registered
bar, and it does not choose between variants.

Stated alongside it: in 2015-2025 the S&P 500 total return was about
14%/yr, well above its long-run average of about 10%/yr. A strategy's
compound return is the market's return plus its edge; only the edge is in
the strategy's control. With a market return of 7-10%/yr and an edge of
1-2%/yr, expect roughly 9-12%/yr. Reaching 15-20% would need an unusually
strong market, or leverage or concentration, which raise the risk of large
losses as much as the return; neither is part of any registered variant.

---

# Long-history premise check — registered 2026-10-08, before any data was downloaded

The stock-level backtest cannot reach before 2012: the SEC's machine-readable
financial data begins in 2009-2011. The idea behind the strategy (cheap,
profitable, rising stocks beat the market) can be tested much further back
with Kenneth French's free data library (Dartmouth), built from CRSP and
Compustat: monthly returns of portfolios sorted on exactly these
characteristics, from 1963, survivorship-free, through the 2000-02 and
2008-09 crashes.

It tests the **premise**, not the exact strategy: French's portfolios use
his definitions and annual (momentum: monthly) re-sorting, not our ranks.

## Proxies (monthly, each theme equal-weighted, rebalanced monthly)

From the 2x3 size-by-characteristic portfolios, value-weighted returns of
the **high** third ("Hi") in **big** stocks (above the NYSE median) and,
separately, small stocks:

| Proxy | Themes |
|---|---|
| A (variant 1's idea) | value = mean(high E/P, high CF/P); profitability = high operating profitability; momentum = high prior 2-12 month return |
| B (variants 2-3's idea) | A's three themes, plus low accruals (lowest quintile) and low net share issuance (no net issuance, i.e. buybacks), from the univariate portfolio files, where each is available |

A cost of **0.5%/yr** is subtracted from every proxy (French's returns carry
no trading costs). The market is French's market return including
dividends (Mkt-RF + RF).

## Reported

Compound return, excess over the market, monthly t-statistic and worst fall,
for 1995-2025 (30 years) and from 1963 to the latest month; each decade of
1995-2025; and the §13.2 windows 2000-02 and 2008-09.

## Supports the premise if the big-company Proxy A

1. beats the market over 1995-2025, **and**
2. beats it over the full history from 1963, **and**
3. beats it in at least 2 of the 3 decades 1995-2004, 2005-2014, 2015-2025.

If it does not, the stock-level result is more likely luck or this decade
than a lasting edge, and that is said plainly. The check never changes a
registered variant or the choice between them.

## Stock-level extension

The winning variant is also run from **2012-01-01**, the earliest start the
SEC data supports, as a robustness check reported alongside the registered
2015-2025 result. It does not change the selection.

---

# Selection and an added robustness check — 2026-10-09, after all three results

Results: variant 1 passed (+1.60%/yr over the S&P 500 total return, t
+0.74), variant 2 passed (+3.58%/yr, t +1.27), variant 3 failed (-0.59%/yr,
t +0.07, on a 4,000-company sample). By the registered rule (all that pass;
the higher full-period excess wins), **variant 2 is selected** and goes to
paper trading.

Added now, **before it is run**, as a robustness check that cannot change the
selection: variant 2's unchanged rules on the **4,000-company sample**
variant 3 used. Variant 2 was tested on 1,000 companies, and one holding
(AppLovin) produced 31% of its gains; variant 3, on 4,000, found no edge.
Running variant 2 on the larger sample shows whether its result survives a
bigger, more representative universe. Reported plainly either way; if it does
not beat the S&P 500 there, the expectation for paper trading is set
accordingly (no demonstrated edge), and paper trading remains the deciding
test.

---

# Robustness results for variant 2 — 2026-10-09

| Test | Result vs S&P 500 TR | t | Top winners' share of gains | Without top 2 |
|---|---|---|---|---|
| Registered: 1,000 companies, 2015-2025 | +3.58%/yr | +1.27 | AppLovin 31%, FICO 16% | 12.9%/yr (index 14.0%) |
| Check: 1,000 companies, from 2012 | **-2.03%/yr** | -0.69 | Meta 16% | 11.2%/yr |
| Check: 4,000 companies, 2015-2025 | +7.46%/yr | +0.97 | AppLovin 36%, GameStop 22% (2021: +130% in one year) | 14.4%/yr (index 14.0%) |

Reading, stated plainly: every result that beat the index depended on one or
two extreme winners caught by the momentum rule (AppLovin, GameStop's 2021
squeeze); with those removed each is roughly the index or below, the
strategy is ahead of the index in only about half of months, and no t-statistic
reaches 2. The rules may hold a genuine tendency to catch big winners, as the
momentum literature suggests, but on this data that is **not distinguishable
from luck**. Variant 2 stays selected for paper trading as registered; per
stage 3, real money waits for paper trading, and even then is limited to
10-20% of stock money with the rest in an index fund.

---

# Variant 4 — the review's risk fixes (registered 2026-10-09, before the factor attribution result)

## Why a fourth variant, and why it is not data mining

Variants 1-3 were registered as a closed family of three. Variant 4 is not
added because of their returns. It answers a design review of variant 2
(the "CIO review" in the session log). That review found nothing wrong
with the *signals*. It found the *portfolio* wrong:

- 30 names, never trimmed: one winner could grow to a quarter of the book
  (AppLovin, GameStop), producing 48%/yr volatility and 45%/yr tracking
  error, so no result could be told from luck.
- No industry limit, flat 0.1% costs on small companies, and dividends
  credited in full although a Singapore resident loses 30% of them.

Variant 4 changes **only** those things. The signals, universe and test
period are variant 2's, unchanged, and no number below was chosen by
looking at any result. It was written down while the factor attribution
(run #27) was still running, before its result was seen. It is the 4th
factor variant tried. Counting GCFP v4's configurations too, about **8
strategies** have been tested on this data, and the deflated Sharpe ratio
in its report uses that count.

## Rules (differences from variant 2 only)

| | Variant 2 | Variant 4 |
|---|---|---|
| Holdings | 30 | **100** |
| Sell buffer | kept while in the top 60 | kept while in the top **200** |
| Position size | 1/30 at purchase, never trimmed | 1/100 at purchase; any holding above **2%** of the portfolio at a rebalance is **trimmed back to 1%** |
| Industry limit | none | at most **15** holdings in one 2-digit SIC major group; a candidate that would breach it is skipped |
| Cost per fill | 0.1% | by liquidity (3-month average daily dollar volume): **0.10%** at $50M+, **0.25%** at $10-50M, **0.50%** below $10M |
| Dividends | credited in full | **70%** credited (30% US withholding, Singapore resident). The benchmark, and money parked in it, lose the same 30% of the index's dividend yield |

The rebalance date stays month-end. The 4,000-company sample is variant 3's
and the 2015-01-31 to 2025-09-30 period is unchanged.

## Pass criteria: all must hold

1-4. Variant 2's four: beats the S&P 500 total return in each half, by at
     least 1%/yr over the full period, and beats the equal-weight eligible
     universe. All are judged against the benchmark after the same 30%
     dividend withholding, so both sides pay the same tax.
5.   **Alpha:** the Fama-French 5-factor + momentum regression of its
     monthly returns (gcfp/backtest/attribution.py) has an intercept above
     zero with a **t-statistic of at least 2.0**.

Criterion 5 is new and deliberately strict. It asks whether the strategy
earns something a cheap factor ETF does not. A strategy that passes 1-4 but
fails 5 earned factor premia. It passes as a *factor* strategy but not as
*alpha*, and the honest recommendation is a low-cost multi-factor fund
instead of maintaining this code.

## What happens with each result

- **Passes all five:** variant 4 replaces variant 2 in paper trading, and
  the three months of stage 2 restart.
- **Passes 1-4, fails 5:** variant 2's paper trading continues as the
  pipeline test it is. The recommendation becomes an index fund core plus,
  optionally, a multi-factor ETF in place of a self-run strategy.
- **Fails any of 1-4:** as above, without the factor-fund option.

## Reported, never used to choose (sensitivity)

The same variant 4 rules are also run with the rebalance on the **7th, 14th
and 21st** of each month instead of month-end, to show rebalance-timing
luck, and with **50 and 200** holdings (buffers of 100 and 400). These
results are reported as a range around variant 4. They are never used to
pick a configuration. A wide range means the headline number is fragile.

## Not fixed here, stated up front

- **Survivorship:** only 4% of companies that died have free price
  histories. Fixing it needs licensed data (Norgate, Sharadar or CRSP), a
  cost the operator decides on. Results stay an upper bound until then.
- **2000-02 and 2008:** the SEC's XBRL data starts in 2009. The long-history
  check (Ken French data, 1963 on) covers those years for the premise only.
- **Volatility targeting / beta overlay:** not added. It would add a
  market-timing rule with its own parameters, which means more chances to
  overfit. Diversifying to 100 names attacks the same 48% volatility
  directly.
