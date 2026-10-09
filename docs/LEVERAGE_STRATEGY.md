# Leveraged trend strategy: pre-registered rules (written 2026-10-09, before any test)

## Why this, after the factor strategies failed

The operator's goal is an absolute **15-20%/yr compound return**. Every
stock-selection test so far (GCFP v4, factor variants 1-4) found no edge over
the S&P 500 once luck, costs and tax were counted. Variant 4 earned the
market's return plus known factor tilts, nothing more. So a higher return
cannot come from picking stocks. On the evidence it can only come from
**holding more of the market's own return**, which means leverage.

Leverage alone is dangerous. 2x the US market fell more than 95% in
1929-32, and it compounds badly when volatility is high: the drag is about
L²σ²/2 a year. The best-documented way to hold leverage while sidestepping
the worst crashes is a **trend filter**: hold the market only while it is
above its 10-month average, and hold Treasury bills otherwise (Faber 2007,
"A Quantitative Approach to Tactical Asset Allocation"). Leverage is
applied only in the up-trend regime (Gayed & Bilello 2016, "Leverage for the
Long Run"). Both papers' rules are used as published. Nothing here is
fitted.

## Data

Kenneth French's US market return (Mkt-RF + RF, CRSP value-weighted, all
NYSE/AMEX/NASDAQ stocks, dividends included, no survivorship bias) and the
one-month T-bill rate (RF), monthly, from July 1926 to the latest month
published. This is a century of data, including 1929-32, 1937, 1973-74,
1987, 2000-02, 2008-09, 2020 and 2022.

## Rules

At each month-end:

1. **Signal:** the market's total-return index against the average of its
   last 10 month-end levels, the current month included. Above it is
   **risk-on**; at or below it is **risk-off**.
2. **Risk-on next month:** hold L times the market. The borrowed (L-1)
   costs the T-bill rate plus **1.0%/yr**, a typical margin or futures
   financing spread.
3. **Risk-off next month:** hold T-bills (RF).
4. **Costs:** every switch between risk-on and risk-off costs **0.2%** of
   the portfolio (spread and slippage on a leveraged position), plus
   **0.3%/yr** running cost while risk-on (fund or broker costs).
5. The signal uses the month-end close and is traded at that close. On
   monthly data that means acting on the last trading day's close; the
   real-money version acts at the next open, which the 0.2% switch cost
   allows for.

## Registered variants (2), plus context rows

| Variant | Leverage while risk-on |
|---|---|
| **L1** | 1.5x |
| **L2** | 2.0x |

Reported for context, never chosen: buy-and-hold market (1x), trend at
1.0x, and buy-and-hold at 1.5x and 2.0x with no filter. The last two show
what the filter is for.

## Pass criteria: each variant, all must hold

1. Beats buy-and-hold market by at least **1%/yr** over the full period.
2. Beats buy-and-hold market in **each half** (split at the midpoint month).
3. Beats buy-and-hold market **after publication**: from January 2008,
   after Faber's paper, a period the rule's authors never saw.
4. Its **maximum drawdown is no deeper than -55%**, the S&P 500's 2007-09
   fall. That is the worst an ordinary index investor already sat through.

## Selection

- **Passes:** among the variants that pass all four, the higher leverage is
  selected, because the goal is return within the drawdown limit.
- **Neither passes:** the honest answer is that no tested way of taking more
  risk reaches the goal at an acceptable risk, and an unleveraged index fund
  remains the recommendation.
- The 15-20% goal is reported against each period (full, since 1990, since
  2008). It never chooses between variants, and it lowers no bar.

## Next steps if one passes

- **Stage 2, paper trading:** the monthly signal and position are computed
  from live S&P 500 total-return closes and reported by the weekly job, for
  at least 3 monthly signals, exactly as the factor strategy was.
- **Stage 3, real money:** a small share at first. Practical routes for a
  Singapore resident are a margin loan on an Ireland-domiciled S&P 500 fund,
  or S&P 500 futures. US-domiciled leveraged ETFs (SSO, UPRO) carry 30%
  dividend withholding and, above US$60,000, US estate-tax exposure for
  non-residents. Confirm with a tax adviser before using them.
- The system still only alerts. It never places an order.

## Known limits, stated up front

- The rule is well known. Its post-2008 record is the only out-of-sample
  part, and that is the test that matters most.
- Monthly data cannot see crashes inside a month. Oct 1987 (-21.5% in a
  month) hits a leveraged holder in full, and the drawdown limit counts it.
- Margin calls are not modelled. At 2x, a market fall of about 40-45% inside
  one month would breach typical margin; the monthly data shows whether that
  ever happened while risk-on.
- Counting these 2 variants, about 10 strategies have now been tested.

---

# Result — 2026-10-09 (run #29)

| 1927-05 to 2026-08 | CAGR | Deepest fall | Since 1990 | Since 2008 |
|---|---|---|---|---|
| Market, buy and hold | +10.30% | -84% (1929-32) | +11.04% (-50%) | +11.42% (-48%) |
| Trend, 1.0x | +9.25% | -43% | +9.41% (-19%) | +9.13% (-19%) |
| **L1** trend 1.5x | +11.55% | **-60%** (1929-33) | +12.25% (-29%) | +12.61% (-28%) |
| **L2** trend 2.0x | +13.38% | **-74%** (1929-35) | +14.79% (-38%) | +15.83% (-37%) |
| Buy and hold 2.0x, no filter | +12.21% | -99% | +15.64% (-81%) | +17.64% (-76%) |

Criteria 1-3 pass for both: each beats buy-and-hold by more than 1%/yr over
the century, in both halves, and after publication. Criterion 4 fails for
both: the Great Depression takes L1 to -60% and L2 to -74%, beyond the -55%
limit. **By the registered rules neither passes, and none is selected.**

Reading, stated plainly:

- This is the first rule tested here that improved on the index over a
  century *and* after it was published. The trend filter's edge is risk
  reduction (trend 1.0x: -43% worst fall against the market's -84%), and
  that is what makes leverage survivable. Both variants had a higher return
  *and* a shallower worst fall than plain buy-and-hold.
- The limit that failed is a risk preference, not a test of edge. It was
  set at the S&P 500's 2007-09 fall, -55%. Over the century, buy-and-hold
  itself fell -84%.
- Changing that limit now, after seeing the result, would be a new decision
  made with the result known. Only the operator can make it, and it is
  recorded as such if made. It does not change this record: under the rules
  as registered, both variants failed.
- L2's worst single month was -46% (October 1987). Its post-1990 record
  reached the 15% goal only in the strongest stock market period on record;
  a lower-return decade lowers it roughly in proportion.
