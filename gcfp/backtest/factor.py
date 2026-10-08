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
    holdings: int = 30
    buffer_rank: int = 60
    min_scores: int = 2
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
    if len(quarters) == 4 and assets and assets > 0:
        ebit = [
            q.operating_income if q.operating_income is not None else q.ebit
            for q in quarters
        ]
        if all(v is not None for v in ebit):
            out.profitability = sum(ebit) / assets
    return out


def composite_scores(
    inputs: dict[str, FactorInputs], min_scores: int = 2
) -> dict[str, float]:
    """Mean of the value, profitability and momentum percentile ranks."""
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

        scores = composite_scores(inputs, rules.min_scores)
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


def judge(result: BacktestResult, universe_curve, split) -> Verdict:
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
                 f"{VARIANT}):")
    for text, ok in checks:
        lines.append(f"  [{'PASS' if ok else 'FAIL'}] {text}")
    passed = all(ok for _, ok in checks)
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
    "factor_inputs",
    "judge",
    "percentile_ranks",
]
