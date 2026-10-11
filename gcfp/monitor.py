"""The holdings monitoring loop.

Module H says *what* to check and *when*; this runs it.  Two details the loop
exists to get right, both of which v3 got wrong:

* **Cadence follows the holding, not the calendar.**  A semi-annual reporter
  re-run every quarter either processes stale data or silently skips.  So each
  position is asked when *it* last reported, and only the ones with something
  new get a full A->E re-run.
* **Conviction is compared to what it was at purchase.**  That comparison is
  the whole of H2's REVIEW logic, and it only works because the store appends
  scores rather than updating them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from .classification import Classification, regime_of
from .config import Config
from .data.adapter import DataAdapter, DataUnavailable
from .ledger import AuditLedger
from .modules import a_health, g_execution, h_monitor
from .modules.f_sizing import PortfolioState
from .modules.h_monitor import MonitorFlag, MonitorVerdict
from .modules.k_currency import FxTable
from .pipeline import CandidateInputs, Evaluation, evaluate_candidate
from .storage import Store
from .types import MarketData


@dataclass
class Holding:
    """One position, as the monitor needs to see it."""

    symbol: str
    classification: Classification
    opened_on: date
    shares: float
    cost_base_currency: float
    thesis_invalidation: str
    conviction_at_purchase: float
    last_full_rerun: date | None = None
    intended_amount_base: float | None = None
    actual_amount_base: float | None = None
    #: Set by the operator when the "what would change my mind" condition has
    #: actually been met.  Nothing automates this: the condition is written in
    #: prose precisely because it is a judgement.
    thesis_broken: bool = False
    growth_streaks: h_monitor.GrowthDeterioration | None = None


@dataclass
class HoldingReview:
    """What the loop concluded about one position."""

    holding: Holding
    verdict: MonitorVerdict
    cadence: h_monitor.CadenceDecision
    evaluation: Evaluation | None = None
    conviction_now: float | None = None
    reclassification: h_monitor.ReclassificationProtocol | None = None
    error: str | None = None

    @property
    def needs_attention(self) -> bool:
        return self.verdict.flag is not MonitorFlag.GREEN

    def as_report_lines(self) -> list[str]:
        lines = list(self.verdict.as_report_lines())
        lines.insert(1, f"  {self.cadence.as_log_line()}")
        if self.conviction_now is not None:
            change = self.conviction_now - self.holding.conviction_at_purchase
            lines.insert(
                2,
                f"  conviction {self.holding.conviction_at_purchase:.1f} -> "
                f"{self.conviction_now:.1f} ({change:+.1f})",
            )
        if self.reclassification is not None:
            lines.extend(f"  {l}" for l in self.reclassification.as_report_lines())
        if self.error:
            lines.append(f"  ERROR: {self.error}")
        return lines


def load_holdings(store: Store) -> list[Holding]:
    """Open positions, with each one's conviction at purchase.

    The purchase score comes from the earliest row in conviction history —
    which is only meaningful because that table is append-only.
    """
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM holdings WHERE closed_on IS NULL ORDER BY opened_on"
        ).fetchall()

    holdings: list[Holding] = []
    for row in rows:
        at_purchase = store.conviction_at_purchase(row["symbol"])
        holdings.append(
            Holding(
                symbol=row["symbol"],
                classification=Classification(row["classification"]),
                opened_on=date.fromisoformat(row["opened_on"]),
                shares=row["shares"] or 0.0,
                cost_base_currency=row["cost_base_currency"] or 0.0,
                thesis_invalidation=row["thesis_invalidation"],
                conviction_at_purchase=at_purchase if at_purchase is not None else 0.0,
            )
        )
    return holdings


def review_holding(
    holding: Holding,
    adapter: DataAdapter,
    market: MarketData,
    config: Config,
    inputs: CandidateInputs,
    portfolio: PortfolioState,
    fx: FxTable,
    *,
    as_of: date | None = None,
) -> HoldingReview:
    """Apply H1's cadence, then re-run A->E if this holding has reported."""
    as_of = as_of or date.today()

    try:
        data = adapter.load_company(holding.symbol)
    except DataUnavailable as exc:
        # A position whose data has gone missing is not healthy by default.
        verdict = MonitorVerdict(
            holding.symbol,
            MonitorFlag.REVIEW,
            review_reasons=[f"data unavailable: {exc}"],
        )
        return HoldingReview(
            holding,
            verdict,
            h_monitor.CadenceDecision(
                False, False, holding.classification and
                __import__("gcfp.types", fromlist=["ReportingFrequency"]).ReportingFrequency.UNKNOWN,
                "data unavailable",
            ),
            error=str(exc),
        )

    cadence = h_monitor.cadence(data, config, holding.last_full_rerun, as_of)

    # A late filing is an A4 failure regardless of cadence (H1).
    late = h_monitor.filing_is_late(data, config, as_of)

    if not cadence.should_full_rerun:
        reasons = []
        if late:
            reasons.append("scheduled report more than 15 days late — A4 failure")
        flag = MonitorFlag.SELL if late else MonitorFlag.GREEN
        verdict = MonitorVerdict(
            holding.symbol, flag, sell_reasons=reasons,
            notes=[cadence.reason] if not late else [],
        )
        return HoldingReview(holding, verdict, cadence)

    evaluation = evaluate_candidate(
        data, market, config, inputs, portfolio, fx, adapter=adapter, as_of=as_of
    )

    module_a_failed = not evaluation.passed_health or late
    conviction_now = (
        evaluation.conviction.total if evaluation.conviction else 0.0
    )

    # H3: a changed tag freezes the position and starts the six-step protocol.
    reclassification = None
    new_tag = evaluation.health.classification
    if new_tag is not None and new_tag is not holding.classification:
        reclassification = h_monitor.start_reclassification(
            holding.symbol, holding.classification, new_tag, as_of
        )

    triangulation = evaluation.triangulation
    verdict = h_monitor.evaluate_holding(
        holding.symbol,
        new_tag or holding.classification,
        config,
        module_a_failed=module_a_failed,
        both_anchors_overvalued=(
            triangulation.both_confirm_overvaluation if triangulation else False
        ),
        premium_above_threshold=_premium_above_threshold(evaluation, config),
        thesis_broken=holding.thesis_broken,
        conviction_now=conviction_now,
        conviction_at_purchase=holding.conviction_at_purchase,
        gates_weakening=_gates_weakening(evaluation),
        anchors_now_diverging=(
            triangulation.anchors_disagree if triangulation else False
        ),
        re_rating_detected=_re_rating_detected(evaluation),
        past_hard_cap=_past_hard_cap(holding, portfolio, config),
        classification_changed=reclassification is not None,
        growth=holding.growth_streaks,
        ledger=evaluation.ledger,
    )

    return HoldingReview(
        holding, verdict, cadence, evaluation, conviction_now, reclassification
    )


