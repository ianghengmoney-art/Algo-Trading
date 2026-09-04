"""GCFP v4 parameters — the single place any tunable number lives.

Prime Directive 8: parameters change only at scheduled annual review, never
mid-drawdown, never to make a specific name pass.  Keeping every number here
means a parameter change is a visible diff against one file rather than a
quiet edit buried in gate logic.  ``Config.fingerprint`` is written into every
report so a stored recommendation can be tied back to the parameters that
produced it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Mapping

from .classification import Classification


@dataclass(frozen=True)
class UniverseConfig:
    """Module A universe screen."""

    min_market_cap_usd: float = 300_000_000.0
    min_adv_usd: float = 2_000_000.0
    # Growth-routed names need deeper liquidity before we size into them.
    min_adv_usd_growth: float = 5_000_000.0


@dataclass(frozen=True)
class HealthConfig:
    """Module A gate thresholds."""

    # A1
    min_current_ratio: float = 1.0
    # A2
    leverage_median_multiple: float = 1.5
    min_peers_for_subindustry_median: int = 8
    # A3
    min_ocf_to_net_income: float = 0.80
    min_runway_months: float = 24.0
    # A4
    restatement_lookback_years: int = 3
    auditor_change_lookback_months: int = 12
    max_share_count_growth: float = 0.15
    max_filing_delay_days: int = 15
    # A5
    max_data_age_months: int = 12
    max_data_age_months_growth: int = 6


@dataclass(frozen=True)
class DiscountRateConfig:
    """Module B1 discount-rate build, fully pinned (fixes v3 flaw 4)."""

    equity_risk_premium: float = 0.050
    erp_sweep_low: float = 0.045
    erp_sweep_high: float = 0.055
    absolute_floor: float = 0.090
    core_growth_floor: float = 0.100
    spec_growth_floor: float = 0.120
    # Applied to interest expense / average total debt.
    default_tax_rate: float = 0.21


@dataclass(frozen=True)
class ValuationConfig:
    """Module B."""

    terminal_growth_cap: float = 0.025
    # B1
    b1_projection_years: int = 10
    b1_stage_one_years: int = 5
    terminal_value_share_flag: float = 0.75
    # B2
    b2_phase1_years: int = 3
    b2_phase2_end_year: int = 8
    b2_growth_cap_multiple: float = 1.5
    scenario_weights: Mapping[str, float] = field(
        default_factory=lambda: {"bear": 0.30, "base": 0.50, "bull": 0.20}
    )
    # B2.1 TAM ceiling
    tam_share_ceiling: float = 0.25
    tam_source_disagreement_flag: float = 0.50
    tam_min_sources: int = 2
    tam_proxy_years: int = 10

    def __post_init__(self) -> None:
        total = sum(self.scenario_weights.values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"scenario weights must sum to 1.0, got {total}")


@dataclass(frozen=True)
class AnchorConfig:
    """Module C."""

    history_window_years: int = 7
    reduced_confidence_window_years: int = 5
    min_history_years: int = 3
    # C1.1 — an acquisition above this share of market cap is transformative.
    transformative_event_market_cap_share: float = 0.25
    # C2
    peer_min: int = 4
    peer_max: int = 8
    peer_market_cap_low: float = 0.3
    peer_market_cap_high: float = 3.0
    peer_growth_band: float = 0.10
    # C3
    pegy_flag_above: float = 2.0
    # C4
    divergence_flag: float = 0.30
    #: C1's anchor multiple for CORE-GROWTH.  The spec's default is forward
    #: P/E, which needs analyst estimates.  On a source without them, set this
    #: to "trailing_pe" — an explicit, logged substitution rather than a
    #: forward multiple that quietly degrades into a trailing one.
    core_growth_multiple: str = "forward_pe"
    #: Whether the data source supplies forward EPS estimates at all.  When
    #: False, C3 reports "PEGY n/a" and the substitution above is logged.
    forward_estimates_available: bool = True
    # C5
    single_anchor_threshold_penalty: float = 0.10
    single_anchor_conviction_cap: float = 70.0
    single_anchor_portfolio_cap: float = 0.20
    single_anchor_conservatism_multiplier: float = 0.5


@dataclass(frozen=True)
class ConvictionConfig:
    """Module D point scales."""

    health_margin_max: float = 25.0
    valuation_excess_max: float = 30.0
    anchor_conservatism_max: float = 15.0
    business_quality_max: float = 20.0
    momentum_max: float = 10.0

    # D2: full marks at gate + this many percentage points of extra discount.
    valuation_excess_denominator: float = 0.25
    # D3: full marks when the conservative anchor implies this discount.
    anchor_conservatism_denominator: float = 0.40
    # D4
    roic_spread_full_marks: float = 0.10
    # D5
    momentum_min_passers: int = 3
    momentum_neutral_score: float = 5.0

    high_conviction: float = 80.0
    standard_conviction: float = 60.0
    marginal_conviction: float = 40.0


@dataclass(frozen=True)
class TriggerConfig:
    """Module E."""

    buy_discount: Mapping[Classification, float] = field(
        default_factory=lambda: {
            Classification.CORE_STABLE: 0.25,
            Classification.FINANCIAL_BANK: 0.25,
            Classification.REIT: 0.25,
            Classification.INSURER: 0.25,
            Classification.CORE_GROWTH: 0.35,
            Classification.SPEC_GROWTH: 0.40,
        }
    )
    sell_premium: Mapping[Classification, float] = field(
        default_factory=lambda: {
            Classification.CORE_STABLE: 0.20,
            Classification.FINANCIAL_BANK: 0.20,
            Classification.REIT: 0.20,
            Classification.INSURER: 0.20,
            Classification.CORE_GROWTH: 0.30,
            Classification.SPEC_GROWTH: 0.30,
        }
    )
    min_conviction_to_buy: float = 60.0
    earnings_blackout_trading_days: int = 10


@dataclass(frozen=True)
class SizingConfig:
    """Module F."""

    # CORE regime, as a share of the core sleeve.
    core_high_conviction_size: float = 0.08
    core_standard_size: float = 0.04
    core_name_cap: float = 0.10
    core_sector_cap: float = 0.30
    core_target_names: tuple[int, int] = (8, 15)

    # GROWTH regime, as a share of the growth sleeve.
    growth_high_conviction_size: float = 0.15
    growth_standard_size: float = 0.10
    growth_name_cap: float = 0.20
    growth_sector_cap: float = 0.35
    growth_target_names: tuple[int, int] = (8, 12)
    growth_sleeve_cap_of_portfolio: float = 0.15

    # F2 buckets, as a share of the total portfolio.
    ballast_target: tuple[float, float] = (0.45, 0.55)
    core_picks_target: tuple[float, float] = (0.30, 0.40)
    growth_picks_target: tuple[float, float] = (0.00, 0.15)

    # F3
    min_passers_per_sleeve: int = 6


@dataclass(frozen=True)
class ExecutionConfig:
    """Module G."""

    size_deviation_threshold: float = 0.25


@dataclass(frozen=True)
class MonitorConfig:
    """Module H."""

    sell_conviction_floor: float = 40.0
    review_conviction_floor: float = 50.0
    review_conviction_drop: float = 20.0
    semiannual_midpoint_move_flag: float = 0.25
    growth_revenue_growth_floor: float = 0.10
    growth_rule_of_40_floor: float = 20.0
    growth_min_runway_months: float = 12.0
    growth_consecutive_reports_to_sell: int = 2


@dataclass(frozen=True)
class ExpectationConfig:
    """Module I."""

    min_evaluation_years: int = 5
    parameter_change_cooling_off_days: int = 30
    sleeve_cap_increase_cooling_off_days: int = 90
    min_closed_positions_for_outcome_break: int = 20
    classification_accuracy_floor: float = 0.70
    peer_gameability_spread: float = 0.30
    single_anchor_rate_break: float = 0.40
    divergence_rate_break: float = 0.50


@dataclass(frozen=True)
class TaxConfig:
    """Module J — operator-configurable, defaults to a Singapore resident."""

    jurisdiction: str = "SG"
    capital_gains_rate: float = 0.0
    us_dividend_withholding: float = 0.30
    sgx_reit_distribution_exempt: bool = True
    dividend_thesis_flag_yield: float = 0.04


@dataclass(frozen=True)
class CurrencyConfig:
    """Module K."""

    base_currency: str = "SGD"


@dataclass(frozen=True)
class Config:
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    discount_rate: DiscountRateConfig = field(default_factory=DiscountRateConfig)
    valuation: ValuationConfig = field(default_factory=ValuationConfig)
    anchors: AnchorConfig = field(default_factory=AnchorConfig)
    conviction: ConvictionConfig = field(default_factory=ConvictionConfig)
    triggers: TriggerConfig = field(default_factory=TriggerConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    expectations: ExpectationConfig = field(default_factory=ExpectationConfig)
    tax: TaxConfig = field(default_factory=TaxConfig)
    currency: CurrencyConfig = field(default_factory=CurrencyConfig)

    def buy_threshold(self, classification: Classification, single_anchor: bool) -> float:
        """Module E gate X, including C5's SINGLE-ANCHOR MODE penalty."""
        base = self.triggers.buy_discount[classification]
        if single_anchor:
            base += self.anchors.single_anchor_threshold_penalty
        return base

    def sell_threshold(self, classification: Classification) -> float:
        """Module E gate Y."""
        return self.triggers.sell_premium[classification]

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @property
    def fingerprint(self) -> str:
        """Stable hash of every parameter, stamped onto reports and stored rows."""
        blob = json.dumps(self.to_dict(), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _plain(value: Any) -> Any:
    """asdict() cannot walk Enum-keyed mappings; this can."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {
            (k.value if isinstance(k, Classification) else str(k)): _plain(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, Classification):
        return value.value
    return value


DEFAULT_CONFIG = Config()

__all__ = [
    "Config",
    "DEFAULT_CONFIG",
    "UniverseConfig",
    "HealthConfig",
    "DiscountRateConfig",
    "ValuationConfig",
    "AnchorConfig",
    "ConvictionConfig",
    "TriggerConfig",
    "SizingConfig",
    "ExecutionConfig",
    "MonitorConfig",
    "ExpectationConfig",
    "TaxConfig",
    "CurrencyConfig",
]
