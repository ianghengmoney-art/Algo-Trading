"""The simulation loop.

At each rebalance date the engine does what the operator would do that week:
re-check what is held, screen for new passers, size what qualifies, and record
the result.  The only thing that makes it a backtest rather than a live run is
``as_of`` — every adapter call is pinned to the rebalance date so no evaluation
can see a filing that had not happened yet.

§13.3 requires a walk-forward split with no tuning on the holdout.  That is
enforced by :class:`WalkForwardSplit` handing out two disjoint periods and by
the sweep refusing to run on the holdout; it is not left to the operator to
remember.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Callable, Iterable, Sequence

from ..classification import Classification, Regime, regime_of
from ..config import Config
from ..data.adapter import DataAdapter, DataUnavailable
from ..modules import a_health, c_anchors, f_sizing
from ..modules.e_triggers import SignalType
from ..modules.f_sizing import Bucket, PortfolioState
from ..modules.h_monitor import MonitorFlag
from ..modules.k_currency import FxTable
from ..pipeline import CandidateInputs, Evaluation, evaluate_candidate
from ..types import CompanyData, MarketData
from ..universe import Universe, build_universe, find_peers
from .portfolio import BacktestBook, Snapshot

#: The mandatory windows §13.2 names, where growth names, financials, and
#: multiple-expansion respectively got hit hardest.
MANDATORY_WINDOWS: tuple[tuple[str, date, date], ...] = (
    ("dot-com unwind", date(2000, 1, 1), date(2002, 12, 31)),
    ("global financial crisis", date(2008, 1, 1), date(2009, 12, 31)),
    ("2022 multiple compression", date(2022, 1, 1), date(2022, 12, 31)),
)


@dataclass(frozen=True)
class WalkForwardSplit:
    """§13.3.  Two disjoint periods; the holdout is never tuned on."""

    train_start: date
    train_end: date
    test_start: date
    test_end: date

    def __post_init__(self) -> None:
        if self.train_end >= self.test_start:
            raise ValueError(
                "train and test periods overlap; a walk-forward split that "
                "leaks is worse than no split, because it looks rigorous"
            )

    @classmethod
    def by_fraction(
        cls, start: date, end: date, train_fraction: float = 0.6
    ) -> "WalkForwardSplit":
        span = (end - start).days
        boundary = start + timedelta(days=int(span * train_fraction))
        return cls(start, boundary, boundary + timedelta(days=1), end)

    def contains_train(self, day: date) -> bool:
        return self.train_start <= day <= self.train_end

    def contains_test(self, day: date) -> bool:
        return self.test_start <= day <= self.test_end

    def as_report_line(self) -> str:
        return (
            f"train {self.train_start.isoformat()}..{self.train_end.isoformat()} · "
            f"holdout {self.test_start.isoformat()}..{self.test_end.isoformat()}"
        )


@dataclass(frozen=True)
class BacktestSettings:
    start: date
    end: date
    initial_capital: float = 100_000.0
    #: Months between rebalances.  The spec's cadence is per-earnings for
    #: holdings and weekly for the screen; monthly is the coarsest step that
    #: still catches every quarterly report, and keeps a 15-year run tractable.
    rebalance_months: int = 1
    benchmark_symbol: str = "^GSPC"
    #: Ballast is modelled as cash, so this is the floor the screen may not
    #: spend below (F2's 45-55% band, taken at its lower bound).
    min_cash_weight: float = 0.45
    #: Applied to every simulated fill, as a stand-in for spread and slippage.
    transaction_cost: float = 0.001
    #: What a holding is assumed to return when its price stops for good.
    #: The free price feeds drop delisted tickers, so the real exit price is
    #: usually unknowable.  Studies of performance-related delistings put the
    #: typical loss at roughly a third; takeovers, the other common cause,
    #: usually exit at a premium.  -30% is a stated middle, not a measurement,
    #: and every position it touches is labelled so the result can be judged.
    delisting_return: float = -0.30
    #: Rebalances a holding may go unpriced before it is treated as delisted.
    #: One allows for a feed hiccup without carrying a dead name for months.
    delisting_grace_rebalances: int = 1


@dataclass
class RebalanceRecord:
    """What happened on one date — the row the reports are built from."""

    as_of: date
    evaluated: int
    passers: int
    buys: list[str] = field(default_factory=list)
    sells: list[tuple[str, str]] = field(default_factory=list)
    single_anchor_candidates: int = 0
    dual_anchor_candidates: int = 0
    divergent_candidates: int = 0
    reclassifications: list[tuple[str, str, str]] = field(default_factory=list)
    classifications: dict[str, str] = field(default_factory=dict)
    stopped_at: dict[str, int] = field(default_factory=dict)


@dataclass
class BacktestResult:
    """Everything one run produced."""

    label: str
    settings: BacktestSettings
    split: WalkForwardSplit | None
    book: BacktestBook
    rebalances: list[RebalanceRecord] = field(default_factory=list)
    benchmark_curve: list[tuple[date, float]] = field(default_factory=list)
    config_fingerprint: str = ""
    notes: list[str] = field(default_factory=list)
    #: Holdings closed because their price stopped, at the assumed return.
    assumed_delistings: int = 0

    @property
    def equity_curve(self) -> list[tuple[date, float]]:
        return self.book.equity_curve

    @property
    def single_anchor_rate(self) -> float | None:
        """§13.9.  Above 40% is a Module I break criterion."""
        single = sum(r.single_anchor_candidates for r in self.rebalances)
        dual = sum(r.dual_anchor_candidates for r in self.rebalances)
        total = single + dual
        return single / total if total else None

    @property
    def divergence_rate(self) -> float | None:
        """C4 firing on more than half of candidates means triangulation is
        not producing signal (Module I)."""
        divergent = sum(r.divergent_candidates for r in self.rebalances)
        dual = sum(r.dual_anchor_candidates for r in self.rebalances)
        return divergent / dual if dual else None

    @property
    def reclassification_count(self) -> int:
        """§13.10."""
        return sum(len(r.reclassifications) for r in self.rebalances)


#: Prefix on the exit reason of a holding closed because its price stopped.
DELISTED_REASON = "price stopped — treated as delisted"


def month_ends(start: date, end: date, step_months: int = 1) -> list[date]:
    """Rebalance dates, on the last calendar day of each step."""
    out: list[date] = []
    year, month = start.year, start.month
    while True:
        if month == 12:
            nxt = date(year + 1, 1, 1)
        else:
            nxt = date(year, month + 1, 1)
        day = nxt - timedelta(days=1)
        if day > end:
            break
        if day >= start:
            out.append(day)
        month += step_months
        while month > 12:
            month -= 12
            year += 1
    return out


class Backtester:
    """Runs one configuration over one period."""

    def __init__(
        self,
        adapter: DataAdapter,
        config: Config,
        settings: BacktestSettings,
        symbols: Sequence[str],
        *,
        label: str = "GCFP v4",
        split: WalkForwardSplit | None = None,
        universe_builder: Callable[[DataAdapter, Config, date], Universe] | None = None,
        signal_filter: Callable[[Evaluation], bool] | None = None,
        sizer: Callable[[Evaluation, PortfolioState, Config], float] | None = None,
        conviction_scorer: Callable[..., object] | None = None,
    ) -> None:
        self.adapter = adapter
        self.config = config
        self.settings = settings
        self.symbols = list(symbols)
        self.label = label
        self.split = split
        self.universe_builder = universe_builder or self._default_universe
        #: Variants override these two hooks; the loop itself never branches on
        #: which variant is running, so a benchmark cannot accidentally differ
        #: from the base run in more ways than it claims to.
        self.signal_filter = signal_filter or (lambda e: True)
        self.sizer = sizer
        self.conviction_scorer = conviction_scorer

    # -- point-in-time ----------------------------------------------------
    def _pin(self, as_of: date) -> DataAdapter:
        """Return an adapter that can see nothing filed after ``as_of``.

        §13.8 is the reason this exists and the reason it is done by
        construction rather than by convention: a lookahead bug does not raise,
        it just improves the result.
        """
        for attribute in ("as_of",):
            if hasattr(self.adapter, attribute):
                try:
                    object.__setattr__(self.adapter, attribute, as_of)
                except Exception:
                    setattr(self.adapter, attribute, as_of)
        inner = getattr(self.adapter, "fundamentals", None)
        if inner is not None and hasattr(inner, "as_of"):
            setattr(inner, "as_of", as_of)
        return self.adapter

    def _default_universe(
        self, adapter: DataAdapter, config: Config, as_of: date
    ) -> Universe:
        return build_universe(adapter, self.symbols, config, as_of=as_of)

    # -- prices -----------------------------------------------------------
    def _price_on(self, symbol: str, as_of: date) -> float | None:
        """The last close at or before ``as_of``, within a fortnight."""
        try:
            prices = self.adapter.get_prices(
                symbol, as_of - timedelta(days=14), as_of
            )
        except Exception:
            return None
        usable = [p for p in prices if p.price_date <= as_of]
        if not usable:
            return None
        return max(usable, key=lambda p: p.price_date).close

    def _prices_on(self, symbols: Iterable[str], as_of: date) -> dict[str, float]:
        out: dict[str, float] = {}
        for symbol in symbols:
            price = self._price_on(symbol, as_of)
            if price is not None:
                out[symbol] = price
        return out

    # -- the loop ---------------------------------------------------------
    def run(self, progress: bool = False) -> BacktestResult:
        settings = self.settings
        book = BacktestBook(cash=settings.initial_capital)
        result = BacktestResult(
            label=self.label,
            settings=settings,
            split=self.split,
            book=book,
            config_fingerprint=self.config.fingerprint,
        )

        dates = month_ends(settings.start, settings.end, settings.rebalance_months)
        if not dates:
            result.notes.append("no rebalance dates in the requested period")
            return result

        for index, as_of in enumerate(dates):
            if progress and index % 12 == 0:
                print(f"  {as_of.isoformat()} ...", flush=True)
            record = self._rebalance(book, as_of, result)
            result.rebalances.append(record)

        # Close everything at the end so every position becomes a closed one
        # and the return distribution covers the whole run.
        final = dates[-1]
        prices = self._prices_on(list(book.positions), final)
        for symbol in list(book.positions):
            price = prices.get(symbol)
            if price is not None:
                book.sell(symbol, price * (1 - settings.transaction_cost), final,
                          "end of backtest period")
            else:
                # Previously left open, which removed it from every return
                # statistic — a company that went bust never counted as a loss.
                self._close_as_delisted(book, symbol, final, result)

        return result

    def _close_as_delisted(
        self, book: BacktestBook, symbol: str, as_of: date, result: BacktestResult
    ) -> None:
        """Close a holding whose price has stopped, at an assumed return.

        The price feeds drop delisted tickers, so the true exit is unknown.
        Leaving the position open silently removed it from every return
        statistic; closing it at its last price would assume nothing was lost.
        The assumed delisting return sits between, and is labelled on the
        closed position so the report can count and judge it.
        """
        position = book.positions.get(symbol)
        if position is None:
            return
        rate = self.settings.delisting_return
        exit_price = max(position.last_price * (1.0 + rate), 0.0)
        book.sell(
            symbol,
            exit_price,
            as_of,
            f"{DELISTED_REASON} at an assumed {rate:+.0%} from last close "
            f"{position.last_price:,.2f}",
            allow_zero=True,
        )
        result.assumed_delistings += 1

    def _rebalance(
        self, book: BacktestBook, as_of: date, result: BacktestResult
    ) -> RebalanceRecord:
        settings = self.settings
        adapter = self._pin(as_of)
        record = RebalanceRecord(as_of=as_of, evaluated=0, passers=0)

        universe = self.universe_builder(adapter, self.config, as_of)
        candidates = [m.symbol for m in universe.included]

        held = list(book.positions)
        prices = self._prices_on(set(candidates) | set(held), as_of)
        book.mark(prices)
        for symbol in held:
            position = book.positions.get(symbol)
            if (
                position is not None
                and position.missed_marks > settings.delisting_grace_rebalances
            ):
                self._close_as_delisted(book, symbol, as_of, result)
                record.sells.append((symbol, "assumed delisted"))
        held = list(book.positions)

        total_value = book.total_value(prices)
        portfolio_state = self._portfolio_state(book, prices, total_value)

        # 1. Re-run held positions and act on SELL verdicts first, so capital
        #    freed this cycle is available to this cycle's buys.
        for symbol in held:
            evaluation = self._evaluate(adapter, universe, symbol, as_of, portfolio_state)
            if evaluation is None:
                continue
            position = book.positions.get(symbol)
            if position is None:
                continue

            new_tag = evaluation.health.classification
            if new_tag is not None and new_tag is not position.classification:
                record.reclassifications.append(
                    (symbol, position.classification.value, new_tag.value)
                )
                position.reclassified_to = new_tag

            reason = self._exit_reason(evaluation, position, as_of)
            if reason is not None:
                price = prices.get(symbol)
                if price is not None:
                    book.sell(
                        symbol, price * (1 - settings.transaction_cost), as_of, reason
                    )
                    record.sells.append((symbol, reason))

        # 2. Screen for new passers.
        prices = self._prices_on(set(candidates) | set(book.positions), as_of)
        total_value = book.total_value(prices)
        portfolio_state = self._portfolio_state(book, prices, total_value)

        passers: list[Evaluation] = []
        for symbol in candidates:
            if symbol in book.positions:
                continue
            evaluation = self._evaluate(adapter, universe, symbol, as_of, portfolio_state)
            if evaluation is None:
                continue
            record.evaluated += 1
            if evaluation.classification is not None:
                record.classifications[symbol] = evaluation.classification.value
            if evaluation.stopped_at:
                record.stopped_at[evaluation.stopped_at] = (
                    record.stopped_at.get(evaluation.stopped_at, 0) + 1
                )

            triangulation = evaluation.triangulation
            if triangulation is not None:
                if triangulation.mode.is_single:
                    record.single_anchor_candidates += 1
                elif triangulation.mode.value == "DUAL":
                    record.dual_anchor_candidates += 1
                    if triangulation.anchors_disagree:
                        record.divergent_candidates += 1

            if (
                evaluation.signal is not None
                and evaluation.signal.signal is SignalType.BUY
                and self.signal_filter(evaluation)
            ):
                passers.append(evaluation)

        record.passers = len(passers)

        # 3. Size and fill, best conviction first, respecting the cash floor.
        passers.sort(key=lambda e: e.conviction.total if e.conviction else 0.0, reverse=True)
        cash_floor = total_value * settings.min_cash_weight

        for evaluation in passers:
            if book.cash <= cash_floor:
                break
            price = prices.get(evaluation.symbol)
            if price is None:
                continue

            weight = self._weight_for(evaluation, portfolio_state)
            if weight <= 0:
                continue
            amount = min(total_value * weight, book.cash - cash_floor)
            if amount <= 0:
                continue

            fill = book.buy(
                evaluation.symbol,
                evaluation.classification,
                amount,
                price * (1 + settings.transaction_cost),
                as_of,
                conviction=evaluation.conviction.total if evaluation.conviction else 0.0,
                intended_weight=weight,
                anchor_mode=evaluation.anchor_mode.value,
                thesis_invalidation="backtest: no operator-supplied thesis",
            )
            if fill is not None:
                record.buys.append(evaluation.symbol)
                portfolio_state = self._portfolio_state(
                    book, prices, book.total_value(prices)
                )

        # 4. Mark and snapshot.
        prices = self._prices_on(list(book.positions), as_of)
        book.mark(prices)
        benchmark = self._price_on(settings.benchmark_symbol, as_of)
        book.snapshot(as_of, prices, benchmark)
        if benchmark is not None:
            result.benchmark_curve.append((as_of, benchmark))

        return record

    # -- helpers ----------------------------------------------------------
    def _evaluate(
        self,
        adapter: DataAdapter,
        universe: Universe,
        symbol: str,
        as_of: date,
        portfolio_state: PortfolioState,
    ) -> Evaluation | None:
        member = universe.by_symbol(symbol)
        if member is None:
            return None
        try:
            data = adapter.load_company(
                symbol,
                price_start=as_of - timedelta(days=730),
                price_end=as_of,
            )
        except DataUnavailable:
            return None
        except Exception:
            return None

        tag, _considered, _reasons = a_health.classify(data, self.config)
        if tag is None:
            multiple = "trailing_pe"
        else:
            multiple = c_anchors.anchor_multiple_for(tag, self.config)

        inputs = CandidateInputs(
            current_multiple=c_anchors.compute_current_multiple(data, multiple),
            trailing_pe=c_anchors.compute_current_multiple(data, "trailing_pe"),
            peer_candidates=find_peers(
                universe, member, self.config, adapter, multiple, as_of=as_of
            ),
            subject_group=member.industry,
            subject_growth=member.revenue_growth,
        )

        try:
            return evaluate_candidate(
                data,
                universe.market_data(self._base_market(adapter)),
                self.config,
                inputs,
                portfolio_state,
                FxTable({}, as_of),
                adapter=adapter,
                conviction_scorer=self.conviction_scorer,
                as_of=as_of,
            )
        except Exception:
            return None

    def _base_market(self, adapter: DataAdapter) -> MarketData:
        try:
            return adapter.get_market_data()
        except Exception:
            return MarketData()

    def _portfolio_state(
        self, book: BacktestBook, prices: dict[str, float], total_value: float
    ) -> PortfolioState:
        position_values = {
            s: p.market_value(prices.get(s, p.cost_basis / max(p.shares, 1e-9)))
            for s, p in book.positions.items()
        }
        core = sum(
            v for s, v in position_values.items()
            if book.positions[s].regime is Regime.CORE
        )
        growth = sum(
            v for s, v in position_values.items()
            if book.positions[s].regime is Regime.GROWTH
        )
        return PortfolioState(
            total_value=max(total_value, 1e-9),
            bucket_values={
                Bucket.BALLAST: book.cash,
                Bucket.CORE_PICKS: core,
                Bucket.GROWTH_PICKS: growth,
            },
            position_values=position_values,
            position_regimes={s: p.regime for s, p in book.positions.items()},
            single_anchor_positions={
                s for s, p in book.positions.items()
                if p.anchor_mode.startswith("SINGLE")
            },
        )

    def _weight_for(
        self, evaluation: Evaluation, portfolio_state: PortfolioState
    ) -> float:
        """Portfolio weight for one buy.

        Variants replace this wholesale (equal-weight, for instance), which is
        why it is a hook rather than inline arithmetic.
        """
        if self.sizer is not None:
            return self.sizer(evaluation, portfolio_state, self.config)
        if evaluation.sizing is not None and evaluation.sizing.qualified:
            return evaluation.sizing.portfolio_share
        return 0.0

    def _exit_reason(
        self, evaluation: Evaluation, position, as_of: date
    ) -> str | None:
        """Module A failure, H2's conviction floor, or the sell trigger."""
        if not evaluation.passed_health:
            return "Module A outright FAIL"

        conviction = evaluation.conviction
        if conviction is not None:
            if conviction.total < self.config.monitor.sell_conviction_floor:
                return (
                    f"conviction {conviction.total:.0f} below the absolute floor "
                    f"of {self.config.monitor.sell_conviction_floor:.0f}"
                )

        triangulation = evaluation.triangulation
        fair_value = evaluation.fair_value
        signal = evaluation.signal
        if (
            triangulation is not None
            and fair_value is not None
            and signal is not None
            and triangulation.both_confirm_overvaluation
        ):
            price = signal.values.get("price")
            if price is not None:
                try:
                    premium = -fair_value.discount_to(price)
                except Exception:
                    premium = None
                threshold = self.config.sell_threshold(evaluation.classification)
                if premium is not None and premium >= threshold:
                    return (
                        f"price {premium:.0%} above fair value with both anchors "
                        "confirming overvaluation"
                    )
        return None


__all__ = [
    "Backtester",
    "BacktestSettings",
    "BacktestResult",
    "WalkForwardSplit",
    "RebalanceRecord",
    "MANDATORY_WINDOWS",
    "month_ends",
]
