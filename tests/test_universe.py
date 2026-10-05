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


class TestPriceCache:
    """An interrupted run must resume, not start over.

    SEC filings were cached but prices were not, so a laptop going to sleep
    halfway through a 1,400-company probe meant downloading every price
    series again.
    """

    class Counting:
        name = "counting"

        def __init__(self, fail=False):
            self.calls = 0
            self.fail = fail

        def get_prices(self, symbol, start, end):
            from datetime import timedelta

            from gcfp.data.adapter import DataUnavailable
            from gcfp.types import PricePoint

            self.calls += 1
            if self.fail:
                raise DataUnavailable("prices", "no such ticker")
            out, d = [], end
            while d >= start:
                out.append(PricePoint(price_date=d, close=100.0, volume=1e6))
                d -= timedelta(days=1)
            return out

        def get_splits(self, symbol, start, end):
            return []

        def get_index_level(self, symbol):
            return 4.5

    def cached(self, tmp_path, fail=False):
        from gcfp.data.prices import CachedPriceSource

        inner = self.Counting(fail=fail)
        return CachedPriceSource(inner=inner, cache_dir=tmp_path), inner

    def test_a_second_run_reads_from_disk(self, tmp_path):
        from datetime import timedelta

        today = date.today()
        first, inner = self.cached(tmp_path)
        first.get_prices("CAT", today - timedelta(days=30), today)

        # A fresh object, as after a restart: only the files survive.
        second, inner2 = self.cached(tmp_path)
        bars = second.get_prices("CAT", today - timedelta(days=30), today)
        assert inner2.calls == 0, "a restarted run downloaded again"
        assert len(bars) == 31

    def test_a_longer_window_is_fetched_once_then_served(self, tmp_path):
        from datetime import timedelta

        today = date.today()
        src, inner = self.cached(tmp_path)
        src.get_prices("CAT", today - timedelta(days=30), today)
        src.get_prices("CAT", today - timedelta(days=3000), today)
        src.get_prices("CAT", today - timedelta(days=500), today - timedelta(days=100))
        # The first miss fetches twelve years through today, so both later
        # windows are already on disk.
        assert inner.calls == 1

    def test_a_backtest_walking_forward_downloads_each_symbol_once(self, tmp_path):
        """Each rebalance asks for one month more than the last. The cache
        used to stop at the requested end, so every month was a miss and
        every price history was downloaded again 129 times."""
        from datetime import timedelta

        src, inner = self.cached(tmp_path)
        as_of = date(2015, 1, 31)
        for _ in range(24):
            history = src.get_prices("CAT", as_of - timedelta(days=2922), as_of)
            recent = src.get_prices("CAT", as_of - timedelta(days=14), as_of)
            assert max(p.price_date for p in [*history, *recent]) <= as_of, "lookahead"
            assert min(p.price_date for p in history) >= as_of - timedelta(days=2922)
            as_of += timedelta(days=30)
        assert inner.calls == 1

    def test_an_unpriceable_ticker_is_not_retried_the_same_day(self, tmp_path):
        from datetime import timedelta

        from gcfp.data.adapter import DataUnavailable

        today = date.today()
        src, inner = self.cached(tmp_path, fail=True)
        for _ in range(2):
            with pytest.raises(DataUnavailable):
                src.get_prices("DEAD", today - timedelta(days=30), today)
        assert inner.calls == 1

    def test_a_run_crossing_midnight_does_not_re_ask_about_dead_tickers(self, tmp_path):
        """A "no data" answer stood for the calendar day only, so a backtest
        that crossed midnight UTC re-fetched every dead ticker at once."""
        import json
        from datetime import timedelta

        from gcfp.data.adapter import DataUnavailable

        today = date.today()
        src, inner = self.cached(tmp_path, fail=True)
        with pytest.raises(DataUnavailable):
            src.get_prices("DEAD", today - timedelta(days=30), today)
        for age, expected_calls in ((1, 0), (4, 1)):
            path = tmp_path / "DEAD.json"
            entry = json.loads(path.read_text())
            entry["fetched_on"] = (today - timedelta(days=age)).isoformat()
            path.write_text(json.dumps(entry))
            src, inner = self.cached(tmp_path, fail=True)
            with pytest.raises(DataUnavailable):
                src.get_prices("DEAD", date(2015, 1, 1), date(2015, 2, 1))
            assert inner.calls == expected_calls, f"age {age} days"

    def test_a_series_reaching_today_is_refreshed_on_a_later_day(self, tmp_path):
        """Yesterday's file must not stand in for today's close."""
        import json
        from datetime import timedelta

        today = date.today()
        src, inner = self.cached(tmp_path)
        src.get_prices("CAT", today - timedelta(days=30), today)
        path = tmp_path / "CAT.json"
        stale = json.loads(path.read_text())
        stale["fetched_on"] = (today - timedelta(days=1)).isoformat()
        path.write_text(json.dumps(stale))

        # A later day is a later run: a fresh process reading the file.
        src, inner = self.cached(tmp_path)
        src.get_prices("CAT", today - timedelta(days=30), today)
        assert inner.calls == 1

    def test_splits_are_fetched_once_and_filtered_by_window(self, tmp_path):
        from datetime import timedelta

        from gcfp.types import CorporateAction, CorporateActionType

        today = date.today()
        src, inner = self.cached(tmp_path)
        split = CorporateAction(CorporateActionType.SPLIT, today - timedelta(days=400), ratio=2.0)
        calls = []
        inner.get_splits = lambda symbol, start, end: calls.append(1) or [split]

        for months in range(12):
            end = today - timedelta(days=30 * months)
            src.get_splits("CAT", end - timedelta(days=365), end)
        assert len(calls) == 1, "the feed was asked for splits at every rebalance"
        assert src.get_splits("CAT", today - timedelta(days=100), today) == ()
        assert src.get_splits("CAT", today - timedelta(days=800), today) == (split,)

    def test_reports_still_name_the_underlying_feed(self, tmp_path):
        src, _ = self.cached(tmp_path)
        assert src.name == "counting"


