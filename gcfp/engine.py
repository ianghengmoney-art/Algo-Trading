"""Pipeline orchestration.

confirm the business is healthy -> classify what kind of business it is ->
value it by the method that fits that kind -> cross-check that value against
two independent anchors -> score conviction -> size by regime -> monitor on
fundamentals -> never execute automatically.

Nothing in this module, or anywhere below it, can place an order. There is no
broker client, no execution hook and no trading library in the dependency tree,
by design and by test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Sequence

from .config import Params
from .data.base import CapabilityGate, DataAdapter, DataUnavailable
from .modules import b_valuation
from .modules.a_health import (
    HealthResult,
    TrailingWindow,
    build_trailing_window,
    evaluate_health,
    sorted_annuals,
)
from .modules.b_valuation import (
    MethodMismatch,
    ValuationImpossible,
    ValuationResult,
    compute_affo,
    compute_ffo,
)
from .modules.c_triangulation import (
    MULTIPLE_FOR_CLASSIFICATION,
    PeerCandidate,
    TriangulationResult,
    own_history_anchor,
    peer_anchor,
    select_peers,
    triangulate,
)
from .modules.d_conviction import (
    ConvictionScore,
    apply_momentum_ranking,
    score_conviction,
    total_return_12_1,
)
from .modules.e_triggers import TriggerResult, evaluate_buy
from .modules.j_tax import TaxTreatment, assess as assess_tax
from .types import CandidateData


@dataclass
class CandidateAssessment:
    symbol: str
    candidate: Optional[CandidateData] = None
    health: Optional[HealthResult] = None
    classification: Optional[str] = None
    valuation: Optional[ValuationResult] = None
    triangulation: Optional[TriangulationResult] = None
    conviction: Optional[ConvictionScore] = None
    trigger: Optional[TriggerResult] = None
    tax: Optional[TaxTreatment] = None
    stopped_at: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    @property
    def passed_health(self) -> bool:
        return self.health is not None and self.health.passed

    @property
    def is_buy(self) -> bool:
        return self.trigger is not None and self.trigger.is_buy

    @property
    def fair_value(self) -> Optional[float]:
        return self.valuation.fair_value_per_share if self.valuation else None

    def all_warnings(self) -> list[str]:
        out: list[str] = list(self.notes)
        if self.valuation:
            out += self.valuation.warnings
        if self.triangulation:
            out += self.triangulation.warnings
        return out


# ---------------------------------------------------------------------------
# current-multiple computation
# ---------------------------------------------------------------------------


def current_multiple(
    candidate: CandidateData, classification: str, window: Optional[TrailingWindow]
) -> Optional[float]:
    """Today's classification-appropriate multiple.

    Returns ``None`` where the multiple is undefined -- notably a P/E on
    negative earnings, which is a refusal rather than a very large number.
    """
    if window is None:
        return None
    profile = candidate.profile
    price = profile.price
    shares = window.latest("shares_diluted") or profile.shares_outstanding
    if price is None or not shares:
        return None

    name = MULTIPLE_FOR_CLASSIFICATION.get(classification)

    if name == "trailing P/E":
        net_income = window.total("net_income")
        if net_income is None or net_income <= 0:
            return None
        return price / (net_income / shares)

    if name == "forward P/E":
        forward_eps = candidate.estimates.forward_eps
        if forward_eps and forward_eps > 0:
            return price / forward_eps
        net_income = window.total("net_income")
        if net_income is None or net_income <= 0:
            return None
        return price / (net_income / shares)

    if name == "P/B":
        equity = window.latest("total_equity")
        if equity is None or equity <= 0:
            return None
        return price / (equity / shares)

    if name == "EV/Revenue":
        revenue = window.total("revenue")
        if revenue is None or revenue <= 0 or profile.market_cap is None:
            return None
        net_debt = window.periods[0].net_debt if window.periods else 0.0
        return (profile.market_cap + (net_debt or 0.0)) / revenue

    if name == "P/AFFO":
        parts = [compute_affo(p) for p in window.periods]
        if any(v is None for v in parts):
            parts = [compute_ffo(p) for p in window.periods]
        if not parts or any(v is None for v in parts):
            return None
        per_share = sum(parts) / shares
        if per_share <= 0:
            return None
        return price / per_share

    return None


def revenue_growth_pct(candidate: CandidateData) -> Optional[float]:
    annuals = sorted_annuals(candidate)
    if len(annuals) < 2:
        return None
    latest, prior = annuals[0].revenue, annuals[1].revenue
    if latest is None or prior is None or prior <= 0:
        return None
    return 100.0 * (latest / prior - 1.0)


def build_peer_candidate(
    adapter: DataAdapter, symbol: str, classification: str
) -> Optional[PeerCandidate]:
    try:
        peer_data = adapter.load_candidate(symbol)
    except DataUnavailable:
        return None
    window = build_trailing_window(peer_data)
    return PeerCandidate(
        symbol=symbol,
        sector=peer_data.profile.sector,
        market_cap=peer_data.profile.market_cap,
        revenue_growth_pct=revenue_growth_pct(peer_data),
        multiple=current_multiple(peer_data, classification, window),
    )


# ---------------------------------------------------------------------------
# single-candidate pipeline
# ---------------------------------------------------------------------------


def assess_candidate(
    adapter: DataAdapter,
    symbol: str,
    params: Params,
    capability_gate: CapabilityGate,
    as_of: Optional[date] = None,
    tam_usd: Optional[float] = None,
    sector_cap_rate_pct: Optional[float] = None,
) -> CandidateAssessment:
    """Run Modules A -> E for one company."""
    assessment = CandidateAssessment(symbol=symbol)

    try:
        candidate = adapter.load_candidate(symbol, as_of=as_of)
    except DataUnavailable as exc:
        assessment.stopped_at = "load"
        assessment.notes.append(str(exc))
        return assessment

    assessment.candidate = candidate
    as_of = as_of or candidate.as_of or date.today()

    # Module A. Health before value: nothing below runs without a PASS.
    health = evaluate_health(candidate, params, as_of=as_of, capability_gate=capability_gate)
    assessment.health = health
    if not health.passed or not health.classification:
        assessment.stopped_at = "A"
        return assessment

    classification = health.classification
    assessment.classification = classification
    window = build_trailing_window(candidate)

    # Module C2 first: the relative methods in B3/B4/B5 need the peer median,
    # and building it once keeps the peer set identical across both modules.
    subject = PeerCandidate(
        symbol=symbol,
        sector=candidate.profile.sector,
        market_cap=candidate.profile.market_cap,
        revenue_growth_pct=revenue_growth_pct(candidate),
        multiple=current_multiple(candidate, classification, window),
    )
    peer_pool = [
        p
        for p in (build_peer_candidate(adapter, s, classification) for s in candidate.peer_symbols)
        if p is not None
    ]
    selection = select_peers(subject, peer_pool, params)
    peer_reading = peer_anchor(
        subject, selection, params, MULTIPLE_FOR_CLASSIFICATION.get(classification, "multiple")
    )
    peer_median = peer_reading.detail.get("peer_median")

    # Module B.
    try:
        valuation = b_valuation.value_candidate(
            candidate,
            window,
            params,
            classification,
            peer_median_multiple=peer_median,
            tam_usd=tam_usd,
            sector_cap_rate_pct=sector_cap_rate_pct,
        )
    except (MethodMismatch, ValuationImpossible) as exc:
        assessment.stopped_at = "B"
        assessment.notes.append(str(exc))
        return assessment
    assessment.valuation = valuation

    # Module C1.
    if capability_gate.own_history_anchor_enabled:
        try:
            series = adapter.get_multiple_series(
                symbol,
                MULTIPLE_FOR_CLASSIFICATION.get(classification, "multiple"),
                years=max(10, params.triangulation.c1_min_window_years),
            )
        except DataUnavailable as exc:
            series = None
            assessment.notes.append(str(exc))
    else:
        series = None
        assessment.notes.append(
            "Module C1 disabled by the data-capability gate -- running single-anchor."
        )

    own_reading = own_history_anchor(series, subject.multiple, params, classification)

    trailing_pe = None
    if window is not None:
        net_income = window.total("net_income")
        shares = window.latest("shares_diluted") or candidate.profile.shares_outstanding
        if net_income and shares and net_income > 0 and candidate.profile.price:
            trailing_pe = candidate.profile.price / (net_income / shares)

    triangulation = triangulate(
        symbol,
        classification,
        own_reading,
        peer_reading,
        selection,
        params,
        trailing_pe=trailing_pe,
        forward_eps_growth_pct=candidate.estimates.forward_eps_growth_pct,
        dividend_yield_pct=candidate.estimates.dividend_yield_pct,
    )
    assessment.triangulation = triangulation

    # Module D.
    discount_rate = valuation.diagnostics.get("discount_rate_pct")
    conviction = score_conviction(
        candidate, health, valuation.fair_value_per_share, triangulation, params, discount_rate
    )
    assessment.conviction = conviction

    # Module E.
    assessment.trigger = evaluate_buy(
        symbol,
        classification,
        candidate.profile.price,
        valuation.fair_value_per_share,
        triangulation,
        conviction,
        params,
        as_of=as_of,
        next_earnings=candidate.estimates.next_earnings_date,
    )

    # Module J.
    assessment.tax = assess_tax(
        symbol,
        candidate.profile.exchange,
        candidate.profile.country,
        candidate.estimates.dividend_yield_pct,
        params,
        classification=classification,
    )
    return assessment


# ---------------------------------------------------------------------------
# screen
# ---------------------------------------------------------------------------


@dataclass
class ScreenResult:
    as_of: date
    adapter_name: str
    assessments: list[CandidateAssessment] = field(default_factory=list)
    header_notices: list[str] = field(default_factory=list)

    def passers(self) -> list[CandidateAssessment]:
        return [a for a in self.assessments if a.is_buy]

    def qualified_but_deferred(self) -> list[CandidateAssessment]:
        return [a for a in self.assessments if a.trigger and a.trigger.deferred]

    def health_failures(self) -> list[CandidateAssessment]:
        return [a for a in self.assessments if a.stopped_at == "A"]


def screen(
    adapter: DataAdapter,
    symbols: Sequence[str],
    params: Params,
    capability_gate: Optional[CapabilityGate] = None,
    as_of: Optional[date] = None,
) -> ScreenResult:
    """Run the pipeline across a universe and rank the survivors.

    Momentum is applied only here, and only across names that already cleared
    every gate -- it can order the survivors, never admit one.
    """
    from .data.registry import capability_gate as build_gate

    gate = capability_gate or build_gate(adapter, symbols=symbols)
    as_of = as_of or date.today()

    result = ScreenResult(as_of=as_of, adapter_name=adapter.name)
    result.header_notices.extend(gate.blocking_notices())

    for symbol in symbols:
        result.assessments.append(
            assess_candidate(adapter, symbol, params, gate, as_of=as_of)
        )

    scored = [a for a in result.assessments if a.conviction is not None]
    returns = {
        a.symbol: total_return_12_1(a.candidate) if a.candidate else None for a in scored
    }
    apply_momentum_ranking([a.conviction for a in scored], returns, params)

    # The buy trigger reads the conviction total, so it is re-evaluated once
    # momentum has been ranked across the surviving set.
    for a in scored:
        if a.classification and a.valuation and a.candidate:
            a.trigger = evaluate_buy(
                a.symbol,
                a.classification,
                a.candidate.profile.price,
                a.valuation.fair_value_per_share,
                a.triangulation,
                a.conviction,
                params,
                as_of=as_of,
                next_earnings=a.candidate.estimates.next_earnings_date,
            )

    return result
