"""Section 17's two stop conditions, enforced in code rather than documented."""

from __future__ import annotations

from datetime import date

from gcfp.config import SPEC_GROWTH
from gcfp.data.base import Capability, CapabilityGate
from gcfp.modules.a_health import build_trailing_window, classify, gate_a3_earnings_quality

FULL = {
    Capability.PROFILE, Capability.ANNUAL_STATEMENTS, Capability.QUARTERLY_STATEMENTS,
    Capability.SHARE_COUNT_HISTORY, Capability.CASH_BURN_HISTORY,
    Capability.MULTIPLE_HISTORY, Capability.PEER_LIST,
}


def test_spec_growth_disabled_without_cash_burn_or_share_count():
    without_burn = CapabilityGate("src", FULL - {Capability.CASH_BURN_HISTORY}, 10.0)
    without_shares = CapabilityGate("src", FULL - {Capability.SHARE_COUNT_HISTORY}, 10.0)
    assert not without_burn.spec_growth_enabled
    assert not without_shares.spec_growth_enabled
    assert CapabilityGate("src", FULL, 10.0).spec_growth_enabled


def test_disabled_spec_growth_refuses_the_tag_rather_than_approximating(adapter, params):
    """A name that would be SPEC-GROWTH is left unclassified, not routed elsewhere."""
    blocked = CapabilityGate("src", FULL - {Capability.CASH_BURN_HISTORY}, 10.0)
    candidate = adapter.load_candidate("BURNER")

    assert classify(candidate, params, CapabilityGate("src", FULL, 10.0))[0] == SPEC_GROWTH

    tag, detail = classify(candidate, params, blocked)
    assert tag is None
    assert detail["spec_growth_disabled"] is True


def test_pre_profit_a3_branch_is_a_data_gap_when_burn_history_is_missing(adapter, params):
    blocked = CapabilityGate("src", FULL - {Capability.CASH_BURN_HISTORY}, 10.0)
    window = build_trailing_window(adapter.load_candidate("BURNER"))
    outcome = gate_a3_earnings_quality(window, params, blocked)
    assert outcome.verdict.value == "DATA_GAP"
    assert "cash-burn history" in outcome.reason


def test_c1_disabled_below_seven_years_and_reported_as_single_anchor():
    short = CapabilityGate("src", FULL, multiple_history_years=5.5)
    assert not short.own_history_anchor_enabled
    assert short.single_anchor_degraded
    notice = "\n".join(short.blocking_notices())
    assert "MODULE C1 DISABLED" in notice
    assert "no BUY alert can fire" in notice


def test_c1_enabled_at_exactly_seven_years():
    assert CapabilityGate("src", FULL, multiple_history_years=7.0).own_history_anchor_enabled


def test_missing_point_in_time_is_a_header_notice_not_a_footnote():
    gate = CapabilityGate("src", FULL, 10.0)
    notice = "\n".join(gate.blocking_notices())
    assert "NO POINT-IN-TIME DATA" in notice
    assert "survivorship bias" in notice


def test_single_anchor_source_cannot_produce_a_buy(adapter, params):
    """With C1 off, buy condition 2 is unsatisfiable by construction."""
    from gcfp.engine import screen

    gate = CapabilityGate("src", FULL, multiple_history_years=2.0)
    result = screen(adapter, ["MATURE", "REITCO", "FASTPROF"], params, gate, as_of=date(2026, 6, 30))
    assert result.passers() == []