class TestStructuralExclusions:
    """Operator decision after §18: REITs and 20-F filers cannot be valued on
    this data source, so they are excluded up front with the reason named."""

    @staticmethod
    def build(config=None, **profile_kw):
        from dataclasses import replace as dc_replace

        from gcfp.config import Config
        from gcfp.universe import build_universe
        from tests.conftest import build_company, stable_profile

        data = build_company(profile=stable_profile(**profile_kw))

        class Stub:
            name = "stub"

            def load_company(self, symbol, **kw):
                return data

        return build_universe(Stub(), ["X"], config or Config()).members[0]

    def test_a_reit_is_excluded_with_the_reason_named(self):
        member = self.build(is_reit=True)
        assert not member.included
        assert "REIT" in member.excluded_reason and "AFFO" in member.excluded_reason

    def test_a_20f_filer_is_excluded_with_the_reason_named(self):
        from gcfp.types import ReportingFrequency

        member = self.build(reporting_frequency=ReportingFrequency.SEMIANNUAL)
        assert not member.included
        assert "foreign filer" in member.excluded_reason

    def test_an_ordinary_company_is_unaffected(self):
        assert self.build().included

    def test_the_exclusions_can_be_switched_off(self):
        """Reversible when a data source that supplies AFFO is added."""
        from dataclasses import replace as dc_replace

        from gcfp.config import Config

        cfg = Config()
        cfg = dc_replace(
            cfg,
            universe=dc_replace(cfg.universe, exclude_reits=False, exclude_foreign_filers=False),
        )
        assert self.build(config=cfg, is_reit=True).included

    def test_an_old_universe_snapshot_still_loads(self, tmp_path):
        """Snapshots saved before is_foreign_filer existed must not break."""
        import json

        from gcfp.universe import Universe

        path = tmp_path / "u.json"
        path.write_text(json.dumps({"as_of": "2026-09-01", "members": [{
            "symbol": "X", "name": "X", "market_cap": 1e9, "adv_3m_usd": 1e7,
            "sector": "S", "industry": "I", "sub_industry": None,
            "net_debt_to_ebitda": 1.0, "gross_margin_stdev": None,
            "revenue_growth": 0.05, "beta": 1.0,
        }]}))
        assert Universe.load(path).members[0].is_foreign_filer is False


