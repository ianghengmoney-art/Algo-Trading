"""Modules D and E -- conviction scoring and triggers."""

from __future__ import annotations

from datetime import date, timedelta

from gcfp.config import CORE_STABLE, SPEC_GROWTH
from gcfp.modules.d_conviction import ConvictionScore, apply_momentum_ranking
from gcfp.modules.e_triggers import (
    BUY, HOLD, NO_ACTION, REVIEW, SELL, evaluate_buy, evaluate_sell, in_earnings_blackout,
)
from gcfp.types import Direction, Verdict


class _Anchor:
    def __init__(self, direction, upside):
        self.direction = direction
        self.implied_upside_pct = upside

    @property
    def confirms_undervalued(self):
        return self.direction is Direction.UNDERVALUED

    @property
    def confirms_overvalued(self):
        return self.direction is Direction.OVERVALUED


class _Tri:
    def __init__(self, own, peer, disagree=False):
        self.own_history = own
        self.peer = peer
        self.anchors_disagree = disagree

    @property
    def both_confirm_undervalued(self):
        return not self.anchors_disagree and self.own_history.confirms_undervalued and self.peer.confirms_undervalued

    @property
    def both_confirm_overvalued(self):
        return not self.anchors_disagree and self.own_history.confirms_overvalued and self.peer.confirms_overvalued

    @property
    def confirming_count(self):
        if self.anchors_disagree:
            return 0
        return sum(1 for a in (self.own_history, self.peer) if a.confirms_undervalued)


def _both_under():
    return _Tri(_Anchor(Direction.UNDERVALUED, 40.0), _Anchor(Direction.UNDERVALUED, 35.0))


def _score(total):
    s = ConvictionScore("X")
    s.health_margin = total
    s.momentum_pending = False
    return s


def test_buy_requires_all_three_conditions(params):
    good = evaluate_buy("X", CORE_STABLE, 70.0, 100.0, _both_under(), _score(70.0), params)
    assert good.action == BUY

    # Discount alone is not enough.
    one_anchor = _Tri(_Anchor(Direction.UNDERVALUED, 40.0), _Anchor(Direction.OVERVALUED, -5.0))
    assert evaluate_buy("X", CORE_STABLE, 70.0, 100.0, one_anchor, _score(70.0), params).action == NO_ACTION

    # Conviction below the floor blocks it.
    assert evaluate_buy("X", CORE_STABLE, 70.0, 100.0, _both_under(), _score(55.0), params).action == NO_ACTION

    # Too small a discount blocks it.
    assert evaluate_buy("X", CORE_STABLE, 90.0, 100.0, _both_under(), _score(70.0), params).action == NO_ACTION


def test_more_uncertain_classifications_need_a_larger_cushion(params):
    price, fair = 68.0, 100.0  # a 32% discount
    assert evaluate_buy("X", CORE_STABLE, price, fair, _both_under(), _score(70.0), params).action == BUY
    assert evaluate_buy("X", SPEC_GROWTH, price, fair, _both_under(), _score(70.0), params).action == NO_ACTION


def test_divergence_blocks_a_buy_outright(params):
    diverging = _Tri(_Anchor(Direction.UNDERVALUED, 60.0), _Anchor(Direction.UNDERVALUED, 5.0), disagree=True)
    result = evaluate_buy("X", CORE_STABLE, 60.0, 100.0, diverging, _score(80.0), params)
    assert result.action == NO_ACTION
    assert any("anchors disagree" in r for r in result.reasons)


def test_earnings_blackout_defers_the_alert_without_changing_the_verdict(params):
    as_of = date(2026, 6, 30)
    result = evaluate_buy(
        "X", CORE_STABLE, 70.0, 100.0, _both_under(), _score(70.0), params,
        as_of=as_of, next_earnings=as_of + timedelta(days=5),
    )
    assert result.action == BUY          # the verdict stands
    assert result.deferred is True       # only the alert waits
    assert result.is_buy is False
    assert "stays on the pass list" in result.deferral_reason


def test_blackout_window_is_bounded(params):
    as_of = date(2026, 6, 30)
    assert in_earnings_blackout(as_of, as_of + timedelta(days=5), params)
    assert not in_earnings_blackout(as_of, as_of + timedelta(days=60), params)
    assert not in_earnings_blackout(as_of, None, params)


def test_sell_on_module_a_failure_regardless_of_price(params):
    result = evaluate_sell("X", CORE_STABLE, 200.0, 100.0, _both_under(), Verdict.FAIL, params)
    assert result.action == SELL
    assert "fundamental deterioration" in result.reasons[0]


def test_sell_on_overvaluation_needs_both_anchors(params):
    both_over = _Tri(_Anchor(Direction.OVERVALUED, -30.0), _Anchor(Direction.OVERVALUED, -25.0))
    assert evaluate_sell("X", CORE_STABLE, 130.0, 100.0, both_over, Verdict.PASS, params).action == SELL

    mixed = _Tri(_Anchor(Direction.OVERVALUED, -30.0), _Anchor(Direction.UNDERVALUED, 10.0))
    result = evaluate_sell("X", CORE_STABLE, 130.0, 100.0, mixed, Verdict.PASS, params)
    assert result.action == REVIEW
    assert "mixed valuation signal" in result.reasons[0]


def test_no_sell_when_merely_expensive(params):
    both_over = _Tri(_Anchor(Direction.OVERVALUED, -5.0), _Anchor(Direction.OVERVALUED, -4.0))
    assert evaluate_sell("X", CORE_STABLE, 110.0, 100.0, both_over, Verdict.PASS, params).action == HOLD


def test_momentum_orders_passers_and_cannot_admit_one(params):
    scores = [_score(0.0) for _ in range(3)]
    for s, sym in zip(scores, ["A", "B", "C"]):
        s.symbol = sym
        s.health_margin = 0.0
    apply_momentum_ranking(scores, {"A": -20.0, "B": 5.0, "C": 60.0}, params)
    by_symbol = {s.symbol: s.momentum for s in scores}
    assert by_symbol["C"] > by_symbol["B"] > by_symbol["A"]
    assert max(by_symbol.values()) <= params.conviction.momentum_max

    # Momentum alone can never reach the conviction floor.
    assert params.conviction.momentum_max < params.triggers.min_conviction_to_buy


def test_momentum_absent_scores_zero_rather_than_guessing(params):
    score = _score(0.0)
    score.symbol = "A"
    apply_momentum_ranking([score], {"A": None}, params)
    assert score.momentum == 0.0
    assert any("no rankable return" in n for n in score.notes)
