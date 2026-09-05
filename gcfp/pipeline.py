"""Orchestration: run Modules A -> K in the order the spec fixes.

The ordering is not incidental.  Health before value (Prime Directive 2) means
Module A gates before Module B runs at all; C5's anchor mode sets the buy
threshold that D2 scores excess against, so C must complete before D; and F's
sizing depends on D's conviction band.  Running these out of order produces
numbers that look right and are not, which is why the sequence lives in one
function rather than being reassembled per caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Sequence

from .classification import Classification, Regime, regime_of
from .config import Config
from .data.adapter import DataAdapter, DataUnavailable
from .ledger import AuditLedger
from .modules import (
    a_health,
    b_valuation,
    c_anchors,
    d_conviction,
    e_triggers,
    f_sizing,
    j_tax,
    k_currency,
)
from .modules.b_valuation import FairValue, MethodRefused
from .modules.c_anchors import AnchorMode, PeerCandidate, TriangulationResult
from .modules.d_conviction import ConvictionScore
from .modules.e_triggers import Signal, SignalType
from .modules.f_sizing import PortfolioState, SizingDecision
from .types import CompanyData, MarketData, TamSource


@dataclass
class CandidateInputs:
    """The per-name inputs the pipeline cannot derive from the adapter alone.

    Peer multiples, TAM citations, and sector reference multiples are judgement
    inputs the operator supplies.  Keeping them in one explicit object means an
    evaluation that silently ran without a TAM citation is visible in the call
    site, not buried three modules down.
    """

    current_multiple: float | None = None
    trailing_pe: float | None = None
    peer_candidates: Sequence[PeerCandidate] = ()
    subject_group: str | None = None
    subject_growth: float | None = None
    tam_sources: Sequence[TamSource] = ()
    industry_revenue: float | None = None
    industry_cagr: float | None = None
    sector_price_to_book: float | None = None
    peer_price_to_affo: float | None = None
    thesis_invalidation: str = ""


def _ensure_anchor_series(
    data: CompanyData,
    adapter: DataAdapter | None,
    classification: Classification,
    config: Config,
    ledger: AuditLedger,
) -> CompanyData:
    """Fetch C1's multiple series once the classification is known.

    Which multiple C1 uses depends on the classification, and the
    classification is not known until A6 has run — so the series cannot be
    loaded up front with the rest of the company.  Fetching it here also means
    a P/AFFO series is never pulled for a name that turns out to be an
    industrial.

    A caller that already supplied the series (a fixture, or a backtest
    replaying cached data) keeps theirs untouched.
    """
    if data.multiples or adapter is None:
        return data

    multiple = c_anchors.anchor_multiple_for(classification, config)
    try:
        series = adapter.get_historical_multiples(
            data.profile.symbol, multiple, config.anchors.history_window_years
        )
    except DataUnavailable as exc:
        ledger.note(f"C1 series unavailable ({multiple}): {exc}")
        return data
    except Exception as exc:  # provider-specific transport failures
        ledger.note(f"C1 series error ({multiple}): {type(exc).__name__}: {exc}")
        return data

    return replace(data, multiples=tuple(series))


@dataclass
class Evaluation:
    """Everything one pass over one company produced."""

    symbol: str
    ledger: AuditLedger
    health: a_health.HealthAssessment
    classification: Classification | None = None
    fair_value: FairValue | None = None
    triangulation: TriangulationResult | None = None
    conviction: ConvictionScore | None = None
    signal: Signal | None = None
    sizing: SizingDecision | None = None
    dual_currency: k_currency.DualReport | None = None
    yield_assessment: j_tax.YieldAssessment | None = None
    adr: k_currency.AdrExposure | None = None
    stopped_at: str | None = None
    stop_reason: str | None = None

    @property
    def passed_health(self) -> bool:
        return self.health.passed

    @property
    def is_buy(self) -> bool:
        return self.signal is not None and self.signal.signal is SignalType.BUY

    @property
    def anchor_mode(self) -> AnchorMode:
        return self.triangulation.mode if self.triangulation else AnchorMode.NONE


def evaluate_candidate(
    data: CompanyData,
    market: MarketData,
    config: Config,
    inputs: CandidateInputs,
    portfolio: PortfolioState,
    fx: k_currency.FxTable,
    *,
    adapter: DataAdapter | None = None,
    momentum_returns: dict[str, float | None] | None = None,
    as_of: date | None = None,
) -> Evaluation:
    """Run one candidate through the full stack.

    Stops at the first module that cannot proceed, recording where and why.  A
    name that stopped at Module A never reaches valuation, which is the point of
    Prime Directive 2 rather than an implementation convenience.
    """
    as_of = as_of or date.today()
    ledger = AuditLedger(data.profile.symbol, as_of)

    # -- Module A ---------------------------------------------------------
    health = a_health.run_module_a(data, market, config, ledger, as_of)
    evaluation = Evaluation(data.profile.symbol, ledger, health)
    if not health.passed or health.classification is None:
        evaluation.stopped_at = "A"
        evaluation.stop_reason = "; ".join(
            g.describe() for g in health.gates if g.failed or not g.computable
        )
        return evaluation

    classification = health.classification
    evaluation.classification = classification

    # C1's multiple depends on the classification A6 just assigned, so the
    # series is fetched here rather than with the rest of the company data.
    data = _ensure_anchor_series(data, adapter, classification, config, ledger)

    # -- Module B ---------------------------------------------------------
    try:
        fair_value = b_valuation.value(
            data,
            market,
            config,
            classification,
            ledger,
            tam_sources=inputs.tam_sources,
            industry_revenue=inputs.industry_revenue,
            industry_cagr=inputs.industry_cagr,
            sector_price_to_book=inputs.sector_price_to_book,
            peer_price_to_affo=inputs.peer_price_to_affo,
        )
    except (MethodRefused, DataUnavailable) as exc:
        evaluation.stopped_at = "B"
        evaluation.stop_reason = f"{type(exc).__name__}: {exc}"
        ledger.note(f"B · STOPPED — {exc}")
        return evaluation
    evaluation.fair_value = fair_value

    # -- Module C ---------------------------------------------------------
    c1 = c_anchors.compute_c1(
        data, config, classification, inputs.current_multiple, ledger, as_of
    )
    c2 = c_anchors.compute_c2(
        data,
        config,
        inputs.current_multiple,
        inputs.peer_candidates,
        inputs.subject_group,
        inputs.subject_growth,
        ledger,
    )
    triangulation = c_anchors.triangulate(
        data, config, c1, c2, inputs.trailing_pe, ledger
    )
    evaluation.triangulation = triangulation

    if triangulation.mode is AnchorMode.NONE:
        # C5: both uncomputable is an automatic FAIL.  Module B alone is not
        # grounds to proceed.
        evaluation.stopped_at = "C5"
        evaluation.stop_reason = (
            "both anchors uncomputable — automatic FAIL; no valuation is possible"
        )
        return evaluation

    price = data.current_price
    if price is None:
        evaluation.stopped_at = "C"
        evaluation.stop_reason = "no current price; the discount cannot be computed"
        return evaluation

    # -- Module D ---------------------------------------------------------
    single = triangulation.mode.is_single
    gate_threshold = config.buy_threshold(classification, single)
    try:
        actual_discount = fair_value.discount_to(price)
    except MethodRefused as exc:
        evaluation.stopped_at = "D"
        evaluation.stop_reason = str(exc)
        return evaluation

    conviction = d_conviction.score_conviction(
        data,
        market,
        config,
        classification,
        health,
        triangulation,
        actual_discount,
        gate_threshold,
        fair_value.discount_rate,
        momentum_returns,
        ledger,
    )
    evaluation.conviction = conviction

    # -- Module E ---------------------------------------------------------
    signal = e_triggers.evaluate_buy(
        data.profile.symbol,
        classification,
        price,
        fair_value,
        triangulation,
        conviction,
        config,
        data.profile.next_earnings_date,
        as_of,
        ledger,
    )
    evaluation.signal = signal

    # -- Modules J and K (reporting, not gating) --------------------------
    evaluation.yield_assessment = j_tax.assess_yield(
        data.dividend_yield, data.profile.exchange, classification, config
    )
    evaluation.adr = k_currency.adr_exposure(data.profile)
    try:
        evaluation.dual_currency = k_currency.dual_report(
            data.profile, price, fair_value.fair_value_per_share, fx, config
        )
    except k_currency.FxRateUnavailable as exc:
        ledger.note(f"K · {exc}")

    # -- Module F ---------------------------------------------------------
    if signal.signal is SignalType.BUY:
        evaluation.sizing = f_sizing.size_position(
            data.profile.symbol,
            classification,
            conviction.total,
            data.profile.sector or "UNCLASSIFIED",
            portfolio,
            config,
            single_anchor=single,
            ledger=ledger,
        )

    return evaluation


def run_screen(
    adapter: DataAdapter,
    symbols: Sequence[str],
    market: MarketData,
    config: Config,
    inputs_by_symbol: dict[str, CandidateInputs],
    portfolio: PortfolioState,
    fx: k_currency.FxTable,
    as_of: date | None = None,
) -> list[Evaluation]:
    """Screen a universe.

    D5's momentum ranks among *current passers only*, so the screen runs in two
    passes: the first establishes who passes Module A and can be priced, the
    second scores conviction against that peer set.  Ranking against the whole
    universe would be a different statistic than the spec asks for.
    """
    as_of = as_of or date.today()

    loaded: dict[str, CompanyData] = {}
    for symbol in symbols:
        try:
            loaded[symbol] = adapter.load_company(symbol)
        except DataUnavailable:
            continue

    # First pass: who clears Module A?
    passers: list[str] = []
    for symbol, data in loaded.items():
        probe_ledger = AuditLedger(symbol, as_of)
        health = a_health.run_module_a(data, market, config, probe_ledger, as_of)
        if health.passed:
            passers.append(symbol)

    momentum = {
        s: d_conviction.total_return_12_1(loaded[s]) for s in passers
    }

    # Second pass: full evaluation, with momentum ranked among passers only.
    results: list[Evaluation] = []
    for symbol, data in loaded.items():
        results.append(
            evaluate_candidate(
                data,
                market,
                config,
                inputs_by_symbol.get(symbol, CandidateInputs()),
                portfolio,
                fx,
                adapter=adapter,
                momentum_returns=momentum,
                as_of=as_of,
            )
        )
    return results


def sleeve_passer_counts(evaluations: Sequence[Evaluation]) -> dict[Regime, int]:
    """F3 input: how many passers each sleeve produced this cycle."""
    counts = {Regime.CORE: 0, Regime.GROWTH: 0}
    for ev in evaluations:
        if ev.is_buy and ev.classification is not None:
            counts[regime_of(ev.classification)] += 1
    return counts


__all__ = [
    "CandidateInputs",
    "Evaluation",
    "evaluate_candidate",
    "run_screen",
    "sleeve_passer_counts",
]