def _premium_above_threshold(evaluation: Evaluation, config: Config) -> bool:
    if evaluation.fair_value is None or evaluation.signal is None:
        return False
    price = evaluation.signal.values.get("price")
    if price is None:
        return False
    try:
        premium = -evaluation.fair_value.discount_to(price)
    except Exception:
        return False
    classification = evaluation.classification
    if classification is None:
        return False
    return premium >= config.sell_threshold(classification)


def _gates_weakening(evaluation: Evaluation) -> bool:
    """A gate that passed but sits close to its threshold.

    H2 asks for "Module A gates weakening but not failing", which needs a
    notion of proximity: within 10% of the threshold counts.
    """
    for gate in evaluation.health.gates:
        if not gate.passed or gate.value is None or not gate.threshold:
            continue
        if gate.gate == "A2":  # lower is better
            if gate.value > gate.threshold * 0.90:
                return True
        elif gate.gate in ("A1", "A3"):  # higher is better
            if gate.value < gate.threshold * 1.10:
                return True
    return False


def _re_rating_detected(evaluation: Evaluation) -> bool:
    triangulation = evaluation.triangulation
    if triangulation is None:
        return False
    return bool(triangulation.c1.detail.get("re_rating_detected"))


def _past_hard_cap(
    holding: Holding, portfolio: PortfolioState, config: Config
) -> bool:
    """Whether appreciation has carried the position past its per-name cap."""
    if portfolio.total_value <= 0:
        return False
    regime = regime_of(holding.classification)
    sleeve = portfolio.sleeve_value(regime)
    if sleeve <= 0:
        return False
    held = portfolio.position_values.get(holding.symbol)
    if held is None:
        return False
    cap = (
        config.sizing.core_name_cap
        if regime.value == "CORE"
        else config.sizing.growth_name_cap
    )
    return held / sleeve > cap


def run_monitor(
    store: Store,
    adapter: DataAdapter,
    market: MarketData,
    config: Config,
    portfolio: PortfolioState,
    fx: FxTable,
    inputs_by_symbol: dict[str, CandidateInputs] | None = None,
    *,
    as_of: date | None = None,
    persist: bool = True,
) -> list[HoldingReview]:
    """Review every open position and record what changed."""
    as_of = as_of or date.today()
    inputs_by_symbol = inputs_by_symbol or {}
    reviews: list[HoldingReview] = []

    for holding in load_holdings(store):
        review = review_holding(
            holding,
            adapter,
            market,
            config,
            inputs_by_symbol.get(holding.symbol, CandidateInputs()),
            portfolio,
            fx,
            as_of=as_of,
        )
        reviews.append(review)

        if persist and review.evaluation is not None:
            evaluation = review.evaluation
            store.record_audit(evaluation.ledger)
            if evaluation.conviction is not None:
                store.record_conviction(
                    evaluation.conviction,
                    evaluation.anchor_mode.value,
                    config.fingerprint,
                    scored_on=as_of,
                )
            if review.reclassification is not None:
                store.record_transition(review.reclassification)

    return reviews


def discipline_report(
    reviews: Sequence[HoldingReview],
    portfolio: PortfolioState,
    config: Config,
    executions: Sequence[g_execution.ExecutionRecord] = (),
    as_of: date | None = None,
) -> g_execution.QuarterlyDisciplineReport:
    """Module G's quarterly numbers, built from the same pass."""
    as_of = as_of or date.today()
    regime_caps = {
        r.holding.symbol: (
            config.sizing.core_name_cap
            if regime_of(r.holding.classification).value == "CORE"
            else config.sizing.growth_name_cap
        )
        for r in reviews
    }
    shares = {}
    for review in reviews:
        regime = regime_of(review.holding.classification)
        sleeve = portfolio.sleeve_value(regime)
        held = portfolio.position_values.get(review.holding.symbol)
        if held is not None and sleeve > 0:
            shares[review.holding.symbol] = held / sleeve

    thesis_broken = [
        r.holding.symbol for r in reviews if r.holding.thesis_broken
    ]
    return g_execution.quarterly_review(
        executions, shares, regime_caps, thesis_broken, as_of
    )


__all__ = [
    "Holding",
    "HoldingReview",
    "load_holdings",
    "review_holding",
    "run_monitor",
    "discipline_report",
]