class TestPriceSourceBreaker:
    def test_an_unreachable_source_is_skipped_after_three_failures(self):
        from gcfp.data.adapter import DataUnavailable
        from gcfp.data.prices import FallbackPriceSource, PriceSource
        from gcfp.types import PricePoint

        class Dead(PriceSource):
            name = "dead"
            calls = 0
            def get_prices(self, symbol, start, end):
                Dead.calls += 1
                raise DataUnavailable("prices", "dead exhausted retries: timeout")

        class Alive(PriceSource):
            name = "alive"
            def get_prices(self, symbol, start, end):
                return (PricePoint(end, 1.0),)

        source = FallbackPriceSource(sources=(Dead(), Alive()))
        for _ in range(10):
            assert source.get_prices("X", date(2020, 1, 1), date(2020, 2, 1))
        assert Dead.calls == 3

    def test_an_unknown_symbol_does_not_count_against_the_source(self):
        from gcfp.data.adapter import DataUnavailable
        from gcfp.data.prices import FallbackPriceSource, PriceSource

        class Picky(PriceSource):
            name = "picky"
            calls = 0
            def get_prices(self, symbol, start, end):
                Picky.calls += 1
                raise DataUnavailable("prices", f"no data for {symbol}")

        source = FallbackPriceSource(sources=(Picky(),))
        for _ in range(5):
            with pytest.raises(DataUnavailable):
                source.get_prices("X", date(2020, 1, 1), date(2020, 2, 1))
        assert Picky.calls == 5


class TestWiderPeerGroup:
    """Option 3: when an industry is thin, look in its SIC major group."""

    @staticmethod
    def member(symbol, code, industry):
        from gcfp.universe import UniverseMember

        return UniverseMember(
            symbol=symbol, name=symbol, market_cap=1e9, adv_3m_usd=1e7,
            sector="Manufacturing", industry=industry, sub_industry=None,
            net_debt_to_ebitda=None, gross_margin_stdev=None,
            revenue_growth=0.05, beta=None, industry_code=code,
        )

    def test_the_major_group_key(self):
        from gcfp.universe import industry_group_key

        assert industry_group_key(self.member("A", "3531", "x")) == "SIC 35xx"
        assert industry_group_key(self.member("A", None, "x")) is None

    def test_peers_come_from_sibling_industries(self):
        from unittest.mock import Mock

        from gcfp.config import Config
        from gcfp.universe import Universe, find_peers, industry_group_key

        subject = self.member("CAT", "3531", "CONSTRUCTION MACHINERY")
        siblings = [self.member(f"S{i}", "3533", "OIL FIELD MACHINERY") for i in range(5)]
        stranger = self.member("BANK", "6021", "NATIONAL BANKS")
        universe = Universe(as_of=date(2020, 1, 1), members=[subject, *siblings, stranger])
        adapter = Mock()
        adapter.load_company = Mock(return_value=Mock(current_price=None))

        narrow = find_peers(universe, subject, Config(), adapter, "trailing_pe")
        wide = find_peers(universe, subject, Config(), adapter, "trailing_pe",
                          grouping=industry_group_key)
        assert narrow == []
        assert {c.symbol for c in wide} == {s.symbol for s in siblings}
        assert all(c.group == "SIC 35xx" for c in wide)
