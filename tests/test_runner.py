"""Run assembly and the CLI entry points.

Smoke-level: the CLIs are thin, and what matters is that the wiring holds
together and that the free stack's explicit downgrades are actually applied.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from gcfp.classification import Classification
from gcfp.config import DEFAULT_CONFIG
from gcfp.fixtures_probe import build_probe_fixture
from gcfp.modules import c_anchors
from gcfp.modules.f_sizing import Bucket, PortfolioState
from gcfp.runner import assemble, build_inputs, free_stack_config, load_or_build_universe
from gcfp.universe import Universe, build_universe

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def adapter():
    return build_probe_fixture()


@pytest.fixture(scope="module")
def universe(adapter, config):
    return build_universe(adapter, sorted(adapter.companies), config)


class TestFreeStackConfig:
    def test_core_growth_anchor_moves_to_a_trailing_multiple(self):
        """A source without analyst estimates cannot compute a forward P/E, and
        the substitution has to be explicit rather than a silent degradation."""
        free = free_stack_config(DEFAULT_CONFIG)
        assert c_anchors.anchor_multiple_for(Classification.CORE_GROWTH, free) == "trailing_pe"
        assert (
            c_anchors.anchor_multiple_for(Classification.CORE_GROWTH, DEFAULT_CONFIG)
            == "forward_pe"
        )

    def test_the_downgrade_is_recorded_in_the_config_fingerprint(self):
        """A parameter change must be visible as a fingerprint change, so a
        stored recommendation ties back to the rules that produced it."""
        assert free_stack_config(DEFAULT_CONFIG).fingerprint != DEFAULT_CONFIG.fingerprint

    def test_other_classifications_are_untouched(self):
        free = free_stack_config(DEFAULT_CONFIG)
        for classification in (Classification.CORE_STABLE, Classification.REIT):
            assert c_anchors.anchor_multiple_for(
                classification, free
            ) == c_anchors.ANCHOR_MULTIPLE[classification]


class TestAssemble:
    def test_grouping_statistics_reach_market_data(self, adapter, universe, config):
        context = assemble(
            adapter, config, universe,
            PortfolioState(total_value=100_000.0),
        )
        assert context.market.group_net_debt_ebitda_median
        assert context.market.group_member_counts

    def test_a_supplied_fx_rate_overrides_the_adapters(self, adapter, universe, config):
        context = assemble(
            adapter, config, universe,
            PortfolioState(total_value=100_000.0),
            fx_rates={"USDSGD": 1.42},
        )
        assert context.fx.rates["USDSGD"] == 1.42

    def test_candidate_symbols_are_the_included_members(self, adapter, universe, config):
        context = assemble(adapter, config, universe, PortfolioState(total_value=1.0))
        assert set(context.candidate_symbols) == {m.symbol for m in universe.included}


class TestBuildInputs:
    def test_peers_come_from_the_universe_not_a_vendor_list(
        self, adapter, universe, config
    ):
        inputs = build_inputs(adapter, universe, config, ["CAT"])
        assert "CAT" in inputs
        assert len(inputs["CAT"].peer_candidates) >= 4
        assert inputs["CAT"].subject_group == "Agricultural - Machinery"

    def test_the_anchor_multiple_matches_the_assigned_classification(
        self, adapter, universe, config
    ):
        inputs = build_inputs(adapter, universe, config, ["CAT", "JPM"])
        # CAT is CORE-STABLE (trailing P/E); JPM is a bank (P/B).
        assert inputs["CAT"].current_multiple is not None
        assert inputs["JPM"].current_multiple is not None
        assert inputs["CAT"].current_multiple != inputs["JPM"].current_multiple

    def test_an_unclassifiable_name_is_skipped_rather_than_guessed(
        self, adapter, universe, config
    ):
        inputs = build_inputs(adapter, universe, config, ["NOSUCHTICKER"])
        assert "NOSUCHTICKER" not in inputs


class TestUniverseCache:
    def test_a_fresh_cache_is_reused(self, adapter, universe, config, tmp_path):
        path = tmp_path / "universe.json"
        universe.save(path)
        before = path.stat().st_mtime_ns
        reused = load_or_build_universe(adapter, config, path, max_age_days=7)
        assert path.stat().st_mtime_ns == before, "rebuilt despite a fresh cache"
        assert len(reused.members) == len(universe.members)

    def test_a_stale_cache_is_rebuilt(self, adapter, config, tmp_path):
        from datetime import timedelta

        path = tmp_path / "universe.json"
        Universe(as_of=date.today() - timedelta(days=60), members=[]).save(path)
        rebuilt = load_or_build_universe(adapter, config, path, max_age_days=7)
        assert rebuilt.as_of == date.today()
        assert rebuilt.members, "a rebuild produced an empty universe"


class TestCommandLine:
    """The scripts must at least run, print a report, and exit cleanly."""

    def _run(self, script: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / script), *args],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=180,
        )

    def test_screen_runs_against_fixtures(self, tmp_path):
        result = self._run(
            "run_screen.py", "--source", "fixture",
            "--db", str(tmp_path / "g.sqlite"),
            "--reports", str(tmp_path / "reports"),
        )
        assert result.returncode == 0, result.stderr
        assert "GCFP v4" in result.stdout
        assert "MODULE I — EXPECTATION STATEMENT" in result.stdout
        assert "Nothing executes." in result.stdout
        assert list((tmp_path / "reports").glob("*new-passers.txt"))

    def test_monitor_exits_cleanly_with_no_positions(self, tmp_path):
        result = self._run(
            "run_monitor.py", "--source", "fixture",
            "--db", str(tmp_path / "empty.sqlite"),
            "--reports", str(tmp_path / "reports"),
        )
        assert result.returncode == 0
        assert "No open positions" in result.stderr

    def test_probe_exits_nonzero_when_a_stop_condition_trips(self, tmp_path):
        result = self._run("data_feasibility_probe.py", "--source", "fixture")
        assert result.returncode == 1, "a tripped stop condition must fail the run"
        assert "STOP CONDITIONS" in result.stdout
        assert "report all findings before" in result.stderr

    def test_screen_requires_a_user_agent_for_edgar(self, tmp_path):
        result = self._run("run_screen.py", "--source", "edgar")
        assert result.returncode != 0
        assert "user-agent" in result.stderr.lower()

    @pytest.mark.parametrize("script", [
        "data_feasibility_probe.py", "run_screen.py", "run_monitor.py",
    ])
    def test_every_cli_accepts_the_edgar_source(self, script):
        """The free stack is the documented default, so a CLI that does not
        offer it sends the operator to a source they were told not to need.

        This regressed once: the probe was written before the EDGAR adapter
        existed and still listed only fmp and fixture, so following the README
        produced an argparse error.
        """
        result = self._run(script, "--source", "edgar")
        combined = (result.stdout + result.stderr).lower()
        assert "invalid choice" not in combined, (
            f"{script} does not accept --source edgar"
        )
        # Where the CLI actually proceeds it must ask for the user-agent.
        # run_monitor legitimately exits first when the store is empty: there
        # is no reason to demand credentials in order to do nothing.
        assert "user-agent" in combined or "no open positions" in combined

    @pytest.mark.parametrize("script", [
        "data_feasibility_probe.py", "run_screen.py", "run_monitor.py",
    ])
    def test_every_cli_runs_offline(self, script, tmp_path):
        """Every entry point must work with no network, so the install can be
        verified before spending an hour on a live run."""
        source = "fixture"
        args = [script, "--source", source]
        if script != "data_feasibility_probe.py":
            args += ["--db", str(tmp_path / f"{script}.sqlite"),
                     "--reports", str(tmp_path / "reports")]
        result = self._run(*args)
        combined = (result.stdout + result.stderr).lower()
        assert "invalid choice" not in combined
        assert "traceback" not in combined


class TestSymbolListHandoff:
    """The screen produces the list the backtest consumes.

    The old error said "--symbols is required" and stopped there, which left
    the operator to invent a candidate list by hand — and a list invented
    today is survivorship bias in its purest form.
    """

    @staticmethod
    def backtest_module():
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "rb", Path(__file__).resolve().parent.parent / "scripts/run_backtest.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_comments_and_blank_lines_are_ignored(self, tmp_path):
        f = tmp_path / "syms.txt"
        f.write_text("# a comment\n\nCAT\n  DE  # trailing\n\nnvda\n")
        assert self.backtest_module().read_symbol_file(f) == ["CAT", "DE", "NVDA"]

    def test_a_missing_file_fails_loudly(self, tmp_path):
        with pytest.raises(SystemExit):
            self.backtest_module().read_symbol_file(tmp_path / "nope.txt")

    def test_a_file_of_only_comments_is_not_an_empty_backtest(self, tmp_path):
        """Silently running zero symbols would produce an empty report that
        looks like a result."""
        f = tmp_path / "syms.txt"
        f.write_text("# nothing but warnings\n#\n")
        with pytest.raises(SystemExit):
            self.backtest_module().read_symbol_file(f)

    def test_the_written_list_carries_the_survivorship_warning(self, tmp_path):
        """The warning has to travel with the file — whoever runs the backtest
        may not be whoever ran the screen."""
        import subprocess
        import sys
        from pathlib import Path

        out = tmp_path / "syms.txt"
        root = Path(__file__).resolve().parent.parent
        subprocess.run(
            [sys.executable, str(root / "scripts/run_screen.py"),
             "--source", "fixture", "--symbols-out", str(out),
             "--db", str(tmp_path / "t.sqlite"),
             "--reports", str(tmp_path / "reports")],
            cwd=root, capture_output=True, check=True,
        )
        body = out.read_text()
        assert "SURVIVORSHIP WARNING" in body
        assert "CAT" in body
