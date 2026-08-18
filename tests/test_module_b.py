"""Module B -- method matches business type, or the code refuses."""

from __future__ import annotations

import pytest

from gcfp.config import CORE_GROWTH, CORE_STABLE, FINANCIAL_BANK, REIT, SPEC_GROWTH
from gcfp.modules.a_health import build_trailing_window
from gcfp.modules.b_valuation import (
    MethodMismatch, ValuationImpossible, value_bank, value_core_stable,
    value_growth, value_insurer, value_reit,
)


def _window(adapter, symbol):
    return adapter.load_candidate(symbol), build_trailing_window(adapter.load_candidate(symbol))


def test_dcf_is_banned_for_banks(adapter, params):
    candidate, window = _window(adapter, "BANKCO")
    with pytest.raises(MethodMismatch):
        value_core_stable(candidate, window, params, classification=FINANCIAL_BANK)


def test_scenario_dcf_refuses_non_growth_classifications(adapter, params):
    candidate, window = _window(adapter, "REITCO")
    with pytest.raises(MethodMismatch):
        value_growth(candidate, window, params, REIT)


def test_reit_method_refuses_other_classifications(adapter, params):
    candidate, window = _window(adapter, "MATURE")
    with pytest.raises(MethodMismatch):
        value_reit(candidate, window, params, 17.0, classification=CORE_STABLE)


def test_core_stable_dcf_refuses_negative_free_cash_flow(adapter, params):
    """A single-stage FCF DCF on a cash-burning company is a modelling error."""
    candidate, window = _window(adapter, "BURNER")
    with pytest.raises(ValuationImpossible):
        value_core_stable(candidate, window, params)


def test_terminal_growth_is_capped_and_discount_floored(adapter, params):
    candidate, window = _window(adapter, "MATURE")
    result = value_core_stable(candidate, window, params)
    assert result.diagnostics["terminal_growth_pct"] <= params.valuation.b1_max_terminal_growth_pct
    assert result.diagnostics["discount_rate_pct"] >= params.valuation.b1_min_discount_rate_pct


def test_b1_prints_terminal_value_share_and_half_growth_rerun(adapter, params):
    candidate, window = _window(adapter, "MATURE")
    result = value_core_stable(candidate, window, params)
    assert result.diagnostics["terminal_value_share_pct"] is not None
    assert result.diagnostics["half_growth_fair_value_per_share"] is not None
    assert result.diagnostics["half_growth_verdict_survives"] in (True, False)


def test_b1_flags_a_model_that_is_mostly_perpetuity(adapter, params):
    candidate, window = _window(adapter, "MATURE")
    result = value_core_stable(candidate, window, params)
    share = result.diagnostics["terminal_value_share_pct"]
    flagged = any("TERMINAL VALUE" in w for w in result.warnings)
    assert flagged == (share > params.valuation.b1_terminal_value_share_flag_pct)


def test_b2_weights_are_thirty_fifty_twenty_and_bear_models_failure(adapter, params):
    candidate, window = _window(adapter, "FASTPROF")
    result = value_growth(candidate, window, params, CORE_GROWTH)
    scenarios = result.diagnostics["scenarios"]
    assert scenarios["bear"]["weight"] == 0.30
    assert scenarios["base"]["weight"] == 0.50
    assert scenarios["bull"]["weight"] == 0.20
    # The bear case must model the thesis failing, not merely slowing.
    assert scenarios["bear"]["phase1_growth_pct"] < 0.5 * scenarios["base"]["phase1_growth_pct"]
    assert result.scenario_values["bear"] < result.scenario_values["base"]


def test_b2_discount_floor_is_higher_for_spec_growth(adapter, params):
    _, core_window = _window(adapter, "FASTPROF")
    core = value_growth(adapter.load_candidate("FASTPROF"), core_window, params, CORE_GROWTH)
    _, spec_window = _window(adapter, "BURNER")
    spec = value_growth(adapter.load_candidate("BURNER"), spec_window, params, SPEC_GROWTH)
    assert core.diagnostics["discount_rate_floor_pct"] == params.valuation.b2_min_discount_rate_core_growth_pct
    assert spec.diagnostics["discount_rate_floor_pct"] == params.valuation.b2_min_discount_rate_spec_growth_pct


def test_b2_phase1_growth_never_exceeds_the_cap(adapter, params):
    candidate, window = _window(adapter, "FASTPROF")
    result = value_growth(candidate, window, params, CORE_GROWTH)
    assert result.diagnostics["phase1_growth_pct"] <= result.diagnostics["phase1_growth_cap_pct"] + 1e-9


def test_b2_warns_when_no_tam_ceiling_can_be_tested(adapter, params):
    candidate, window = _window(adapter, "FASTPROF")
    result = value_growth(candidate, window, params, CORE_GROWTH)
    assert any("TAM CEILING TEST NOT RUN" in w for w in result.warnings)


def test_b2_flags_a_breached_tam_ceiling(adapter, params):
    candidate, window = _window(adapter, "FASTPROF")
    tiny_market = 1_000_000_000.0
    result = value_growth(candidate, window, params, CORE_GROWTH, tam_usd=tiny_market)
    assert result.diagnostics["implied_year10_market_share_pct"] > params.valuation.b2_tam_ceiling_share_pct
    assert any("TAM CEILING BREACHED" in w for w in result.warnings)


def test_b3_reports_roe_against_the_banks_own_history(adapter, params):
    candidate, window = _window(adapter, "BANKCO")
    result = value_bank(candidate, window, params, peer_median_p_tbv=1.4)
    assert result.diagnostics["dcf_banned"] is True
    assert result.diagnostics["long_run_roe_pct"] is not None


def test_b4_uses_affo_and_never_pe(adapter, params):
    candidate, window = _window(adapter, "REITCO")
    result = value_reit(candidate, window, params, peer_median_p_affo=17.0)
    assert result.diagnostics["pe_banned"] is True
    assert result.diagnostics["basis"] == "AFFO"
    assert "P/AFFO" in result.method


def test_b4_falls_back_to_ffo_and_says_so(adapter, params):
    import dataclasses

    candidate = adapter.load_candidate("REITCO")
    stripped = [dataclasses.replace(q, recurring_capex=None) for q in candidate.quarterly]
    candidate = dataclasses.replace(candidate, quarterly=stripped)
    window = build_trailing_window(candidate)
    result = value_reit(candidate, window, params, peer_median_p_affo=17.0)
    assert result.diagnostics["basis"] == "FFO"
    assert any("AFFO NOT COMPUTABLE" in w for w in result.warnings)


def test_b5_reads_combined_ratio_and_strips_unrealised_gains(adapter, params):
    candidate, window = _window(adapter, "INSURCO")
    result = value_insurer(candidate, window, params, peer_median_p_b=1.7)
    assert result.diagnostics["combined_ratio_pct"] is not None
    assert result.diagnostics["operating_roe_ex_unrealised_pct"] is not None


def test_fair_value_is_labelled_with_method_and_classification(adapter, params):
    candidate, window = _window(adapter, "MATURE")
    result = value_core_stable(candidate, window, params)
    assert "B1" in result.label and CORE_STABLE in result.label
