"""Modules I and J, plus the Section 12 validation protocol."""

from __future__ import annotations

from datetime import date

import pytest

from gcfp.config import REIT
from gcfp.modules.i_expectations import (
    EXPECTATION_STATEMENT, NOT_STRATEGY_BREAK_CRITERIA, STRATEGY_BREAK_CRITERIA,
    DisciplineLedger, report_header,
)
from gcfp.modules.j_tax import assess, trim_tax_cost_note
from gcfp.validation.metrics import (
    PositionOutcome, by_classification, classification_accuracy_pct,
    max_drawdown_pct, summarise, time_to_recovery,
)
from gcfp.validation.protocol import BacktestCoverage, check
from gcfp.validation.sweep import sweep


def test_expectation_statement_is_printed_verbatim(params):
    header = report_header(params)
    assert EXPECTATION_STATEMENT in header


def test_header_carries_pre_registered_bands_and_revision(params):
    header = report_header(params)
    assert "PRE-REGISTERED PERFORMANCE BANDS" in header
    assert params.revision in header


def test_drawdown_and_duration_are_not_strategy_break_criteria():
    joined = " ".join(NOT_STRATEGY_BREAK_CRITERIA).lower()
    assert "drawdown depth" in joined
    assert "underperformance duration" in joined
    assert all("drawdown" not in c.lower() for c in STRATEGY_BREAK_CRITERIA)


def test_parameter_change_cools_off_for_thirty_days(params):
    ledger = DisciplineLedger()
    entry = ledger.request_change(date(2026, 6, 30), "lower the buy threshold", params)
    assert entry.required_days == params.discipline.parameter_change_cooling_off_days
    assert not entry.is_eligible(date(2026, 7, 20))
    assert entry.is_eligible(date(2026, 8, 1))


def test_sleeve_cap_increase_cools_off_for_ninety_days(params):
    ledger = DisciplineLedger()
    entry = ledger.request_change(
        date(2026, 6, 30), "raise the growth sleeve cap", params, sleeve_cap_increase=True
    )
    assert entry.required_days == params.discipline.sleeve_cap_increase_cooling_off_days
    assert not entry.is_eligible(date(2026, 9, 1))
    assert len(ledger.pending(date(2026, 7, 15))) == 1


def test_us_dividend_is_reported_after_withholding(params):
    treatment = assess("T", "NYSE", "US", 4.0, params)
    assert treatment.after_withholding_yield_pct == pytest.approx(2.8)
    assert treatment.withholding_pct == 30.0


def test_sgx_reit_distributions_are_exempt(params):
    treatment = assess("S", "SGX", "SG", 6.0, params, classification=REIT)
    assert treatment.after_withholding_yield_pct == pytest.approx(6.0)
    assert any("tax-exempt" in n for n in treatment.notes)


def test_high_yield_us_name_is_flagged_as_tax_inefficient(params):
    treatment = assess("H", "NYSE", "US", 5.5, params)
    assert treatment.dividend_thesis_flagged
    assert any("appreciation-oriented holdings are more tax-efficient" in n for n in treatment.notes)


def test_zero_capital_gains_means_trimming_is_costless(params):
    note = trim_tax_cost_note(params)
    assert "no tax cost" in note and "TRIM-TO-CAP" in note


def test_protocol_rejects_a_backtest_that_skips_the_windows_that_matter():
    verdict = check(
        BacktestCoverage(
            start=date(2010, 1, 1), end=date(2026, 1, 1),
            point_in_time_constituents=True, point_in_time_peers=True,
            classifications_reported=(
                "CORE-STABLE", "CORE-GROWTH", "SPEC-GROWTH", "FINANCIAL-BANK", "REIT", "INSURER",
            ),
            benchmarks_reported=(
                "index", "gcfp_v2_rules", "equal_weight_all_passers",
                "no_momentum_overlay", "single_anchor_valuation",
            ),
            walk_forward_holdout=(date(2022, 1, 1), date(2026, 1, 1)),
        )
    )
    assert not verdict.certified
    assert any("2000-2002" in b for b in verdict.blocking)
    assert any("2008-2009" in b for b in verdict.blocking)


def test_protocol_rejects_a_blended_backtest():
    verdict = check(
        BacktestCoverage(
            start=date(1999, 1, 1), end=date(2026, 1, 1),
            point_in_time_constituents=True, point_in_time_peers=True,
            classifications_reported=("CORE-STABLE",),
            benchmarks_reported=(
                "index", "gcfp_v2_rules", "equal_weight_all_passers",
                "no_momentum_overlay", "single_anchor_valuation",
            ),
            walk_forward_holdout=(date(2022, 1, 1), date(2026, 1, 1)),
        )
    )
    assert not verdict.certified
    assert any("separately per classification" in b for b in verdict.blocking)


