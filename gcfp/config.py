"""GCFP v3 parameters.

Single source of truth for every threshold in the system. Prime Directive 8:
parameter changes only at scheduled annual review -- never mid-drawdown, never
after a single result, never to make a specific name pass.

Every field carries the module that owns it in its name so that a diff to this
file is self-evidently a strategy change and not a refactor. ``Params`` is
frozen; the only supported way to change a value in a running deployment is a
new dated revision recorded through ``gcfp.modules.i_expectations``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping

# Classification tags. Defined here rather than in types.py because several
# parameter tables are keyed by them.
CORE_STABLE = "CORE-STABLE"
CORE_GROWTH = "CORE-GROWTH"
SPEC_GROWTH = "SPEC-GROWTH"
FINANCIAL_BANK = "FINANCIAL-BANK"
REIT = "REIT"
INSURER = "INSURER"

ALL_CLASSIFICATIONS = (
    CORE_STABLE,
    CORE_GROWTH,
    SPEC_GROWTH,
    FINANCIAL_BANK,
    REIT,
    INSURER,
)

# Module F1: which sleeve a classification is sized under. Prime Directive 7 --
# these two regimes never blend.
CORE_SLEEVE = "CORE"
GROWTH_SLEEVE = "GROWTH"

SLEEVE_OF: Mapping[str, str] = {
    CORE_STABLE: CORE_SLEEVE,
    FINANCIAL_BANK: CORE_SLEEVE,
    REIT: CORE_SLEEVE,
    INSURER: CORE_SLEEVE,
    CORE_GROWTH: GROWTH_SLEEVE,
    SPEC_GROWTH: GROWTH_SLEEVE,
}


@dataclass(frozen=True)
class UniverseParams:
    """Module A preamble."""

    min_market_cap_usd: float = 300_000_000.0
    # 3-month average daily dollar volume.
    min_adv_usd: float = 2_000_000.0
    # Growth names gap; thin liquidity plus gap risk turns a 30% paper loss
    # into a 50% realised one.
    min_adv_usd_growth: float = 5_000_000.0
    require_us_listed_common: bool = True


@dataclass(frozen=True)
class HealthParams:
    """Module A gates A1-A5."""

    # A1 -- solvency.
    min_current_ratio: float = 1.0
    # A2 -- leverage, sector-relative.
    net_debt_ebitda_sector_multiple: float = 1.5
    min_sector_peers_for_median: int = 4
    # A3 -- earnings quality.
    min_ocf_to_net_income: float = 0.80
    min_cash_runway_months: float = 24.0
    # A4 -- red flags.
    restatement_lookback_years: int = 3
    auditor_change_lookback_months: int = 12
    max_share_count_growth_pct_per_year: float = 15.0
    share_count_growth_window_years: int = 2
    # A5 -- data integrity.
    max_data_age_months: float = 12.0
    max_data_age_months_growth: float = 6.0


@dataclass(frozen=True)
class ClassificationParams:
    """Module A6 router thresholds."""

    core_stable_min_profitable_years: int = 3
    core_stable_max_revenue_growth_pct: float = 20.0
    core_stable_min_positive_fcf_years: int = 4
    core_stable_fcf_window_years: int = 5

    core_growth_min_revenue_growth_pct: float = 20.0
    core_growth_min_2y_cagr_pct: float = 15.0
    core_growth_min_rule_of_40: float = 40.0

    spec_growth_min_revenue_usd: float = 50_000_000.0


@dataclass(frozen=True)
class ValuationParams:
    """Module B."""

    # B1 -- CORE-STABLE two-stage DCF.
    b1_stage1_years: int = 5
    b1_fade_years: int = 5
    b1_max_terminal_growth_pct: float = 2.5
    b1_min_discount_rate_pct: float = 9.0
    b1_terminal_value_share_flag_pct: float = 75.0
    b1_sensitivity_growth_multiplier: float = 0.5

    # B2 -- three-phase scenario DCF.
    b2_phase1_years: int = 3
    b2_phase2_years: int = 5
    b2_phase1_growth_cap_multiple: float = 1.5
    b2_max_terminal_growth_pct: float = 2.5
    b2_min_discount_rate_core_growth_pct: float = 10.0
    b2_min_discount_rate_spec_growth_pct: float = 12.0
    b2_scenario_weights: Mapping[str, float] = field(
        default_factory=lambda: {"bear": 0.30, "base": 0.50, "bull": 0.20}
    )
    b2_tam_ceiling_share_pct: float = 25.0
    b2_tam_horizon_years: int = 10

    # Equity risk premium and risk-free rate for CAPM. Operator-set at annual
    # review; deliberately not fetched live so that a data outage cannot move
    # the discount rate underneath a verdict.
    risk_free_rate_pct: float = 4.0
    equity_risk_premium_pct: float = 5.0


@dataclass(frozen=True)
class TriangulationParams:
    """Module C."""

    c1_min_window_years: int = 7
    c1_reduced_confidence_window_years: int = 5
    # Step-change scan: a break is flagged when the two segments' means differ
    # by more than this many pooled standard deviations and both segments are
    # at least ``c1_min_segment_points`` long.
    c1_step_change_sigma: float = 1.0
    c1_min_segment_points: int = 6

    c2_min_peers: int = 4
    c2_max_peers: int = 8
    c2_market_cap_low_multiple: float = 0.3
    c2_market_cap_high_multiple: float = 3.0
    c2_revenue_growth_band_pp: float = 10.0

    c3_pegy_flag_above: float = 2.0

    c4_divergence_pct: float = 30.0


@dataclass(frozen=True)
class ConvictionParams:
    """Module D component maxima and interpretation bands."""

    health_margin_max: float = 25.0
    valuation_margin_max: float = 30.0
    anchor_agreement_max: float = 15.0
    business_quality_max: float = 20.0
    momentum_max: float = 10.0

    # Valuation margin scaling: 0 points at 0% discount, full points at this
    # discount or better.
    valuation_full_points_discount_pct: float = 50.0

    anchor_both_confirm_points: float = 15.0
    anchor_one_confirm_points: float = 5.0
    anchor_divergent_points: float = 0.0

    band_high_conviction: float = 80.0
    band_standard: float = 60.0
    band_marginal: float = 40.0

    # Module H review trigger.
    review_score_drop_points: float = 20.0


@dataclass(frozen=True)
class TriggerParams:
    """Module E."""

    buy_discount_pct: Mapping[str, float] = field(
        default_factory=lambda: {
            CORE_STABLE: 25.0,
            FINANCIAL_BANK: 25.0,
            REIT: 25.0,
            INSURER: 25.0,
            CORE_GROWTH: 35.0,
            SPEC_GROWTH: 40.0,
        }
    )
    sell_premium_pct: Mapping[str, float] = field(
        default_factory=lambda: {
            CORE_STABLE: 20.0,
            FINANCIAL_BANK: 20.0,
            REIT: 20.0,
            INSURER: 20.0,
            CORE_GROWTH: 30.0,
            SPEC_GROWTH: 30.0,
        }
    )
    min_conviction_to_buy: float = 60.0
    earnings_blackout_trading_days: int = 10


@dataclass(frozen=True)
class SizingParams:
    """Module F."""

    core_high_conviction_pct: float = 8.0
    core_standard_pct: float = 4.0
    core_hard_cap_pct: float = 10.0
    core_sector_cap_pct: float = 30.0
    core_target_names: tuple[int, int] = (8, 15)

    growth_high_conviction_pct: float = 15.0
    growth_standard_pct: float = 10.0
    growth_hard_cap_pct: float = 20.0
    growth_sector_cap_pct: float = 35.0
    growth_target_names: tuple[int, int] = (8, 12)

    # Operator-set, hard-enforced: growth sleeve as a share of total portfolio.
    growth_sleeve_max_pct_of_portfolio: float = 15.0

    # F2 -- index/cash/gold ballast target.
    ballast_target_low_pct: float = 45.0
    ballast_target_high_pct: float = 55.0

    # F3 -- below-minimum handling.
    min_passers_per_sleeve: int = 6


@dataclass(frozen=True)
class ExecutionParams:
    """Module G."""

    size_deviation_flag_pct: float = 25.0


@dataclass(frozen=True)
class MonitorParams:
    """Module H, GROWTH-only deterioration triggers."""

    growth_min_revenue_growth_pct: float = 10.0
    growth_min_rule_of_40: float = 20.0
    growth_consecutive_quarters: int = 2
    growth_min_cash_runway_months: float = 12.0


@dataclass(frozen=True)
class DisciplineParams:
    """Module I pre-commitments."""

    min_evaluation_horizon_years: int = 5
    parameter_change_cooling_off_days: int = 30
    sleeve_cap_increase_cooling_off_days: int = 90
    min_closed_positions_for_break_review: int = 20


@dataclass(frozen=True)
class TaxParams:
    """Module J. Default profile: Singapore-resident individual."""

    jurisdiction: str = "SG"
    capital_gains_tax_pct: float = 0.0
    us_dividend_withholding_pct: float = 30.0
    local_reit_distribution_tax_pct: float = 0.0
    # A candidate whose forward return case rests this heavily on dividend
    # yield is flagged as tax-inefficient for this operator profile.
    dividend_thesis_flag_yield_pct: float = 3.0


@dataclass(frozen=True)
class Params:
    """The complete parameter set."""

    revision: str = "2026-annual-review"
    universe: UniverseParams = field(default_factory=UniverseParams)
    health: HealthParams = field(default_factory=HealthParams)
    classification: ClassificationParams = field(default_factory=ClassificationParams)
    valuation: ValuationParams = field(default_factory=ValuationParams)
    triangulation: TriangulationParams = field(default_factory=TriangulationParams)
    conviction: ConvictionParams = field(default_factory=ConvictionParams)
    triggers: TriggerParams = field(default_factory=TriggerParams)
    sizing: SizingParams = field(default_factory=SizingParams)
    execution: ExecutionParams = field(default_factory=ExecutionParams)
    monitor: MonitorParams = field(default_factory=MonitorParams)
    discipline: DisciplineParams = field(default_factory=DisciplineParams)
    tax: TaxParams = field(default_factory=TaxParams)

    def with_revision(self, revision: str) -> "Params":
        """Return a copy stamped with a new annual-review revision label."""
        return replace(self, revision=revision)


DEFAULT_PARAMS = Params()
