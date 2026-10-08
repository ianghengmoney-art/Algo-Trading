"""The factor strategy pre-registered in ``docs/FACTOR_STRATEGY.md``.

Every month, every eligible company is ranked on value, profitability and
momentum; the 30 best are held at equal weight, and a holding is kept while
it stays in the top 60. It reuses the GCFP backtest's machinery — the
point-in-time universe, prices on the as-of basis, dividends, the book —
and replaces only the monthly decision. The numbers below are the
pre-registered ones; changing them makes a new variant, which the document
says must be registered and counted.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Sequence

from ..classification import Classification
from ..data.adapter import DataUnavailable
from ..modules import a_health
from ..types import CompanyData
from .engine import Backtester, BacktestResult, RebalanceRecord
from .portfolio import BacktestBook
from .metrics import annualised_return, window_slice

#: Variant number, as counted in docs/FACTOR_STRATEGY.md.
VARIANT = 1


@dataclass(frozen=True)
class FactorRules:
    #: Which registered variant (docs/FACTOR_STRATEGY.md): 1, 2 or 3.
    variant: int = 1
    holdings: int = 30
    buffer_rank: int = 60
    min_scores: int = 2
    #: Variant 3 keeps only companies at least this large on the date.
    min_market_cap: float | None = None
    #: Momentum window: from 12 months to 1 month before the date.
    momentum_from_days: int = 365
    momentum_to_days: int = 30


def percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    """0 for the lowest, 1 for the highest; ties share the average rank."""
    if not values:
        return {}
    if len(values) == 1:
        return {k: 0.5 for k in values}
    ordered = sorted(values.items(), key=lambda kv: kv[1])
    out: dict[str, float] = {}
    i = 0
    n = len(ordered)
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        rank = (i + j) / 2 / (n - 1)
        for k in range(i, j + 1):
            out[ordered[k][0]] = rank
        i = j + 1
    return out


@dataclass
class FactorInputs:
    earnings_yield: float | None = None
    fcf_yield: float | None = None
    profitability: float | None = None
    momentum: float | None = None
    # Variants 2 and 3.
    ebit_ev: float | None = None
    fcf_ev: float | None = None
    gross_profitability: float | None = None
    accruals: float | None = None
    share_growth: float | None = None
    earnings_growth: float | None = None


def rules_for(variant: int) -> "FactorRules":
    """The registered rules for a variant number."""
    if variant == 1:
        return FactorRules(variant=1, min_scores=2)
    if variant == 2:
        return FactorRules(variant=2, min_scores=3)
    if variant == 3:
        return FactorRules(variant=3, min_scores=3, min_market_cap=2_000_000_000.0)
    raise ValueError(f"variant {variant} is not registered in docs/FACTOR_STRATEGY.md")


def factor_inputs(
    data: CompanyData, market_cap: float | None, momentum: float | None
) -> FactorInputs:
    """The raw numbers behind the three scores, each ``None`` when missing."""
    out = FactorInputs(momentum=momentum)
    if market_cap and market_cap > 0:
        net_income = a_health._ttm(data, "net_income")
        if net_income is not None:
            out.earnings_yield = net_income / market_cap
        ocf = a_health._ttm(data, "operating_cash_flow")
        capex = a_health._ttm(data, "capital_expenditure")
        if ocf is not None and capex is not None:
            out.fcf_yield = (ocf + capex) / market_cap
    quarters = data.trailing_quarters(4)
    latest = data.latest_quarter
    assets = latest.total_assets if latest is not None else None
    ttm_ebit = None
    if len(quarters) == 4:
        ebit = [
            q.operating_income if q.operating_income is not None else q.ebit
            for q in quarters
        ]
        if all(v is not None for v in ebit):
            ttm_ebit = sum(ebit)
    if ttm_ebit is not None and assets and assets > 0:
        out.profitability = ttm_ebit / assets

    # Variants 2 and 3.
    net_debt = latest.net_debt if latest is not None else None
    if market_cap and market_cap > 0 and net_debt is not None:
        ev = market_cap + net_debt
        # An enterprise value at or below zero makes the ratio meaningless
        # (it would rank the cheapest companies as the dearest).
        if ev > 0:
            if ttm_ebit is not None:
                out.ebit_ev = ttm_ebit / ev
            if out.fcf_yield is not None:
                out.fcf_ev = out.fcf_yield * market_cap / ev
    if assets and assets > 0:
        gross = a_health._ttm(data, "gross_profit")
        if gross is not None:
            out.gross_profitability = gross / assets
        net_income = a_health._ttm(data, "net_income")
        ocf = a_health._ttm(data, "operating_cash_flow")
        if net_income is not None and ocf is not None:
            out.accruals = (net_income - ocf) / assets
        eight = data.trailing_quarters(8)
        if len(eight) == 8 and all(q.net_income is not None for q in eight):
            recent = sum(q.net_income for q in eight[:4])
            prior = sum(q.net_income for q in eight[4:])
            out.earnings_growth = (recent - prior) / assets
    out.share_growth = a_health.share_count_cagr(data, years=1)
    return out


def _ranks(inputs: dict[str, FactorInputs], attr: str, sign: float = 1.0) -> dict[str, float]:
    return percentile_ranks({
        s: sign * getattr(i, attr) for s, i in inputs.items()
        if getattr(i, attr) is not None
    })


def theme_scores(inputs: dict[str, FactorInputs], min_themes: int = 3) -> dict[str, float]:
    """Variants 2 and 3: four themes, each the mean of its available
    component ranks; the composite is the mean of the themes."""
    themes = [
        [_ranks(inputs, "ebit_ev"), _ranks(inputs, "fcf_ev")],               # value
        [_ranks(inputs, "gross_profitability"), _ranks(inputs, "accruals", -1.0)],  # quality
        [_ranks(inputs, "share_growth", -1.0)],                              # shareholder yield
        [_ranks(inputs, "momentum"), _ranks(inputs, "earnings_growth")],     # momentum
    ]
    out: dict[str, float] = {}
    for symbol in inputs:
        scores = []
        for components in themes:
            parts = [r[symbol] for r in components if symbol in r]
            if parts:
                scores.append(statistics.fmean(parts))
        if len(scores) >= min_themes:
            out[symbol] = statistics.fmean(scores)
    return out


def composite_scores(
    inputs: dict[str, FactorInputs], min_scores: int = 2
) -> dict[str, float]:
    """Variant 1: mean of the value, profitability and momentum ranks."""
    ey = percentile_ranks(
        {s: i.earnings_yield for s, i in inputs.items() if i.earnings_yield is not None}
    )
    fy = percentile_ranks(
        {s: i.fcf_yield for s, i in inputs.items() if i.fcf_yield is not None}
    )
    prof = percentile_ranks(
        {s: i.profitability for s, i in inputs.items() if i.profitability is not None}
    )
    mom = percentile_ranks(
        {s: i.momentum for s, i in inputs.items() if i.momentum is not None}
    )
    out: dict[str, float] = {}
    for symbol in inputs:
        value_parts = [r[symbol] for r in (ey, fy) if symbol in r]
        scores = []
        if value_parts:
            scores.append(statistics.fmean(value_parts))
        if symbol in prof:
            scores.append(prof[symbol])
        if symbol in mom:
            scores.append(mom[symbol])
        if len(scores) >= min_scores:
            out[symbol] = statistics.fmean(scores)
    return out


class FactorBacktester(Backtester):
    """GCFP's backtest loop with the factor strategy's monthly decision."""

    def __init__(self, *args, rules: FactorRules = FactorRules(), **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.rules = rules
        #: The equal-weight eligible universe, as an index level per month.
        self.universe_curve: list[tuple[date, float]] = []
        self._universe_level = 1.0
        self._universe_last: dict[str, float] = {}
        self._universe_last_date: date | None = None
        self.eligible_counts: list[int] = []

    def _momentum(self, symbol: str, as_of: date) -> float | None:
        end = self._price_on(symbol, as_of - timedelta(days=self.rules.momentum_to_days))
        start = self._price_on(symbol, as_of - timedelta(days=self.rules.momentum_from_days))
        if not end or not start or start <= 0:
            return None
        return end / start - 1.0

    def _advance_universe_index(self, as_of: date, prices: dict[str, float]) -> None:
        """Equal-weight return of last month's eligible names, dividends
        included and costs excluded (which flatters this benchmark). A name
        with no price this month is carried at its last price."""
        if self._universe_last and self._universe_last_date is not None:
            returns = []
            for symbol, then in self._universe_last.items():
                now = prices.get(symbol)
                if now is None:
                    now = self._price_on(symbol, as_of) or then
                paid = 0.0
                if hasattr(type(self.adapter), "get_dividends"):
                    try:
                        paid = sum(
                            d.amount for d in self.adapter.get_dividends(
                                symbol, self._universe_last_date + timedelta(days=1), as_of
                            )
                        )
                    except Exception:
                        paid = 0.0
                returns.append((now + paid) / then - 1.0)
            if returns:
                self._universe_level *= 1.0 + statistics.fmean(returns)
        self.universe_curve.append((as_of, self._universe_level))

    def _rebalance(
        self, book: BacktestBook, as_of: date, result: BacktestResult
    ) -> RebalanceRecord:
        settings, rules = self.settings, self.rules
        adapter = self._pin(as_of)
        record = RebalanceRecord(as_of=as_of, evaluated=0, passers=0)
        if settings.ballast_in_index:
            self._grow_ballast(book, as_of)
        self.credit_dividends(book, as_of)

        universe = self.universe_builder(adapter, self.config, as_of)
        eligible = [m for m in universe.included if not (m.is_bank or m.is_insurer)]
        if rules.min_market_cap is not None:
            eligible = [
                m for m in eligible
                if m.market_cap is not None and m.market_cap >= rules.min_market_cap
            ]
        inputs: dict[str, FactorInputs] = {}
        tags: dict[str, Classification] = {}
        for member in eligible:
            try:
                data = adapter.load_company(
                    member.symbol, price_start=as_of - timedelta(days=400), price_end=as_of
                )
            except DataUnavailable:
                continue
            except Exception as exc:
                self._crashed("loading", exc)
                continue
            try:
                inputs[member.symbol] = factor_inputs(
                    data, member.market_cap, self._momentum(member.symbol, as_of)
                )
                tag, _, _ = a_health.classify(data, self.config)
            except Exception as exc:
                self._crashed("scoring", exc)
                continue
            tags[member.symbol] = tag or Classification.CORE_STABLE

        scores = (
            composite_scores(inputs, rules.min_scores) if rules.variant == 1
            else theme_scores(inputs, rules.min_scores)
        )
        ranked = sorted(scores, key=lambda s: -scores[s])
        rank_of = {s: i for i, s in enumerate(ranked)}
        record.evaluated = len(ranked)
        self.eligible_counts.append(len(ranked))

        prices = self._prices_on(set(ranked) | set(book.positions), as_of)
        book.mark(prices)
        for symbol in list(book.positions):
            position = book.positions[symbol]
            if position.missed_marks > settings.delisting_grace_rebalances:
                self._close_as_delisted(book, symbol, as_of, result)
                record.sells.append((symbol, "assumed delisted"))

        # Sell what left the buffer or the universe.
        for symbol in list(book.positions):
            rank = rank_of.get(symbol)
            if rank is not None and rank < rules.buffer_rank:
                continue
            price = prices.get(symbol)
            if price is None:
                continue  # unpriceable: the delisting rule handles it
            reason = (
                "left the eligible universe" if rank is None
                else f"fell to rank {rank + 1}, outside the top {rules.buffer_rank}"
            )
            book.sell(symbol, price * (1 - settings.transaction_cost), as_of, reason)
            record.sells.append((symbol, reason))

        # Fill the empty slots from the top of the ranking.
        total_value = book.total_value(prices)
        for symbol in ranked:
            if len(book.positions) >= rules.holdings:
                break
            if symbol in book.positions:
                continue
            price = prices.get(symbol)
            if price is None:
                continue
            amount = min(total_value / rules.holdings, book.cash)
            if amount <= 0:
                break
            fill = book.buy(
                symbol, tags.get(symbol, Classification.CORE_STABLE), amount,
                price * (1 + settings.transaction_cost), as_of,
                conviction=scores[symbol] * 100.0,
                intended_weight=1.0 / rules.holdings,
                anchor_mode="FACTOR",
                thesis_invalidation=f"falls outside the top {rules.buffer_rank}",
                reason=f"rank {rank_of[symbol] + 1} of {len(ranked)}",
            )
            if fill is not None:
                record.buys.append(symbol)
        record.passers = len(record.buys)

        self._advance_universe_index(as_of, prices)
        self._universe_last = {s: prices[s] for s in ranked if s in prices}
        self._universe_last_date = as_of

        prices = self._prices_on(list(book.positions), as_of)
        book.mark(prices)
        benchmark = self._price_on(settings.benchmark_symbol, as_of)
        book.snapshot(as_of, prices, benchmark)
        if benchmark is not None:
            result.benchmark_curve.append((as_of, benchmark))
        return record


def _cagr(curve: Sequence[tuple[date, float]], start: date, end: date) -> float | None:
    return annualised_return(window_slice(curve, start, end))


@dataclass
class Verdict:
    lines: list[str] = field(default_factory=list)
    passed: bool = False


def _monthly_returns(curve: Sequence[tuple[date, float]]) -> dict[date, float]:
    out: dict[date, float] = {}
    for (_, before), (day, after) in zip(curve, curve[1:]):
        if before and before > 0:
            out[day] = after / before - 1.0
    return out


def edge_statistics(curve, benchmark, universe_curve=()) -> list[str]:
    """How much of the excess return could be luck.

    Informational, not a pass criterion: the criteria were registered
    without it. A t-statistic below about 2 is what luck alone often
    produces; with three variants tried, about 2.4 is the bar for strong
    evidence (a Bonferroni allowance for the extra chances).
    """
    strat, bench = _monthly_returns(curve), _monthly_returns(benchmark)
    days = sorted(set(strat) & set(bench))
    lines: list[str] = []
    if len(days) >= 12:
        excess = [strat[d] - bench[d] for d in days]
        mean = statistics.fmean(excess)
        sd = statistics.stdev(excess)
        t = mean / (sd / len(excess) ** 0.5) if sd > 0 else float("nan")
        te = sd * 12 ** 0.5
        ahead = sum(1 for e in excess if e > 0) / len(excess)
        lines += [
            "HOW LIKELY IS THE EDGE REAL? (monthly returns against the S&P 500)",
            f"  months: {len(excess)} · ahead in {ahead:.0%} of them",
            f"  average excess: {mean * 12:+.2%}/yr · tracking error {te:.1%}/yr · "
            f"information ratio {mean * 12 / te:+.2f}" if te > 0 else
            f"  average excess: {mean * 12:+.2%}/yr",
            f"  t-statistic: {t:+.2f}  (below ~2: consistent with luck; "
            "~2.4+: strong evidence allowing for 3 variants tried)",
        ]
    years = sorted({d.year for d, _ in curve})
    if len(years) >= 2:
        def by_year(points):
            ends: dict[int, float] = {}
            for d, v in points:
                ends[d.year] = v
            return ends

        s_end, b_end, u_end = by_year(curve), by_year(benchmark), by_year(universe_curve)

        def pct(ends, year):
            if year in ends and year - 1 in ends and ends[year - 1]:
                return f"{ends[year] / ends[year - 1] - 1:+.1%}"
            return "n/a"

        lines += ["", "YEAR BY YEAR (calendar years, from each December's value)",
                  f"  {'year':6s} {'strategy':>9s} {'S&P 500':>9s} {'eq-wt univ':>11s}"]
        for year in years[1:]:
            lines.append(f"  {year:<6d} {pct(s_end, year):>9s} {pct(b_end, year):>9s} "
                         f"{pct(u_end, year):>11s}")
    return lines


def write_run_data(path_prefix, result: BacktestResult, universe_curve, ticker_of) -> list[str]:
    """Curves and every fill as CSV, so a run can be re-analysed without
    being rerun. Returns the paths written."""
    import csv
    from pathlib import Path

    prefix = Path(path_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    bench = dict(result.benchmark_curve)
    univ = dict(universe_curve)
    curves = prefix.with_name(prefix.name + "-curves.csv")
    with curves.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "strategy_value", "sp500_level", "equal_weight_universe", "holdings"])
        for snap in result.book.snapshots:
            w.writerow([snap.as_of.isoformat(), f"{snap.total_value:.2f}",
                        bench.get(snap.as_of, ""), univ.get(snap.as_of, ""),
                        snap.position_count])
    trades = prefix.with_name(prefix.name + "-trades.csv")
    with trades.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "symbol", "ticker", "side", "shares", "price", "value", "reason"])
        for f in result.book.fills:
            w.writerow([f.trade_date.isoformat(), f.symbol, ticker_of(f.symbol), f.side,
                        f"{f.shares:.4f}", f"{f.price:.4f}", f"{f.value:.2f}", f.reason])
    return [str(curves), str(trades)]


#: The operator's goals, reported against but never used to lower the bar
#: or to choose a variant. Revised 2026-10-08 from "5%/yr above the S&P
#: 500" to an absolute 15-20%/yr; the excess is still reported.
GOAL_EXCESS = 0.05
GOAL_CAGR = (0.15, 0.20)


def judge(result: BacktestResult, universe_curve, split, variant: int = VARIANT) -> Verdict:
    """The pre-registered pass criteria, applied once."""
    curve, bench = result.equity_curve, result.benchmark_curve
    full = (curve[0][0], curve[-1][0]) if curve else (None, None)
    rows = [
        ("full period", full[0], full[1]),
        ("earlier half", split.train_start, split.train_end),
        ("later half", split.test_start, split.test_end),
    ]
    lines = [f"{'':14s} {'strategy':>10s} {'S&P 500 TR':>11s} {'difference':>11s} {'eq-wt universe':>15s}"]
    cagr: dict[str, tuple] = {}
    for name, start, end in rows:
        if start is None:
            continue
        s, b, u = _cagr(curve, start, end), _cagr(bench, start, end), _cagr(universe_curve, start, end)
        cagr[name] = (s, b, u)

        def pct(x):
            return f"{x:+.2%}" if x is not None else "n/a"

        diff = (s - b) if s is not None and b is not None else None
        lines.append(f"{name:14s} {pct(s):>10s} {pct(b):>11s} {pct(diff):>11s} {pct(u):>15s}")

    def beats(name, margin=0.0, against=1):
        s, *others = cagr.get(name, (None, None, None))
        other = others[against - 1]
        return s is not None and other is not None and s - other > margin

    checks = [
        ("beats the S&P 500 in the earlier half", beats("earlier half")),
        ("beats the S&P 500 in the later half", beats("later half")),
        ("beats the S&P 500 by at least 1%/yr over the full period",
         beats("full period", 0.01)),
        ("beats the equal-weight eligible universe over the full period",
         beats("full period", 0.0, against=2)),
    ]
    lines.append("")
    lines.append("PRE-REGISTERED PASS CRITERIA (docs/FACTOR_STRATEGY.md, variant "
                 f"{variant} of 3 registered):")
    for text, ok in checks:
        lines.append(f"  [{'PASS' if ok else 'FAIL'}] {text}")
    passed = all(ok for _, ok in checks)
    goal = (
        beats("earlier half", GOAL_EXCESS) and beats("later half", GOAL_EXCESS)
        and beats("full period", GOAL_EXCESS)
    )
    full = cagr.get("full period", (None, None, None))
    low, high = GOAL_CAGR
    strategy_cagr, market_cagr = full[0], full[1]
    if strategy_cagr is None:
        cagr_goal = "n/a"
    elif strategy_cagr >= low:
        cagr_goal = f"MET ({strategy_cagr:+.2%}/yr)"
    else:
        cagr_goal = f"not met ({strategy_cagr:+.2%}/yr)"
    lines.append(f"  GOAL {low:.0%}-{high:.0%}/yr compound return: {cagr_goal}")
    if strategy_cagr is not None and market_cagr is not None:
        lines.append(
            f"    of which the S&P 500 itself returned {market_cagr:+.2%}/yr and the "
            f"strategy added {strategy_cagr - market_cagr:+.2%}/yr. A future decade "
            "with lower market returns lowers the first part, not the second."
        )
    lines.append(
        f"  ({GOAL_EXCESS:.0%}/yr above the S&P 500 in both halves and overall: "
        + ("met)" if goal else "not met)")
    )
    lines.append(
        "  VERDICT: " + (
            "PASSED — proceed to paper trading before any real money."
            if passed else
            "FAILED — no demonstrated edge on this data. The rules may not be "
            "adjusted and re-tested as the same strategy; the honest default "
            "is an index fund."
        )
    )
    return Verdict(lines, passed)


__all__ = [
    "FactorBacktester",
    "FactorInputs",
    "FactorRules",
    "VARIANT",
    "composite_scores",
    "edge_statistics",
    "write_run_data",
    "factor_inputs",
    "judge",
    "percentile_ranks",
    "rules_for",
    "theme_scores",
]
