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

import re
import time

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
from ..universe import (
    Universe, build_universe, find_peers, group_price_to_book, industry_group_key,
)
from .portfolio import BacktestBook, Snapshot
from .timebudget import TimeBudgetExceeded, time_limit

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
    #: Hold money not in stock picks — the BALLAST bucket and any unfilled
    #: sleeve, which F3 sends to ballast — in the benchmark index rather than
    #: as cash at 0%. Module F defines ballast as "broad index funds, cash,
    #: short government bills, and gold"; modelling it as idle cash made the
    #: whole backtest a measure of how little was invested.
    ballast_in_index: bool = True
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
    #: Why candidates were not valued both ways: "C1 missing — <cause>" and
    #: "C2 missing — <cause>" counts, so a 98% single-anchor rate can be read
    #: as a data gap, a sampling artefact, or a design finding.
    anchor_gaps: dict[str, int] = field(default_factory=dict)
    #: Where each screened candidate dropped out, by stage and cause.
    funnel: dict[str, int] = field(default_factory=dict)


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
    #: Last month completed when the walk stopped at its deadline; None
    #: when the whole requested period was covered.
    stopped_early_at: date | None = None
    #: Seconds spent per stage of the walk, for the report's timing section.
    timing: dict[str, float] = field(default_factory=dict)

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


def _plain(reason: str | None) -> str:
    """A reason with its numbers and names removed, so causes aggregate."""
    text = (reason or "no reason given").split(" — ")[0]
    text = re.sub(r"\d+(\.\d+)?", "N", text)
    return text[:90]


def anchor_gap_causes(triangulation, peer_gap: str | None = None) -> list[str]:
    """Each anchor that could not be computed, with an aggregatable cause.

    ``peer_gap`` says why C2 was offered no candidates at all, when known.
    """
    out: list[str] = []
    if not triangulation.c1.computable:
        out.append(f"C1 missing — {_plain(triangulation.c1.reason)}")
    c2 = triangulation.c2
    if not c2.computable:
        decisions = list(c2.peer_decisions)
        if not decisions:
            reason = c2.reason or ""
            cause = (
                f"no candidates offered — {peer_gap}" if peer_gap
                else "no same-industry candidates offered"
                if "peers" in reason or not reason
                else _plain(reason)
            )
        else:
            kept = sum(1 for d in decisions if d.included)
            rejected = [d.reason for d in decisions if not d.included]
            buckets = {
                "size band": sum("market cap" in r for r in rejected),
                "growth band": sum("growth" in r for r in rejected),
                "multiple not computable": sum(
                    "not computable" in r or "non-positive" in r for r in rejected
                ),
                "different grouping": sum("different grouping" in r for r in rejected),
            }
            top = max(buckets, key=buckets.get) if rejected else "none"
            cause = (
                f"{min(kept, 3)} of {len(decisions) if len(decisions) < 5 else '5+'} "
                f"candidates passed (need 4); most rejected on: {top}"
            )
        out.append(f"C2 missing — {cause}")
    return out


_STAGE_NAMES = {
    "A": "1 health checks (Module A)",
    "B": "2 fair value (Module B)",
    "C5": "3 valuation anchors (Module C)",
    "C": "3 valuation anchors (Module C)",
    "D": "4 conviction (Module D)",
}


