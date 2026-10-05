"""qc_algorithm.execution -- what the system may do to a portfolio."""

from __future__ import annotations

import pytest

from gcfp.config import CORE_STABLE, SPEC_GROWTH
from gcfp.modules.f_sizing import QUALIFIED_NO_HEADROOM, SizingDecision
from qc_algorithm.execution import (
    EXIT, OPEN, TRIM, ExecutionPlan, OrderIntent, apply_orders, plan_orders,
)


def sized(symbol, classification=CORE_STABLE, sleeve="CORE", portfolio_pct=2.8, status="SIZED"):
    return SizingDecision(
        symbol=symbol, classification=classification, sleeve=sleeve, status=status,
        intended_sleeve_pct=8.0, intended_value=28_000.0, intended_portfolio_pct=portfolio_pct,
    )


class RecordingAlgorithm:
    def __init__(self):
        self.calls = []

    def set_holdings(self, symbol, fraction, tag=None):
        self.calls.append(("set_holdings", symbol, fraction, tag))

    def liquidate(self, symbol, tag=None):
        self.calls.append(("liquidate", symbol, tag))


# -- planning ---------------------------------------------------------------


def test_exit_beats_open_for_the_same_symbol(params):
    """A SELL verdict is not netted against a BUY in the same name."""
    plan = plan_orders([sized("AAA")], exits=["AAA"], trims=[], current_weights_pct={}, params=params)
    assert [i.action for i in plan.intents] == [EXIT]
    assert any("exited this cycle" in why for _, why in plan.skipped)


def test_trim_and_open_do_not_collide(params):
    plan = plan_orders(
        [sized("BBB")], exits=[], trims=[("BBB", 10.0)], current_weights_pct={}, params=params
    )
    assert [i.action for i in plan.intents] == [TRIM]


def test_no_headroom_is_held_in_cash_not_forced_in(params):
    decision = sized("CCC", status=QUALIFIED_NO_HEADROOM)
    plan = plan_orders([decision], exits=[], trims=[], current_weights_pct={}, params=params)
    assert plan.intents == []
    assert any("held in cash, not forced in" in why for _, why in plan.skipped)


def test_existing_position_is_not_automatically_sized_up(params):
    plan = plan_orders([sized("DDD")], exits=[], trims=[], current_weights_pct={"DDD": 2.0}, params=params)
    assert plan.intents == []
    assert any("sizing up is not automated" in why for _, why in plan.skipped)


def test_target_weight_is_percent_of_total_portfolio(params):
    plan = plan_orders([sized("EEE", portfolio_pct=2.25)], [], [], {}, params)
    intent = plan.intents[0]
    assert intent.target_weight_pct == pytest.approx(2.25)
    assert intent.target_fraction == pytest.approx(0.0225)


# -- the second, independent growth cap ------------------------------------


def test_growth_cap_is_re_enforced_at_the_boundary(params):
    """Failure mode #9 is sizing drift; one enforcement point is one too few."""
    decisions = [sized(f"G{i}", SPEC_GROWTH, "GROWTH", 2.25) for i in range(3)]
    plan = plan_orders(decisions, [], [], {}, params, current_growth_weight_pct=12.0)
    opened = plan.by_action(OPEN)
    assert len(opened) == 1  # 12 + 2.25 fits under 15; a second would not
    assert plan.notices and "CAP ENFORCED AT THE EXECUTION BOUNDARY" in plan.notices[0]


def test_growth_cap_drops_smallest_first(params):
    """An overshoot should not cost the highest-conviction name in the batch."""
    decisions = [
        sized("BIG", SPEC_GROWTH, "GROWTH", 2.25),
        sized("SMALL", SPEC_GROWTH, "GROWTH", 1.5),
    ]
    plan = plan_orders(decisions, [], [], {}, params, current_growth_weight_pct=12.0)
    kept = [i.symbol for i in plan.by_action(OPEN)]
    assert kept == ["BIG"]


def test_core_names_are_untouched_by_the_growth_cap(params):
    decisions = [sized(f"C{i}", CORE_STABLE, "CORE", 2.8) for i in range(4)]
    plan = plan_orders(decisions, [], [], {}, params, current_growth_weight_pct=14.9)
    assert len(plan.by_action(OPEN)) == 4


def test_growth_additions_within_cap_are_untouched(params):
    decisions = [sized("G1", SPEC_GROWTH, "GROWTH", 2.25)]
    plan = plan_orders(decisions, [], [], {}, params, current_growth_weight_pct=0.0)
    assert len(plan.by_action(OPEN)) == 1
    assert plan.notices == []


# -- submission -------------------------------------------------------------


def test_apply_dispatches_exit_to_liquidate_and_open_to_set_holdings(params):
    algorithm = RecordingAlgorithm()
    plan = ExecutionPlan(intents=[
        OrderIntent("XXX", EXIT, 0.0, "sell verdict"),
        OrderIntent("YYY", OPEN, 2.8, "buy verdict"),
    ])
    apply_orders(algorithm, plan, live_enabled=True)
    assert algorithm.calls[0][0] == "liquidate"
    assert algorithm.calls[0][1] == "XXX"
    assert algorithm.calls[1][0] == "set_holdings"
    assert algorithm.calls[1][2] == pytest.approx(0.028)


def test_every_order_carries_its_reason_as_a_tag(params):
    """The broker record should say why, not just what."""
    algorithm = RecordingAlgorithm()
    plan = ExecutionPlan(intents=[OrderIntent("ZZZ", OPEN, 1.0, "BUY: 8.0% of the CORE sleeve")])
    apply_orders(algorithm, plan, live_enabled=True)
    assert algorithm.calls[0][3] == "BUY: 8.0% of the CORE sleeve"


def test_dry_run_places_nothing(params):
    algorithm = RecordingAlgorithm()
    plan = ExecutionPlan(intents=[OrderIntent("AAA", OPEN, 2.5, "test")])
    log = apply_orders(algorithm, plan, live_enabled=False)
    assert algorithm.calls == []
    assert "DRY RUN" in log[0]


def test_empty_plan_is_a_no_op(params):
    algorithm = RecordingAlgorithm()
    assert apply_orders(algorithm, ExecutionPlan(), live_enabled=True) == []
    assert algorithm.calls == []
