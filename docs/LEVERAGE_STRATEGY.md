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

---

# Operator decision — 2026-10-09, after the result

The operator chose **L2 (2.0x) for paper trading**, accepting a worst
historical fall deeper than the registered -55% limit (-74% in 1929-35;
-38% since 1990; -46% in the single month of October 1987). The decision was
made knowing the result, and is recorded here as such. It does not change
the record above: under the rules as registered, both variants failed
criterion 4.

The operator added one condition: **an email before any margin call.**

## Paper trading and the margin alarm

- `scripts/run_leverage_paper.py` runs inside the screen job, now
  dispatched **every weekday after the US close** as well as the Sunday
  schedule. The GCFP screen itself still runs weekly. It keeps a 2.0x paper
  book in `reports/paper/leverage-2x-book.json`, applies each month's signal
  on the first run after the month closes, and writes the report and
  `reports/paper/leverage-alert.txt`.
- **The alarm** measures the index move since the last re-levering against
  the move that would trigger a margin call. At 2x with a 25% maintenance
  requirement, that is a 33% fall; with S&P 500 futures (about 7%), about
  46%.
  - **WARNING** at half that distance.
  - **URGENT** at three quarters.
  - **MARGIN CALL LEVEL** at all of it.
  - **CHECK FAILED** if the data could not be had.
- Every alert states two remedies per $10,000 of equity at the last
  rebalance: cash to add, or position to sell, to get back to 2x. Selling is
  usually the safer of the two.
- **Email:** a daily routine on the operator's account triggers the job,
  reads the alert file, and emails anything that is not OK. It also emails a
  position change (risk-on or risk-off) at each month's signal. No address
  or account detail is stored in this public repository.
- Stage 2 passes after **3 monthly signals** with no failed check. Stage 3
  then starts with a small share of the money, as registered.

---

# Crash protection: two variants registered 2026-10-09, before any test

## The problem

The monthly signal protects against slow bear markets (2000-02, 2008), not
fast crashes. Those happen before a month-end comes round: October 1929,
October 1987 (-20% in one day) and February-March 2020 (-34% in five weeks).
At 2x, October 1987 cost 46% in a month on monthly data. On daily data the
index stood about 30% below its September close at the October 19-20 low,
close to the 33% that triggers a margin call at 2x with a 25% maintenance
requirement. Monthly data also hides the worst moments inside a month, so
the monthly test understates the true falls.

## Data

Kenneth French's **daily** US market and T-bill returns, from July 1926.
The monthly trend signal is computed from the daily index's month-end
levels, exactly as before.

## Variants (in addition to L2, which is re-tested on daily data as the reference)

- **L2-V, volatility-scaled.** As L2, but while risk-on the leverage is set
  every 5 trading days to **min(2.0, 30% / the last 20 days' annualised
  volatility)**. 30% is 2x a typical 15% market volatility, so calm markets
  keep the full 2x, and leverage falls as markets get rough. Volatility
  usually jumps as a crash begins. Moreira & Muir (2017) found managing
  exposure this way improved risk-adjusted returns.
- **L2-VS, volatility-scaled with a crash stop.** As L2-V, plus a stop: if
  the index closes **10% or more below its level at the last month-end
  re-levering**, everything moves to T-bills until the next month-end
  signal. 10% is under a third of the way to a margin call at 2x.

## Costs (daily)

As before: borrowing at T-bills + 1.0%/yr on the borrowed part, 0.3%/yr
while levered, and 0.2% per switch in or out (the stop included). Each
leverage change also costs **0.05% per 1.0x changed**.

## Pass criteria: all must hold

1-3. As registered for L1/L2: beats buy-and-hold by at least 1%/yr over the
     full period, in each half, and since 2008.
4.   Maximum drawdown, **measured on daily closes**, no deeper than -55%.
5.   **Worst calendar month no worse than -30%.** This is the "die in a
     crash" test.
6.   **No margin call** on daily closes with a 25% maintenance requirement:
     equity never below 25% of exposure.

## Selection

If both pass, the higher full-period CAGR is selected and replaces L2 in
paper trading. If neither passes, L2 stays in paper trading under the
operator's decision, and the daily-data L2 row shows its true crash risk.
These are the 3rd and 4th leverage variants. Counting everything, about 12
strategies have now been tested on this data.

## Crash protection result — 2026-10-09 (run #31)

