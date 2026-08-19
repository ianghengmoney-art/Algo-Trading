"""LEAN entry point -- GCFP v3 on QuantConnect paper trading.

Runs the unmodified `gcfp` decision engine inside a QC algorithm. Modules A
through E are untouched: this file supplies the data, the schedule and the
portfolio picture, and hands the verdicts to `execution.plan_orders`.

Order calls live only in `execution.apply_orders`. Nothing here can move
capital directly, which keeps the boundary a one-file audit.

DRY RUN BY DEFAULT. `live_enabled` is a QC algorithm parameter that must be
set to "1" before a single simulated order is placed. Default-on would mean a
misconfigured deploy starts trading, and the whole point of Module G is that
the gap between intent and execution is where this strategy actually fails.
"""

from datetime import timedelta

from AlgorithmImports import (
    AccountType,
    BrokerageName,
    Fundamental,
    QCAlgorithm,
    Resolution,
)

from gcfp.config import DEFAULT_PARAMS, GROWTH_SLEEVE, SLEEVE_OF
from gcfp.data.quantconnect import QCDataAdapter
from gcfp.data.registry import capability_gate
from gcfp.engine import screen
from gcfp.modules.f_sizing import Portfolio, Position, allocate
from gcfp.modules.i_expectations import report_header
from qc_algorithm.execution import apply_orders, plan_orders
from qc_algorithm.state import ObjectStoreState

# Section 12 wants the mandatory windows covered. Backtests are expected to be
# configured from the LEAN config; these are only defaults for a bare run.
DEFAULT_START = (2015, 1, 1)
DEFAULT_CASH = 100_000


