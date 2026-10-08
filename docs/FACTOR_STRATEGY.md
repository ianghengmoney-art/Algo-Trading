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