| Daily data, 1927-2026 | CAGR | Since 1990 | Since 2008 | Worst day | Worst month | Deepest fall | 1987 crash | 1929 crash |
|---|---|---|---|---|---|---|---|---|
| L2 (as chosen) | +12.8% | +14.2% | +15.9% | -40% | -46% | -75% | -49% | -47% |
| L2-V | +12.7% | +12.9% | +13.7% | -31% | -37% | -73% | -39% | -31% |
| L2-VS | +12.5% | +11.6% | +11.7% | -15% | -29% | -73% | -25% | -22% |
| Market | +9.8% | +11.0% | +11.6% | -17% | -29% | -84% | -33% | -44% |

Neither L2-V nor L2-VS passes: both fall -73% in the 1937-42 bear market,
beyond -55%. L2-VS passes the worst-month test that L2 and L2-V fail. **By the
registered rule, L2 stays in paper trading** under the operator's decision.

Reading, stated plainly:

- The protections do what they are for. They roughly halve the damage of
  sudden crashes (1987: -49% to -25%; 1929: -47% to -22%). They cost about
  2.5%/yr since 1990, and they do not stop a long grinding bear market.
- L2 on daily data is riskier than the monthly test showed: -40% on
  1987-10-19 alone, and equity at 26% of exposure on 1929-10-29, one point
  above a margin call at 25% maintenance. A futures account (about 7%
  maintenance) would have had a wide margin there.
- In this data, return and crash safety trade off one for one. No tested
  rule has both 15%/yr and survivable crashes.

## Operator decision — 2026-10-09, after the crash test

The operator keeps **plain L2 (2.0x)** in paper trading, knowing its
daily-data risk: worst day -40%, worst month -46%, deepest fall -75%. Paper
trading continues unchanged. For real money the recommendation is **S&P
500 futures** rather than a margin loan: about 7% maintenance instead of
25%, so the 1929 low (equity 26% of exposure) would not have triggered a
call. The alarm stays at 25%, the conservative setting, so warnings come
earlier than a futures account strictly needs.

## How much leverage? The Kelly calculation — 2026-10-10 (run #33)

The growth-optimal (Kelly) leverage is L* = (expected return - borrowing
cost) / variance. On the trend's risk-on days, 1927-2026: 8.3% / 14%² ≈
**4.1x**, between 4.0x and 4.9x in every sub-period. The daily simulation,
with fat tails, monthly re-levering and real crashes, puts the best CAGR at
**3.0x** (+14.7%/yr; deepest fall -97%; worst day -89%; 19 margin calls).
**3.5x and 4.0x went to zero.** The formula overstates the safe optimum
because real crashes are larger than a normal distribution allows.

**2.0x is half Kelly**, the standard practitioner's choice. It gets about
three quarters of full Kelly's growth with half its volatility, and it is
the highest leverage with no margin call in 100 years (calls start at
2.25x). The chosen L2 stays at 2.0x.

---

# Improvements: registered 2026-10-10, before any test

## 1. Futures implementation (a cost scenario, not a new rule)

The same L2 rule, priced as S&P 500 futures rather than a margin loan:
financing at T-bills + **0.3%/yr** (the typical implied rate in index
futures, against 1.0% on a margin loan), **0.05%/yr** running cost
(commissions), **0.05%** per switch. Dividends count in full: index futures
prices carry them, so no 30% withholding applies. This is how the real
money would be run, so it shows what L2 should earn in practice. Nothing
about the signal changes.

## 2. L2-E, ensemble signal (one new variant)

Instead of one 10-month average, four: **3, 6, 9 and 12 months**, evenly
spaced across the range the trend literature uses (Faber 2007: 10 months;
Moskowitz, Ooi & Pedersen 2012: 12 months; Hurst, Ooi & Pedersen 2017: 1-12
months). At each month-end, leverage = **2.0x x (share of the four
averages the index is above)**: 2.0x, 1.5x, 1.0x, 0.5x or T-bills. Averaging
lookbacks instead of choosing one is the standard way to cut whipsaw and
the luck of one parameter. Nothing is fitted. Same costs as L2, re-levered
monthly.

## Pass criterion for L2-E (registered)

L2-E replaces L2 only if, on daily data with L2's registered costs, it has:

- a **higher CAGR than L2** over the full period **and** in both halves; and
- a **deepest fall no deeper than L2's**.

Otherwise L2 stays. This is the 5th leverage variant. Counting everything,
about 13 strategies have now been tested on this data.

## Improvements result — 2026-10-10 (run #34)

- **Futures:** the same L2 rule at futures costs earns **+1.0-1.1%/yr
  more** in every period: +13.8%/yr over 1927-2026, +15.3% since 1990, +17.0%
  since 2008. The real money should be run this way.
- **L2-E fails:** lower CAGR than L2 in every period (+12.1% vs +12.7% full).
  It softens sudden crashes (worst day -28% vs -40%) but does worse in slow
  ones (2008: -22% vs -9%; 2022: -27% vs -12%). **L2 stays.**

