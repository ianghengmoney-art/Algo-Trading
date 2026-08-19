"""QCDataAdapter -- mapping QuantConnect fundamentals onto the GCFP model."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from gcfp.config import CORE_STABLE
from gcfp.data.base import Capability, DataUnavailable
from gcfp.data.quantconnect import ANNUAL_PERIODS, QCDataAdapter, period_value
from gcfp.modules.a_health import build_trailing_window, classify, evaluate_health
from gcfp.types import Verdict
from qc_fakes import absent, make_fundamental, mpf


def _adapter(fundamentals, history=None, as_of=date(2026, 6, 30)):
    by_symbol = {str(f.symbol): f for f in fundamentals}
    hist = history or {str(f.symbol): [f] for f in fundamentals}
    return QCDataAdapter(by_symbol, history_provider=lambda s, y: hist.get(s, []), as_of=as_of)


# -- the silent-zero trap ---------------------------------------------------


def test_absent_field_is_missing_not_zero():
    """QC returns 0.0 for an unreported figure; that must not become a value."""
    assert period_value(absent(), ANNUAL_PERIODS) is None
    assert period_value(None, ANNUAL_PERIODS) is None


def test_reported_zero_is_preserved():
    """A genuine zero is data, and must survive."""
    assert period_value(mpf(annual=0.0), ANNUAL_PERIODS) == 0.0


def test_annual_and_quarterly_read_different_periods():
    f = make_fundamental(revenue=8_000_000_000.0)
    adapter = _adapter([f])
    annual = adapter.get_annual_financials("TEST")[0]
    quarterly = adapter.get_quarterly_financials("TEST")[0]
    assert annual.revenue == pytest.approx(8_000_000_000.0)
    assert quarterly.revenue == pytest.approx(2_000_000_000.0)


# -- structural routing -----------------------------------------------------


def test_reit_detected_from_the_explicit_field_not_an_industry_string():
    adapter = _adapter([make_fundamental(symbol="REITCO", is_reit=True, template="R")])
    assert adapter.get_profile("REITCO").is_reit is True


@pytest.mark.parametrize(
    "template,sic,expect",
    [("B", "6021", "bank"), ("I", "6331", "insurer"), ("R", "6798", "reit"), ("N", "3531", "none")],
)
def test_industry_template_code_drives_the_structural_tag(template, sic, expect):
    adapter = _adapter([make_fundamental(symbol="X", template=template, sic=sic)])
    profile = adapter.get_profile("X")
    got = {
        "bank": profile.is_bank,
        "insurer": profile.is_insurer,
        "reit": profile.is_reit,
    }
    if expect == "none":
        assert not any(got.values())
    else:
        assert got[expect] is True


def test_sic_corroborates_when_the_template_code_is_missing():
    """The FMP failure was inferring structure from vendor industry strings."""
    adapter = _adapter([make_fundamental(symbol="BANKX", template=None, sic="6021")])
    assert adapter.get_profile("BANKX").is_bank is True


def test_sector_code_maps_to_a_name_the_other_modules_share():
    adapter = _adapter([make_fundamental(sector_code=103)])
    assert adapter.get_profile("TEST").sector == "Financial Services"


def test_unknown_sector_code_is_none_rather_than_a_guess():
    adapter = _adapter([make_fundamental(sector_code=999)])
    assert adapter.get_profile("TEST").sector is None


# -- capabilities -----------------------------------------------------------


def test_point_in_time_is_advertised():
    """The reason this adapter exists: FMP could not certify a backtest."""
    assert Capability.POINT_IN_TIME in QCDataAdapter().capabilities()


def test_stop_conditions_clear_on_this_source():
    from gcfp.data.base import CapabilityGate

    gate = CapabilityGate("quantconnect", QCDataAdapter().capabilities(), multiple_history_years=10.0)
    assert gate.spec_growth_enabled
    assert gate.own_history_anchor_enabled
    assert gate.point_in_time
    assert gate.blocking_notices() == []


# -- gate A4, partially rescued --------------------------------------------


def test_auditor_change_is_detected():
    f_old = make_fundamental(end_time=date(2025, 6, 30), auditor="Auditor A")
    f_new = make_fundamental(end_time=date(2026, 6, 30), auditor="Auditor B")
    adapter = _adapter([f_new], history={"TEST": [f_old, f_new]})
    assert adapter.get_filing_flags("TEST").auditor_change_within_lookback is True


def test_stable_auditor_is_not_flagged():
    f1 = make_fundamental(end_time=date(2025, 6, 30), auditor="Auditor A")
    f2 = make_fundamental(end_time=date(2026, 6, 30), auditor="Auditor A")
    adapter = _adapter([f2], history={"TEST": [f1, f2]})
    assert adapter.get_filing_flags("TEST").auditor_change_within_lookback is False


def test_the_three_unavailable_red_flags_stay_unknown():
    """Unknown must not read as clean -- gate A4 turns None into DATA_GAP."""
    adapter = _adapter([make_fundamental()])
    flags = adapter.get_filing_flags("TEST")
    assert flags.restatement_within_lookback is None
    assert flags.going_concern_language is None
    assert flags.delayed_filing is None


def test_a4_is_a_data_gap_on_this_source(params):
    """The documented consequence: no candidate reaches a full PASS yet."""
    adapter = _adapter([make_fundamental()])
    result = evaluate_health(adapter.load_candidate("TEST"), params, as_of=date(2026, 6, 30))
    a4 = result.gate("A4")
    assert a4.verdict is Verdict.DATA_GAP
    assert result.verdict is not Verdict.PASS


# -- REIT gap survives ------------------------------------------------------


def test_affo_inputs_are_absent_so_b4_must_fall_back():
    adapter = _adapter([make_fundamental(symbol="REITCO", is_reit=True, template="R")])
    period = adapter.get_annual_financials("REITCO")[0]
    assert period.recurring_capex is None
    assert period.straight_line_rent_adjustment is None
    # FFO remains derivable from what is served.
    assert period.real_estate_depreciation is not None
    assert period.gains_on_property_sales is not None


# -- multiples, peers, sector ----------------------------------------------


def test_multiple_series_built_from_valuation_ratios():
    rows = [
        make_fundamental(end_time=date(2026, 6, 30) - timedelta(days=91 * i), pe_ratio=10.0 + i)
        for i in range(30)
    ]
    adapter = _adapter([rows[0]], history={"TEST": list(reversed(rows))})
    series = adapter.get_multiple_series("TEST", "trailing P/E")
    assert len(series.points) == 30
    assert series.years_covered > 7


def test_unmapped_multiple_is_refused():
    adapter = _adapter([make_fundamental()])
    with pytest.raises(DataUnavailable):
        adapter.get_multiple_series("TEST", "P/Sales")


def test_peers_come_from_the_industry_group():
    same = make_fundamental(symbol="PEER1", industry_group=31052)
    other = make_fundamental(symbol="OTHER", industry_group=10101)
    adapter = _adapter([make_fundamental(symbol="TEST", industry_group=31052), same, other])
    peers = adapter.get_peers("TEST")
    assert "PEER1" in peers and "OTHER" not in peers


def test_sector_leverage_needs_computable_members():
    adapter = _adapter([make_fundamental(symbol=f"S{i}") for i in range(5)])
    values = adapter.get_sector_net_debt_ebitda("Industrials")
    assert len(values) == 5


# -- estimates --------------------------------------------------------------


def test_no_earnings_calendar_means_no_blackout():
    """A real loss of entry hygiene, recorded rather than papered over."""
    adapter = _adapter([make_fundamental()])
    assert adapter.get_estimates("TEST").next_earnings_date is None


def test_forward_eps_and_yield_are_derived():
    adapter = _adapter([make_fundamental(price=70.0, pe_ratio=10.0)])
    estimates = adapter.get_estimates("TEST")
    assert estimates.forward_eps == pytest.approx(70.0 / 9.0)
    assert estimates.dividend_yield_pct == pytest.approx(100.0 * 1.80 / 70.0)


# -- it actually feeds Module A --------------------------------------------


def test_adapter_output_classifies_through_module_a(params):
    rows = [
        make_fundamental(end_time=date(2026, 6, 30) - timedelta(days=365 * i),
                         period_end=date(2025 - i, 12, 31),
                         revenue=9_000_000_000.0 * (0.93 ** i))
        for i in range(6)
    ]
    adapter = _adapter([rows[0]], history={"TEST": list(reversed(rows))})
    candidate = adapter.load_candidate("TEST")
    assert build_trailing_window(candidate) is not None
    tag, detail = classify(candidate, params)
    assert tag == CORE_STABLE, detail


def test_missing_symbol_raises_rather_than_returning_empty():
    adapter = _adapter([make_fundamental()])
    with pytest.raises(DataUnavailable):
        adapter.get_profile("NOPE")