def funnel_steps(evaluation) -> list[str]:
    """Where one candidate dropped out, as aggregatable labels.

    Stage first, then the first blocking cause; for names that reach a
    signal, which of E's BUY conditions failed. Numbers are stripped so the
    same rule failing at different values counts together.
    """
    stopped = evaluation.stopped_at
    if stopped:
        stage = _STAGE_NAMES.get(stopped, stopped)
        first = (evaluation.stop_reason or "").split("; ")[0]
        return [f"stopped at {stage} — {_plain(first)}"]
    signal = evaluation.signal
    if signal is None:
        return ["no signal produced"]
    kind = signal.signal.value
    if kind == "BUY":
        return ["5 signal: BUY"]
    failed = [name for name, met in signal.conditions.items() if not met]
    steps = [f"5 signal: {kind}"]
    steps += [f"5 BUY condition failed — {_plain(name)}" for name in failed]
    if not failed and signal.reasons:
        steps.append(f"5 not a BUY — {_plain(signal.reasons[0])}")
    return steps


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
        eligibility: Callable[[str, date], bool] | None = None,
        peer_pool: Callable[[str], Sequence[str]] | None = None,
        member_cache: dict | None = None,
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
        #: Which symbols existed as reporting companies on a date.  A
        #: point-in-time universe passes its filing index here so a company
        #: is screened only while it was actually filing — before its first
        #: report it did not exist, after its last it was gone.
        self.eligibility = eligibility
        #: Same-industry names to offer C2 as peers, beyond the sample. A
        #: random sample of a few hundred filers almost never holds two
        #: companies in one industry, so peers drawn from it alone left C2
        #: with nothing and put every candidate in SINGLE-ANCHOR MODE.
        self.peer_pool = peer_pool
        #: Screened universe members by (symbol, date, universe settings),
        #: shared across the benchmark runs, which replay the same dates
        #: over the same companies with the same screen.
        self.member_cache = member_cache if member_cache is not None else {}

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
        symbols = self.symbols
        if self.eligibility is not None:
            symbols = [s for s in symbols if self.eligibility(s, as_of)]
        members = self._screened(
            adapter, symbols, config, as_of,
            progress=getattr(self, "_universe_progress", False),
        )
        return Universe(
            as_of=as_of, members=members, source=getattr(adapter, "name", "unknown")
        )

    def _screened(
        self,
        adapter: DataAdapter,
        symbols: Sequence[str],
        config: Config,
        as_of: date,
        *,
        progress: bool = False,
    ) -> list:
        """Universe members for ``symbols`` on ``as_of``, screening only the
        ones no earlier run has screened with the same settings."""
        settings_key = repr(config.universe)
        missing = [
            s for s in symbols if (s, as_of, settings_key) not in self.member_cache
        ]
        if missing:
            screened = build_universe(
                adapter, missing, config, as_of=as_of, progress=progress
            )
            for member in screened.members:
                self.member_cache[(member.symbol, as_of, settings_key)] = member
            for symbol in missing:
                # Unreachable names are remembered too, so they are not retried.
                self.member_cache.setdefault((symbol, as_of, settings_key), None)
        return [
            member for s in symbols
            if (member := self.member_cache.get((s, as_of, settings_key))) is not None
        ]

    def _with_industry_peers(
        self,
        adapter: DataAdapter,
        universe: Universe,
        candidates: Sequence[str],
        as_of: date,
    ) -> Universe:
        present = {m.symbol for m in universe.members}
        wanted: list[str] = []
        for symbol in candidates:
            try:
                peers = self.peer_pool(symbol)
            except Exception:
                continue
            for peer in peers:
                if peer not in present and peer not in wanted:
                    wanted.append(peer)

        peers = self._screened(adapter, wanted, self.config, as_of)
        return replace(universe, members=list(universe.members) + peers)

    # -- prices -----------------------------------------------------------
    def _price_on(self, symbol: str, as_of: date) -> float | None:
        """The last close at or before ``as_of``, within a fortnight.

        On the basis adjusted for every split to date, where the adapter
        offers one: the book holds share counts across splits, so its prices
        must not step down when a holding splits.
        """
        fetch = (
            self.adapter.get_adjusted_prices
            if hasattr(type(self.adapter), "get_adjusted_prices")
            else self.adapter.get_prices
        )
        try:
            prices = fetch(symbol, as_of - timedelta(days=14), as_of)
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
    def run(self, progress: bool = False, deadline: float | None = None) -> BacktestResult:
        """Walk the period month by month.

        ``deadline`` is a ``time.monotonic()`` value. Past it, the walk stops
        at the last completed month and says so, rather than being killed by
        the job's time limit with nothing reported.
        """
        settings = self.settings
        book = BacktestBook(cash=settings.initial_capital)
        self._ballast_level: float | None = None
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

        started = time.monotonic()
        final = dates[-1]
        try:
            # The check between months is the orderly stop; the timer is the
            # guarantee, for a month that stalls inside a download.
            with time_limit(deadline):
                for index, as_of in enumerate(dates):
                    if deadline is not None and index > 0 and time.monotonic() > deadline:
                        final = dates[index - 1]
                        self._note_early_stop(result, dates, index, final, during=False)
                        break
                    # The first month downloads every company's filings and
                    # prices, so it reports per company; later months run from
                    # memory and report one line each. A line a year looked
                    # exactly like a hang.
                    self._universe_progress = progress and index == 0
                    if self._universe_progress:
                        print(f"  {as_of.isoformat()}: first month — downloading "
                              f"{len(self.symbols)} companies", flush=True)
                    record = self._rebalance(book, as_of, result)
                    result.rebalances.append(record)
                    if progress:
                        minutes = (time.monotonic() - started) / 60
                        print(
                            f"  {as_of.isoformat()} ({index + 1}/{len(dates)}, "
                            f"{minutes:.0f} min): screened {record.evaluated}, "
                            f"passed {record.passers}, holding {len(book.positions)}",
                            flush=True,
                        )
        except TimeBudgetExceeded:
            # Interrupted inside a month. Trades already made in it stand, so
            # everything is closed at that month's date.
            completed = len(result.rebalances)
            final = dates[min(completed, len(dates) - 1)]
            self._note_early_stop(result, dates, completed, final, during=True)
            if progress:
                print(f"  time budget reached during {final.isoformat()}", flush=True)

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

    def _grow_ballast(self, book: BacktestBook, as_of: date) -> None:
        """Move the uninvested money with the index since the last rebalance.

        A month with no index price carries the last level forward, so the
        next priced month books the whole move rather than losing it.
        """
        level = self._price_on(self.settings.benchmark_symbol, as_of)
        if level is None or level <= 0:
            return
        previous = self._ballast_level
        if previous:
            book.cash *= level / previous
        self._ballast_level = level

    @staticmethod
    def _tick(result: BacktestResult, stage: str, since: float) -> None:
        result.timing[stage] = result.timing.get(stage, 0.0) + time.monotonic() - since

    @staticmethod
    def _note_early_stop(
        result: BacktestResult,
        dates: Sequence[date],
        completed: int,
        last: date,
        *,
        during: bool,
    ) -> None:
        where = f"during {last.isoformat()}" if during else f"at {last.isoformat()}"
        result.notes.append(
            f"STOPPED EARLY {where} ({completed} of {len(dates)} months "
            f"complete) to finish inside the time budget. Every figure covers "
            f"{dates[0].isoformat()}..{last.isoformat()} only, not the period "
            "requested."
        )
        result.stopped_early_at = last

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
        if settings.ballast_in_index:
            self._grow_ballast(book, as_of)

        clock = time.monotonic()
        universe = self.universe_builder(adapter, self.config, as_of)
        candidates = [m.symbol for m in universe.included]
        self._tick(result, "screen sample", clock)
        if self.peer_pool is not None:
            # Peers are added after the candidate list is fixed: they are
            # reference points for C2, never things to buy.
            clock = time.monotonic()
            universe = self._with_industry_peers(adapter, universe, candidates, as_of)
            self._tick(result, "screen peers", clock)
        clock = time.monotonic()

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
            for step in funnel_steps(evaluation):
                record.funnel[step] = record.funnel.get(step, 0) + 1
            if triangulation is not None:
                for gap in anchor_gap_causes(
                    triangulation, getattr(evaluation, "peer_gap", None)
                ):
                    record.anchor_gaps[gap] = record.anchor_gaps.get(gap, 0) + 1
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

        self._tick(result, "evaluate and trade", clock)
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

        peers = find_peers(universe, member, self.config, adapter, multiple, as_of=as_of)
        subject_group = member.industry
        if self.config.anchors.peer_group_fallback:
            kept, _ = c_anchors.select_peers(
                subject_group, member.market_cap, member.revenue_growth,
                peers, self.config,
            )
            if len(kept) < self.config.anchors.peer_min:
                wider = industry_group_key(member)
                if wider:
                    peers = find_peers(
                        universe, member, self.config, adapter, multiple,
                        as_of=as_of, grouping=industry_group_key,
                    )
                    subject_group = wider

        inputs = CandidateInputs(
            current_multiple=c_anchors.compute_current_multiple(data, multiple),
            trailing_pe=c_anchors.compute_current_multiple(data, "trailing_pe"),
            peer_candidates=peers,
            subject_group=subject_group,
            subject_growth=member.revenue_growth,
            sector_price_to_book=(
                group_price_to_book(universe, banks=member.is_bank)
                if member.is_bank or member.is_insurer
                else None
            ),
        )

        try:
            evaluation = evaluate_candidate(
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
        if not peers:
            evaluation.peer_gap = self._why_no_peer_candidates(universe, member)
        return evaluation

    def _why_no_peer_candidates(self, universe: Universe, member) -> str:
        """Why C2 was offered nobody: a data gap, or the spec's size band.

        "No candidates" covered both, and they call for different responses —
        the first is fixable, the second is the rule working.
        """
        if not member.market_cap:
            return "company's own market cap unknown"
        same = [
            m for m in universe.included
            if m.symbol != member.symbol and m.industry and m.industry == member.industry
        ]
        if not same:
            return "no other company in its industry in the screened universe"
        low = self.config.anchors.peer_market_cap_low
        high = self.config.anchors.peer_market_cap_high
        return (
            f"{min(len(same), 5) if len(same) < 5 else '5+'} same-industry, none "
            f"within the {low:g}-{high:g}x size band"
        )

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