---

# MA, multi-asset trend: registered 2026-10-10, before any test

## Why

Leverage is limited by how bumpy the thing being levered is. Kelly
leverage is excess return / variance. Several markets that trend at
different times, each under its own trend rule, are smoother than one, so
the same risk buys more return. This is the best-documented improvement to
trend following: Faber 2007's multi-asset version, and Hurst, Ooi &
Pedersen 2017 ("A Century of Evidence on Trend-Following Investing").

## Assets (monthly, total return in US dollars)

| Sleeve | Data |
|---|---|
| US stocks | Kenneth French US market (as L2) |
| International developed stocks | Kenneth French Developed ex US market, from July 1990 |
| US 10-year Treasuries | built from the Federal Reserve's 10-year yield (FRED DGS10, month-end): coupon income plus the price change of a 10-year par bond repriced at the new yield |
| Gold | monthly gold price (datahub.io gold-prices), with Yahoo's COMEX gold future (GC=F) if that source is unavailable or ends early |

## Rules

- **Sleeves:** each month-end, every asset with data is an equal sleeve
  (1/N of the portfolio). A sleeve holds its asset while that asset's
  total-return index is above its 10-month average, and T-bills otherwise.
- **Leverage:** the whole portfolio is levered L times, re-levered monthly.
- **Costs (futures, as each sleeve would be run):** financing at T-bills +
  0.3%/yr on the borrowed part; 0.05%/yr running cost; 0.05% of the sleeve's
  levered size per sleeve switch.
- **The comparison:** L2 (US only, 2.0x, 10-month rule) at the same futures
  costs, over the same months. MA is reported at 2.0x and at the leverage
  that gives it the same volatility as L2 over the period. That leverage is
  one number fitted in-sample, and is stated as such.

## Pass criterion: MA replaces L2 only if, at the volatility-matched leverage

1. its CAGR beats L2's over the full common period **and** in both halves;
2. its deepest fall is no deeper than L2's; and
3. its worst month is no worse than L2's.

## Known limits, stated up front

- The common period starts in 1991, once international data has 10 months.
  That is 35 years, including 2000-02, 2008, 2020 and 2022, but not 1929 or
  1987's daily detail. Monthly data understates falls inside a month.
- The bond series is built from yields, not traded prices; it is a close but
  not exact match for a Treasury future.
- Real implementation needs four futures (micro S&P 500, MSCI EAFE, 10-year
  Treasury note, micro gold). That is more to manage than one.
- This is the 6th leverage variant. Counting everything, about 14
  strategies have now been tested.

## MA result — 2026-10-10 (run #38)

Monthly, 1991-05 to 2026-08, futures costs for both.

