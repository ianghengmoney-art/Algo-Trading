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