def test_protocol_states_survivorship_inflation_in_the_header():
    verdict = check(
        BacktestCoverage(
            start=date(1999, 1, 1), end=date(2026, 1, 1),
            point_in_time_constituents=False, point_in_time_peers=False,
            classifications_reported=(
                "CORE-STABLE", "CORE-GROWTH", "SPEC-GROWTH", "FINANCIAL-BANK", "REIT", "INSURER",
            ),
            benchmarks_reported=(
                "index", "gcfp_v2_rules", "equal_weight_all_passers",
                "no_momentum_overlay", "single_anchor_valuation",
            ),
            walk_forward_holdout=(date(2022, 1, 1), date(2026, 1, 1)),
        )
    )
    assert verdict.certified
    rendered = verdict.render()
    assert rendered.startswith("!!")
    assert "INFLATED BY AN UNKNOWN BUT MATERIAL AMOUNT" in rendered


def test_protocol_rejects_tuning_on_the_holdout():
    verdict = check(
        BacktestCoverage(
            start=date(1999, 1, 1), end=date(2026, 1, 1),
            point_in_time_constituents=True, point_in_time_peers=True,
            classifications_reported=(
                "CORE-STABLE", "CORE-GROWTH", "SPEC-GROWTH", "FINANCIAL-BANK", "REIT", "INSURER",
            ),
            benchmarks_reported=(
                "index", "gcfp_v2_rules", "equal_weight_all_passers",
                "no_momentum_overlay", "single_anchor_valuation",
            ),
            walk_forward_holdout=(date(2022, 1, 1), date(2026, 1, 1)),
            tuned_on_holdout=True,
        )
    )
    assert not verdict.certified
    assert any("holdout is spent" in b for b in verdict.blocking)


def test_sweep_picks_mid_plateau_and_distrusts_a_spike():
    plateau = sweep("x", [1, 2, 3, 4, 5], lambda v: {1: 0.5, 2: 0.95, 3: 1.0, 4: 0.97, 5: 0.6}[v])
    assert plateau.recommended == 3

    spike = sweep("x", [1, 2, 3, 4, 5], lambda v: {1: 0.2, 2: 0.2, 3: 1.0, 4: 0.2, 5: 0.2}[v])
    assert spike.recommended is None
    assert any("isolated spike" in n for n in spike.notes)


def test_distribution_reports_more_than_the_mean():
    outcomes = [
        PositionOutcome("A", "SPEC-GROWTH", -100.0, 400),
        PositionOutcome("B", "SPEC-GROWTH", -30.0, 400),
        PositionOutcome("C", "SPEC-GROWTH", 250.0, 900),
    ]
    summary = summarise(outcomes)
    assert summary.loss_rate_pct == pytest.approx(66.7, abs=0.1)
    assert summary.total_loss_rate_pct == pytest.approx(33.3, abs=0.1)
    assert summary.median_pct == -30.0
    assert summary.worst_pct == -100.0
    assert set(summary.percentiles) == {5, 25, 50, 75, 95}


def test_results_are_reportable_per_classification():
    outcomes = [
        PositionOutcome("A", "CORE-STABLE", 12.0, 900),
        PositionOutcome("B", "SPEC-GROWTH", -100.0, 300),
    ]
    grouped = by_classification(outcomes)
    assert set(grouped) == {"CORE-STABLE", "SPEC-GROWTH"}


def test_drawdown_and_recovery_metrics():
    curve = [100.0, 120.0, 60.0, 80.0, 130.0]
    assert max_drawdown_pct(curve) == pytest.approx(-50.0)
    # Peak at index 1, regained at index 4.
    assert time_to_recovery(curve) == 3


def test_unrecovered_drawdown_is_not_reported_as_zero():
    assert time_to_recovery([100.0, 120.0, 60.0, 80.0]) == 2


def test_monotonic_curve_has_no_recovery_wait():
    assert time_to_recovery([100.0, 110.0, 120.0]) == 0


def test_classification_accuracy_is_its_own_metric():
    assert classification_accuracy_pct(
        ["CORE-STABLE", "REIT", "SPEC-GROWTH"], ["CORE-STABLE", "REIT", "CORE-GROWTH"]
    ) == pytest.approx(66.7, abs=0.1)