| | CAGR | 1st half | 2nd half | Since 2008 | Deepest fall | Worst month | Volatility |
|---|---|---|---|---|---|---|---|
| L2, US stocks 2.0x | +17.3% | +16.8% | +17.8% | +16.9% | -36% | -32% | 22% |
| **MA at 3.82x** (L2's volatility) | **+20.6%** | +19.6% | +21.6% | +20.1% | **-25%** | **-23%** | 22% |
| MA at 2.0x | +12.4% | +12.6% | +12.3% | +11.5% | -13% | -12% | 11% |
| US stocks, buy and hold | +11.2% | +7.4% | +15.0% | +11.4% | -50% | -17% | 15% |

Crashes: dot-com 2000-02, L2 -31% and MA(3.82x) -9%; 2008, -9% and -4%;
2020, -16% and -8%; 2022, -12% and -12%. Trend-sleeve correlations: US and
international stocks 0.62; bonds and gold about 0 with everything.

**MA passes all five registered criteria and replaces L2.**

Reading, stated plainly:

- The gain is diversification, as the design intended. Four sleeves that
  trend at different times give the same volatility as L2 with about 3%/yr
  more return and shallower falls.
- 3.82x is one number fitted in-sample: the leverage that matches L2's
  volatility. It is gross exposure across four markets, about 0.95x each
  when all four are in trend. It is not 3.8x of any single market.
- Monthly data understates falls inside a month, for both strategies alike.
  The period starts in 1991, so 1929 and 1987 are not covered.
- Running it needs four futures. The contract sizes (about $110,000 for a
  10-year note future and $120,000 for MSCI EAFE) make it practical from
  roughly $150,000-250,000 of capital, or with leveraged ETFs below that.

## Operator decision — 2026-10-10: MA at 3.82x goes to paper trading

The operator chose MA at **3.82x**, the leverage with L2's volatility. Paper
trading (`scripts/run_multi_asset_paper.py`, inside the daily screen job)
works as follows:

- **Prices:** live closes of ^SP500TR, EFA, IEF and GLD, using
  dividend-adjusted closes and re-reading each rebalance price on today's
  scale, so payouts count.
- **Signals:** each sleeve gets its own 10-month signal at every month-end.
- **Book:** the account is valued as futures.
- **Alarm:** `reports/paper/leverage-alert.txt` now belongs to MA. It grades
  the distance to a futures margin call (8% of gross exposure assumed) and
  states the cash to add or positions to cut. The daily email check reads it.
- **L2:** its book keeps running for comparison, with its alert in
  `leverage-2x-alert.txt`.

Stage 2 restarts with MA: 3 monthly signals with no failed check.

---

# MA validation and MA-RP: registered 2026-10-10, before any test

## A. Out-of-sample check: MA-3 back to 1973 (validation, not a new rule)

MA's test covered 1991-2026, because the international data starts in
1990. Three of its four sleeves have older data: US stocks (French, 1926),
10-year Treasuries (from ^TNX yields, 1962) and gold (free-floating since
1971; datahub monthly). **MA-3** is MA's rule on those three, run from
1973-01 to 2026-08. Like MA, it is levered to match L2's volatility over the
period.

The years **1973-1990 were never seen by MA's design.** They hold the 1973-74
crash, 1970s inflation, the 1979-81 bond collapse and 1987.

**Passes** if, at the volatility-matched leverage, MA-3 over 1973-1990:

1. has a higher CAGR than L2 over the same months; and
2. has a deepest fall no deeper than L2's.

**If it fails,** MA stays in paper trading, but the evidence for it is
recorded as weaker: the 1991-2026 result may owe something to its period.

## B. MA-RP, risk-balanced sleeves (one new variant)

As MA, but each sleeve's share is proportional to **1 / its volatility over
the previous 12 months** (monthly returns) instead of 1/4. Out-of-trend
sleeves keep their share in T-bills. The whole is levered to L2's
volatility. This sizes each market to similar risk, as Hurst, Ooi & Pedersen
(2017) do, so the two correlated stock sleeves stop dominating.

**Replaces MA** only if, over MA's period (1991-2026) and at the same
volatility, it has:

1. a higher CAGR than MA over the full period and in both halves;
2. a deepest fall no deeper than MA's; and
3. a gross leverage when all sleeves are in trend no more than **6x**. Past
   that the bond futures position becomes impractical, and the result rests
   heavily on Treasuries.

This is the 7th leverage variant. Counting everything, about 15 strategies
have now been tested.

## Validation and MA-RP results — 2026-10-10 (run #40)

**A. Out of sample: confirmed.** MA-3 (US stocks, Treasuries, gold) at
L2's volatility (3.46x), 1973-1990, a period MA's design never saw:
**+22.6%/yr against L2's +8.3%**, deepest fall -42% against -51%. In the
1973-74 crash it gained 98% while L2 lost 29%; in 1987 it was flat while L2
lost 28%. Over 1973-2026: +21.2%/yr against +14.5%.

The caveat: much of 1973-1990's gain is the 1970s gold boom (gold went from
about $65 to $850 in nine years after the dollar left gold), a one-off of
monetary history. Even so, the result is the same direction and size as
1991-2026, which had no such boom.

**B. MA-RP: fails by one criterion.** Risk-balanced sleeves earned more in
every period (+22.2%/yr against +20.6% full period, at 4.37x), but its
deepest fall was -27% against MA's -25%. **MA stays.** The registered rule
is applied as written, even though this miss is narrow.

---

# MA daily crash check: registered 2026-10-10, before running

A diagnostic of MA as chosen (3.82x, equal sleeves). It changes no rule.
MA's tests used monthly data, which hides falls inside a month. This one
replays it **day by day**:

- **Data:** French daily US and Developed ex US markets; daily 10-year
  Treasury returns from ^TNX yields; daily gold from Yahoo's GC=F. The
  period starts once every sleeve has 10 month-ends of daily data, around
  2001.
- **Model:** the monthly signals and re-levering are unchanged. Between
  month-ends each futures position moves with its market every day, and
  margin is checked on every close at the 8% futures maintenance in
  scripts/leverage_config.json.
- **Reported:** worst day, worst month, deepest fall, the lowest equity as a
  share of exposure, and each crash window (2000-02, 2008, 2011, 2015,
  2018, 2020, 2022), all next to L2 (US stocks 2x) on the same days.

**What would change the decision:**

1. any margin call at 8% maintenance; or
2. a deepest fall on daily closes deeper than L2's over the same days.

Either one is reported to the operator before any real money is used.