class GCFPAlgorithm(QCAlgorithm):
    def initialize(self):
        self.set_start_date(*DEFAULT_START)
        self.set_cash(DEFAULT_CASH)

        # Paper trading. No external brokerage credentials are involved.
        self.set_brokerage_model(BrokerageName.QUANTCONNECT_BROKERAGE, AccountType.MARGIN)

        self.params = DEFAULT_PARAMS
        self.live_enabled = str(self.get_parameter("live_enabled") or "0") == "1"
        self.ballast_pct = float(self.get_parameter("ballast_pct") or 50.0)

        self.universe_settings.resolution = Resolution.DAILY
        self.add_universe(self._select_universe)

        self.state = ObjectStoreState(self.object_store)
        self.adapter = QCDataAdapter(history_provider=self._fundamental_history)

        # Section 14 cadence: a weekly new-passer report, and a full Module A-E
        # re-run each quarter after earnings season.
        self.schedule.on(
            self.date_rules.week_start(), self.time_rules.at(9, 45), self._weekly_screen
        )
        self.schedule.on(
            self.date_rules.month_start(), self.time_rules.at(10, 15), self._quarterly_monitor
        )

        self._selected = []
        self.log(report_header(self.params))
        if not self.live_enabled:
            self.log(
                "DRY RUN: live_enabled is not set. The full pipeline runs and every intended "
                "order is logged, but nothing is submitted. Set the live_enabled parameter to "
                "1 to place simulated orders."
            )

    # -- universe ---------------------------------------------------------
    def _select_universe(self, fundamental):
        """Point-in-time universe, filtered on the Module A preamble.

        The liquidity floor is the standard USD 2M here rather than the USD 5M
        Growth floor, because classification has not happened yet -- Module A
        re-applies the correct per-path floor once the tag is known.
        """
        universe = self.params.universe
        candidates = [
            f for f in fundamental
            if f.has_fundamental_data
            and f.market_cap
            and f.market_cap >= universe.min_market_cap_usd
            and f.dollar_volume >= universe.min_adv_usd
            and f.security_reference.is_primary_share
            and not f.security_reference.is_depositary_receipt
        ]
        candidates.sort(key=lambda f: f.market_cap, reverse=True)
        self._selected = candidates[:400]
        self.adapter.set_universe(self._selected)
        self.adapter.as_of = self.time.date()
        return [f.symbol for f in self._selected]

    def _fundamental_history(self, symbol, years):
        """Fundamental snapshots oldest-first, for the C1 anchor and statements."""
        try:
            rows = self.history[Fundamental](symbol, timedelta(days=int(365.25 * years)), Resolution.DAILY)
            return list(rows)
        except Exception as exc:  # history is best-effort; a gap is a DATA_GAP
            self.debug(f"fundamental history unavailable for {symbol}: {exc}")
            return []

    # -- portfolio picture -------------------------------------------------
    def _portfolio(self):
        total = float(self.portfolio.total_portfolio_value)
        book = self.state.holdings()
        positions = []
        for symbol, holding in self.portfolio.items():
            if not holding.invested:
                continue
            record = book.get(str(symbol), {})
            positions.append(
                Position(
                    symbol=str(symbol),
                    classification=record.get("classification", "CORE-STABLE"),
                    sector=record.get("sector"),
                    cost_value=float(holding.absolute_holdings_cost),
                    current_value=float(holding.holdings_value),
                )
            )
        ballast = total * self.ballast_pct / 100.0
        return Portfolio(total_value=total, ballast_value=ballast, positions=positions)

    def _current_weights_pct(self):
        total = float(self.portfolio.total_portfolio_value) or 1.0
        return {
            str(symbol): 100.0 * float(holding.holdings_value) / total
            for symbol, holding in self.portfolio.items()
            if holding.invested
        }

    def _growth_weight_pct(self):
        book = self.state.holdings()
        return sum(
            weight
            for symbol, weight in self._current_weights_pct().items()
            if SLEEVE_OF.get(book.get(symbol, {}).get("classification")) == GROWTH_SLEEVE
        )

    # -- scheduled work ----------------------------------------------------
    def _weekly_screen(self):
        if not self._selected:
            return

        symbols = self.adapter.symbols()
        gate = capability_gate(self.adapter, symbols=symbols[:5])
        for notice in gate.blocking_notices():
            self.log(f"!! {notice}")

        result = screen(self.adapter, symbols, self.params, gate, as_of=self.time.date())
        passers = result.passers()

        portfolio = self._portfolio()
        allocator = allocate(
            [
                (a.symbol, a.classification, a.candidate.profile.sector, a.conviction.total)
                for a in passers
            ],
            portfolio,
            self.params,
        )
        for notice in allocator.header_notices:
            self.log(f"!! {notice}")

        plan = plan_orders(
            allocator.decisions,
            exits=[],
            trims=[],
            current_weights_pct=self._current_weights_pct(),
            params=self.params,
            current_growth_weight_pct=self._growth_weight_pct(),
        )
        for line in plan.notices:
            self.log(f"!! {line}")
        for line in apply_orders(self, plan, live_enabled=self.live_enabled):
            self.log(line)

        for assessment in passers:
            if self.live_enabled:
                self.state.record_holding(
                    assessment.symbol,
                    assessment.classification,
                    assessment.candidate.profile.sector,
                    self.time.date(),
                    assessment.conviction.total,
                    bool(assessment.triangulation and assessment.triangulation.both_confirm_undervalued),
                )
            self.state.record_conviction(
                assessment.symbol, self.time.date(),
                assessment.conviction.total, assessment.conviction.breakdown(),
            )

        self.state.record_run(self.time.date(), len(symbols), len(passers), result.header_notices)
        self.log(f"weekly screen: {len(symbols)} in universe, {len(passers)} passers")

    def _quarterly_monitor(self):
        """Full Module A-E re-run on holdings, quarterly."""
        if self.time.month % 3 != 1:
            return

        held = [s for s in self.adapter.symbols() if self.portfolio[s].invested]
        if not held:
            return

        gate = capability_gate(self.adapter, symbols=held[:5])
        result = screen(self.adapter, held, self.params, gate, as_of=self.time.date())

        exits = []
        for assessment in result.assessments:
            # A holding that no longer clears Module A is a SELL on
            # fundamentals, independent of price.
            if assessment.stopped_at == "A":
                exits.append(assessment.symbol)

        plan = plan_orders(
            [], exits=exits, trims=[],
            current_weights_pct=self._current_weights_pct(),
            params=self.params,
            current_growth_weight_pct=self._growth_weight_pct(),
        )
        for line in apply_orders(self, plan, live_enabled=self.live_enabled):
            self.log(line)
        for symbol in exits:
            if self.live_enabled:
                self.state.close_holding(symbol, self.time.date(), "Module A FAIL on quarterly re-run")
        self.log(f"quarterly monitor: {len(held)} holdings, {len(exits)} exits")
