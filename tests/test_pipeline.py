"""End-to-end ordering, and the properties that only emerge once modules compose."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from gcfp.classification import Classification, Regime
from gcfp.modules.c_anchors import AnchorMode, PeerCandidate
from gcfp.modules.e_triggers import SignalType
from gcfp.modules.f_sizing import Bucket, PortfolioState
from gcfp.modules.k_currency import FxTable
from gcfp.pipeline import CandidateInputs, evaluate_candidate
from gcfp.storage import Store
from tests.conftest import build_company, stable_profile


@pytest.fixture
def peers():
    return [
        PeerCandidate(f"P{i}", multiple=m, market_cap=mc, revenue_growth=0.05,
                      group="Machinery")
        for i, (m, mc) in enumerate(
            [(19.0, 60e9), (21.0, 90e9), (18.5, 50e9), (22.0, 110e9), (20.0, 75e9)]
        )
    ]


@pytest.fixture
def book():
    return PortfolioState(
        total_value=1_000_000.0,
        bucket_values={
            Bucket.BALLAST: 500_000.0,
            Bucket.CORE_PICKS: 300_000.0,
            Bucket.GROWTH_PICKS: 50_000.0,
        },
        position_values={"HELD1": 150_000.0, "HELD2": 150_000.0},
        position_sectors={"HELD1": "Technology", "HELD2": "Healthcare"},
        position_regimes={"HELD1": Regime.CORE, "HELD2": Regime.CORE},
    )


@pytest.fixture
def fx():
    return FxTable({"USDSGD": 1.29}, date.today())


def run(price, multiple, market, config, peers, book, fx, **kw):
    company = build_company(price=price)
    inputs = CandidateInputs(
        current_multiple=multiple, trailing_pe=multiple, peer_candidates=peers,
        subject_group="Machinery", subject_growth=0.05,
        thesis_invalidation="ROIC-WACC spread below zero for two years",
    )
    return evaluate_candidate(
        company, market, config, inputs, book, fx,
        momentum_returns={"STABLECO": 0.18, "X": 0.05, "Y": 0.30}, **kw
    )


class TestPipelineOrdering:
    def test_a_clean_name_runs_the_whole_stack(self, market, config, peers, book, fx):
        ev = run(120.0, 11.0, market, config, peers, book, fx)
        assert ev.stopped_at is None
        assert ev.classification is Classification.CORE_STABLE
        assert ev.fair_value is not None
        assert ev.triangulation is not None
        assert ev.conviction is not None
        assert ev.signal is not None

    def test_conviction_rises_as_the_discount_deepens(
        self, market, config, peers, book, fx
    ):
        """D2's whole purpose: the score measures the margin above the gate,
        so a name barely clearing it must not score like one clearing it well."""
        scores = [
            run(price, mult, market, config, peers, book, fx).conviction.total
            for price, mult in ((150.0, 13.0), (120.0, 11.0), (100.0, 9.0))
        ]
        assert scores == sorted(scores), scores
        assert scores[-1] - scores[0] > 10

    def test_a_name_at_its_gate_scores_zero_on_valuation_excess(
        self, market, config, peers, book, fx
    ):
        ev = run(120.0, 11.0, market, config, peers, book, fx)
        fair_value = ev.fair_value.fair_value_per_share
        at_gate_price = fair_value * 0.75  # exactly a 25% discount
        exact = run(at_gate_price, 11.0, market, config, peers, book, fx)
        component = exact.conviction.component("valuation excess")
        assert component.points == pytest.approx(0.0, abs=0.01)

    def test_sizing_only_runs_for_a_buy(self, market, config, peers, book, fx):
        no_action = run(150.0, 13.0, market, config, peers, book, fx)
        buy = run(100.0, 9.0, market, config, peers, book, fx)
        assert no_action.signal.signal is SignalType.NO_ACTION
        assert no_action.sizing is None
        assert buy.signal.signal is SignalType.BUY
        assert buy.sizing is not None

    def test_single_anchor_raises_the_bar_a_dual_anchor_name_clears(
        self, market, config, book, fx
    ):
        """The same company, same price: with only one anchor it needs 35%
        rather than 25%, and its conviction is capped."""
        company = build_company(price=120.0)
        common = dict(
            current_multiple=11.0, trailing_pe=11.0, subject_group="Machinery",
            subject_growth=0.05,
        )
        dual_peers = [
            PeerCandidate(f"P{i}", multiple=m, market_cap=mc, revenue_growth=0.05,
                          group="Machinery")
            for i, (m, mc) in enumerate(
                [(19.0, 60e9), (21.0, 90e9), (18.5, 50e9), (22.0, 110e9)]
            )
        ]
        dual = evaluate_candidate(
            company, market, config,
            CandidateInputs(peer_candidates=dual_peers, **common), book, fx,
        )
        single = evaluate_candidate(
            company, market, config,
            CandidateInputs(peer_candidates=dual_peers[:2], **common), book, fx,
        )
        assert dual.anchor_mode is AnchorMode.DUAL
        assert single.anchor_mode is AnchorMode.SINGLE_C1
        assert single.signal.values["threshold"] == pytest.approx(
            dual.signal.values["threshold"] + 0.10
        )
        assert single.conviction.total < dual.conviction.total

    def test_both_anchors_uncomputable_stops_at_c5(self, market, config, book, fx):
        """Module B alone is not grounds to proceed."""
        from gcfp.types import CorporateAction, CorporateActionType

        company = build_company(
            price=100.0,
            corporate_actions=[
                CorporateAction(
                    CorporateActionType.SPINOFF, date.today() - timedelta(days=300)
                )
            ],
        )
        ev = evaluate_candidate(
            company, market, config,
            CandidateInputs(current_multiple=11.0, peer_candidates=[]), book, fx,
        )
        assert ev.stopped_at == "C5"
        assert ev.fair_value is not None, "B ran before C, as the ordering requires"
        assert ev.signal is None, "no verdict was issued without an anchor"

    def test_earnings_blackout_suppresses_the_alert_but_keeps_the_pass(
        self, market, config, peers, book, fx
    ):
        soon = date.today() + timedelta(days=4)
        company = build_company(price=100.0, profile=stable_profile(next_earnings_date=soon))
        ev = evaluate_candidate(
            company, market, config,
            CandidateInputs(
                current_multiple=9.0, trailing_pe=9.0, peer_candidates=peers,
                subject_group="Machinery", subject_growth=0.05,
            ),
            book, fx,
            momentum_returns={"STABLECO": 0.18, "X": 0.05, "Y": 0.30},
        )
        assert ev.signal.signal is SignalType.BUY
        assert ev.signal.suppressed_until == soon


class TestAuditTrail:
    def test_the_ledger_records_peer_exclusions(self, market, config, peers, book, fx):
        ev = run(120.0, 11.0, market, config, peers, book, fx)
        peer_notes = [n for n in ev.ledger.notes if n.startswith("C2 peer")]
        assert len(peer_notes) == len(peers)
        assert all("INCLUDED" in n or "EXCLUDED" in n for n in peer_notes)

    def test_the_ledger_records_the_discount_rate_build(
        self, market, config, peers, book, fx
    ):
        ev = run(120.0, 11.0, market, config, peers, book, fx)
        discount_notes = [n for n in ev.ledger.notes if "discount_rate=" in n]
        assert discount_notes
        assert "beta=" in discount_notes[0] and "erp=" in discount_notes[0]

    def test_the_ledger_records_the_grouping_provenance(
        self, market, config, peers, book, fx
    ):
        ev = run(120.0, 11.0, market, config, peers, book, fx)
        assert any("VENDOR-SUBSTITUTE" in n for n in ev.ledger.notes)


class TestStorage:
    def test_conviction_history_preserves_the_purchase_score(
        self, tmp_path, market, config, peers, book, fx
    ):
        """Module H compares against the score at purchase, so history must be
        appended rather than updated."""
        store = Store(tmp_path / "gcfp.sqlite")
        first = run(100.0, 9.0, market, config, peers, book, fx)
        store.record_conviction(first.conviction, "DUAL", config.fingerprint,
                                scored_on=date(2026, 1, 1))
        later = run(150.0, 13.0, market, config, peers, book, fx)
        store.record_conviction(later.conviction, "DUAL", config.fingerprint,
                                scored_on=date(2026, 6, 1))

        at_purchase = store.conviction_at_purchase("STABLECO")
        latest = store.latest_conviction("STABLECO")
        assert at_purchase == pytest.approx(first.conviction.total)
        assert latest == pytest.approx(later.conviction.total)
        assert at_purchase > latest

    def test_single_anchor_rate_feeds_the_break_criterion(self, tmp_path, config):
        from gcfp.modules.d_conviction import ConvictionScore

        store = Store(tmp_path / "gcfp.sqlite")
        for i in range(3):
            store.record_conviction(
                ConvictionScore(f"S{i}", 65.0, [], "standard"),
                "SINGLE (C1 only)" if i < 2 else "DUAL",
                config.fingerprint,
            )
        assert store.single_anchor_rate() == pytest.approx(2 / 3)

    def test_audit_entries_persist_every_gate(
        self, tmp_path, market, config, peers, book, fx
    ):
        store = Store(tmp_path / "gcfp.sqlite")
        ev = run(120.0, 11.0, market, config, peers, book, fx)
        store.record_audit(ev.ledger)
        assert store.count("audit_entries") == len(ev.ledger.entries)
