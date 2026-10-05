"""Module C -- two anchors, computed independently, never averaged."""

from __future__ import annotations

import statistics
from datetime import date, timedelta

import pytest

from gcfp.config import CORE_STABLE
from gcfp.modules.c_triangulation import (
    PeerCandidate, detect_step_change, own_history_anchor,
    peer_anchor, pegy, select_peers, triangulate,
)
from gcfp.types import Direction, MultiplePoint, MultipleSeries


def _series(values, start=date(2016, 1, 1)):
    points = tuple(
        MultiplePoint(start + timedelta(days=91 * i), v) for i, v in enumerate(values)
    )
    return MultipleSeries("X", "trailing P/E", points)


def test_step_change_detected_on_a_sustained_level_shift(params):
    values = [15.0 + 0.3 * (i % 3) for i in range(20)] + [29.0 + 0.3 * (i % 3) for i in range(20)]
    dates = [date(2016, 1, 1) + timedelta(days=91 * i) for i in range(40)]
    step = detect_step_change(values, dates, params)
    assert step.detected
    assert step.before_mean < step.after_mean


def test_noise_alone_does_not_trip_the_step_change_scan(params):
    values = [18.0 + ((i * 7) % 5 - 2) * 0.4 for i in range(40)]
    dates = [date(2016, 1, 1) + timedelta(days=91 * i) for i in range(40)]
    assert not detect_step_change(values, dates, params).detected


def test_rerating_flag_names_the_risk(params):
    values = [12.0 + 0.2 * (i % 4) for i in range(16)] + [26.0 + 0.2 * (i % 4) for i in range(16)]
    reading = own_history_anchor(_series(values), 24.0, params, CORE_STABLE)
    assert any("POSSIBLE RE-RATING" in w for w in reading.warnings)


def test_short_history_is_refused_not_silently_shortened(params):
    values = [18.0] * 12  # three years
    reading = own_history_anchor(_series(values), 15.0, params, CORE_STABLE)
    assert reading.confidence == "unavailable"
    assert reading.implied_upside_pct is None
    assert any("Not falling back to a shorter window" in w for w in reading.warnings)


def test_between_five_and_seven_years_is_reduced_confidence(params):
    values = [18.0 + 0.5 * (i % 3) for i in range(24)]  # six years
    reading = own_history_anchor(_series(values), 12.0, params, CORE_STABLE)
    assert reading.confidence == "reduced"
    assert any("REDUCED CONFIDENCE" in w for w in reading.warnings)


def test_own_history_reports_z_score_and_percentile(params):
    values = [18.0 + (i % 5) for i in range(32)]
    reading = own_history_anchor(_series(values), 12.0, params, CORE_STABLE)
    assert reading.detail["z_score"] is not None
    assert 0.0 <= reading.detail["percentile"] <= 100.0
    assert reading.direction is Direction.UNDERVALUED


def _subject(**kwargs):
    base = dict(symbol="SUB", sector="Tech", market_cap=10e9, revenue_growth_pct=20.0, multiple=15.0)
    base.update(kwargs)
    return PeerCandidate(**base)


def test_peer_selection_logs_every_rejection_with_a_reason(params):
    subject = _subject()
    candidates = [
        _subject(symbol="SAMESECTOR_OK", market_cap=12e9),
        _subject(symbol="WRONGSECTOR", sector="Energy"),
        _subject(symbol="TOOBIG", market_cap=90e9),
        _subject(symbol="TOOSMALL", market_cap=1e9),
        _subject(symbol="WRONGGROWTH", revenue_growth_pct=45.0),
        _subject(symbol="NOMULTIPLE", multiple=None),
    ]
    selection = select_peers(subject, candidates, params)
    rejected = dict(selection.excluded)
    assert "different sector" in rejected["WRONGSECTOR"]
    assert "outside" in rejected["TOOBIG"] and "band" in rejected["TOOBIG"]
    assert "outside" in rejected["TOOSMALL"]
    assert "revenue growth" in rejected["WRONGGROWTH"]
    assert "no usable multiple" in rejected["NOMULTIPLE"]
    assert [p.symbol for p in selection.included] == ["SAMESECTOR_OK"]


def test_peer_anchor_uses_the_median_not_the_mean(params):
    subject = _subject(multiple=10.0)
    peers = [_subject(symbol=f"P{i}", multiple=m) for i, m in enumerate([12, 13, 14, 15, 400])]
    selection = select_peers(subject, peers, params)
    reading = peer_anchor(subject, selection, params, "trailing P/E")
    multiples = [p.multiple for p in selection.included]
    assert reading.reference == statistics.median(multiples)
    assert reading.reference != statistics.fmean(multiples)


def test_peer_anchor_refuses_to_widen_bands_to_reach_a_quorum(params):
    subject = _subject()
    selection = select_peers(subject, [_subject(symbol="ONLYONE", market_cap=11e9)], params)
    reading = peer_anchor(subject, selection, params, "trailing P/E")
    assert reading.confidence == "unavailable"
    assert any("Not widening the bands" in w for w in reading.warnings)


def test_divergence_beyond_threshold_refuses_a_combined_verdict(params):
    subject = _subject(multiple=10.0)
    peers = [_subject(symbol=f"P{i}", multiple=30.0) for i in range(5)]
    selection = select_peers(subject, peers, params)
    peer_reading = peer_anchor(subject, selection, params, "trailing P/E")
    own = own_history_anchor(_series([10.5] * 32), 10.0, params, CORE_STABLE)

    result = triangulate("SUB", CORE_STABLE, own, peer_reading, selection, params)
    assert result.anchors_disagree
    assert result.combined_verdict == "NO COMBINED VERDICT -- ANCHORS DISAGREE"
    assert not result.both_confirm_undervalued
    assert result.confirming_count == 0
    assert any("ANCHORS DISAGREE" in w for w in result.warnings)


def test_anchors_are_never_averaged(params):
    subject = _subject(multiple=10.0)
    peers = [_subject(symbol=f"P{i}", multiple=12.0) for i in range(5)]
    selection = select_peers(subject, peers, params)
    peer_reading = peer_anchor(subject, selection, params, "trailing P/E")
    own = own_history_anchor(_series([13.0 + 0.2 * (i % 3) for i in range(32)]), 10.0, params, CORE_STABLE)
    result = triangulate("SUB", CORE_STABLE, own, peer_reading, selection, params)

    # Both readings survive separately; no blended number is produced anywhere.
    assert result.own_history.implied_upside_pct != result.peer.implied_upside_pct
    assert not hasattr(result, "combined_upside_pct")


def test_pegy_is_na_rather_than_imputed(params):
    value, note = pegy(None, 12.0, 1.0, params)
    assert value is None and "n/a" in note
    value, note = pegy(20.0, None, 1.0, params)
    assert value is None and "n/a" in note


def test_pegy_flags_but_does_not_reject(params):
    value, note = pegy(60.0, 10.0, 0.0, params)
    assert value == pytest.approx(6.0)
    assert "flagged, not rejected" in note
