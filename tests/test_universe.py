"""Universe construction, grouping statistics, and peer finding."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from gcfp.fixtures_probe import build_probe_fixture
from gcfp.modules.c_anchors import select_peers
from gcfp.types import TaxonomyLevel
from gcfp.universe import Universe, UniverseMember, build_universe, find_peers


@pytest.fixture(scope="module")
def adapter():
    return build_probe_fixture()


@pytest.fixture(scope="module")
def universe(adapter, config):
    return build_universe(adapter, sorted(adapter.companies.keys()), config)


class TestScreening:
    def test_every_exclusion_carries_a_reason(self, adapter, config):
        """A universe that shrinks unexpectedly should be explainable without
        re-running it."""
        tiny = UniverseMember(
            symbol="TINY", name="Tiny", market_cap=1e6, adv_3m_usd=1e5,
            sector="X", industry="Y", sub_industry=None,
            net_debt_to_ebitda=None, gross_margin_stdev=None,
            revenue_growth=None, beta=None,
        )
        assert not tiny.included or tiny.excluded_reason is None
        universe = build_universe(adapter, ["CAT", "NOSUCHTICKER"], config)
        assert any("NOSUCHTICKER" in u for u in universe.unreachable)

    def test_market_cap_floor_excludes_with_the_number_in_the_reason(
        self, adapter, config
    ):
        import dataclasses

        strict = dataclasses.replace(
            config,
            universe=dataclasses.replace(
                config.universe, min_market_cap_usd=1e12
            ),
        )
        universe = build_universe(adapter, ["CAT"], strict)
        member = universe.by_symbol("CAT")
        assert not member.included
        assert "market cap" in member.excluded_reason
        assert "below" in member.excluded_reason

    def test_unreachable_names_are_recorded_not_silently_dropped(
        self, adapter, config
    ):
        universe = build_universe(adapter, ["CAT", "SIVBQ"], config)
        assert len(universe.members) == 1
        assert len(universe.unreachable) == 1


class TestGroupStatistics:
    def test_medians_carry_their_computable_member_count(self, universe):
        """A2's ladder escalates on computable members, not nominal ones, so
        the count has to travel with the median."""
        market = universe.market_data()
        assert market.group_net_debt_ebitda_median
        for key, median in market.group_net_debt_ebitda_median.items():
            assert key in market.group_member_counts
            assert market.group_member_counts[key] >= 1
            assert key in market.group_taxonomy_level

    def test_a_member_without_the_metric_does_not_inflate_the_count(self, config):
        """The whole point of counting computable members."""
        members = [
            UniverseMember(f"S{i}", f"S{i}", 1e10, 1e8, "Sec", "Ind", None,
                           net_debt_to_ebitda=(2.0 if i < 3 else None),
                           gross_margin_stdev=None, revenue_growth=0.05, beta=1.0)
            for i in range(10)
        ]
        universe = Universe(as_of=date.today(), members=members)
        market = universe.market_data()
        assert market.group_member_counts["Ind"] == 3, (
            "counted members that had no computable metric"
        )

    def test_a_grouping_with_no_computable_members_gets_no_median(self, config):
        members = [
            UniverseMember(f"S{i}", f"S{i}", 1e10, 1e8, "Sec", "Ind", None,
                           net_debt_to_ebitda=None, gross_margin_stdev=None,
                           revenue_growth=0.05, beta=1.0)
            for i in range(5)
        ]
        market = Universe(as_of=date.today(), members=members).market_data()
        assert "Ind" not in market.group_net_debt_ebitda_median

    def test_market_data_preserves_the_risk_free_rate_it_was_given(self, universe):
        from gcfp.types import MarketData

        base = MarketData(risk_free_rate=0.042, risk_free_rate_date=date.today())
        merged = universe.market_data(base)
        assert merged.risk_free_rate == 0.042
        assert merged.group_net_debt_ebitda_median


class TestPeerFinding:
    def test_candidates_come_back_for_a_name_with_real_peers(
        self, universe, adapter, config
    ):
        subject = universe.by_symbol("CAT")
        peers = find_peers(universe, subject, config, adapter, "trailing_pe")
        assert len(peers) >= 4
        assert all(p.symbol != "CAT" for p in peers)

    def test_candidates_survive_c2s_own_screen(self, universe, adapter, config):
        subject = universe.by_symbol("CAT")
        peers = find_peers(universe, subject, config, adapter, "trailing_pe")
        kept, decisions = select_peers(
            subject.industry, subject.market_cap, subject.revenue_growth, peers, config
        )
        assert len(kept) >= config.anchors.peer_min
        assert len(decisions) == len(peers), "every candidate needs a decision"

    def test_the_near_monopoly_still_finds_no_genuine_peers(
        self, universe, adapter, config
    ):
        """The pre-filter must not manufacture peers C2 would have rejected."""
        subject = universe.by_symbol("TPL")
        peers = find_peers(universe, subject, config, adapter, "trailing_pe")
        kept, _ = select_peers(
            subject.industry, subject.market_cap, subject.revenue_growth, peers, config
        )
        assert len(kept) < config.anchors.peer_min

    def test_prefilter_only_narrows_on_criteria_c2_itself_tests(
        self, universe, adapter, config
    ):
        """A pre-filter that dropped names on some other basis would be an
        undocumented second screen."""
        subject = universe.by_symbol("CAT")
        peers = find_peers(universe, subject, config, adapter, "trailing_pe")
        low = config.anchors.peer_market_cap_low
        high = config.anchors.peer_market_cap_high
        for peer in peers:
            ratio = peer.market_cap / subject.market_cap
            assert low <= ratio <= high


class TestPersistence:
    def test_a_universe_round_trips_through_disk(self, universe, tmp_path):
        path = universe.save(tmp_path / "universe.json")
        loaded = Universe.load(path)
        assert loaded.as_of == universe.as_of
        assert len(loaded.members) == len(universe.members)
        assert loaded.by_symbol("CAT").market_cap == universe.by_symbol("CAT").market_cap

    def test_staleness_is_reported_against_a_max_age(self, universe):
        old = Universe(as_of=date.today() - timedelta(days=30), members=[])
        assert old.is_stale(max_age_days=7)
        assert not universe.is_stale(max_age_days=7)


class TestUniverseSampling:
    """--limit 500 screened A through ABX and reported its rejection rates as
    though they described the market. Nothing about a ticker's spelling
    predicts its fundamentals.
    """

    class FakeAdapter:
        name = "fake"

        def __init__(self, n=2000):
            self._tickers = {f"{chr(65 + i // 100)}{i:04d}": i for i in range(n)}

        def all_tickers(self):
            return dict(self._tickers)

    def test_a_limit_takes_a_sample_not_the_front_of_the_alphabet(self):
        from gcfp.universe import default_symbol_list

        picked = default_symbol_list(self.FakeAdapter(), limit=100)
        assert len(picked) == 100
        first_letters = {s[0] for s in picked}
        assert len(first_letters) > 5, (
            f"sample spans only {first_letters} — still an alphabetical slice"
        )

    def test_the_sample_is_stable_across_runs(self):
        """Two runs of the same size must cover the same names, or no two
        screens can be compared."""
        from gcfp.universe import default_symbol_list

        a = self.FakeAdapter()
        assert default_symbol_list(a, limit=50) == default_symbol_list(a, limit=50)

    def test_no_limit_returns_everything(self):
        from gcfp.universe import default_symbol_list

        assert len(default_symbol_list(self.FakeAdapter(n=300))) == 300

    def test_a_limit_beyond_the_index_returns_everything(self):
        from gcfp.universe import default_symbol_list

        assert len(default_symbol_list(self.FakeAdapter(n=300), limit=9999)) == 300

    def test_the_taxonomy_says_a_sample_is_a_sample(self):
        from gcfp.diagnostics import RejectionLedger

        ledger = RejectionLedger()
        ledger.record_unreachable("X", "no data")
        ledger.universe_total = 10_000
        text = " ".join(ledger.as_report_lines())
        assert "SAMPLE" in text and "10000" in text.replace(",", "")
