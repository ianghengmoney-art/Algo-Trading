"""End-to-end: the pipeline, the reports, and the state store."""

from __future__ import annotations

from datetime import date

from gcfp.cli import main
from gcfp.config import CORE_STABLE
from gcfp.data.coverage import FIXTURE_PROBE_TARGETS, render_coverage_report, run_coverage_probe
from gcfp.engine import screen
from gcfp.modules.f_sizing import Portfolio, allocate
from gcfp.modules.i_expectations import EXPECTATION_STATEMENT
from gcfp.reports.render import weekly_report
from gcfp.state.db import Store

from conftest import SUBJECTS


def test_screen_produces_buys_across_multiple_classifications(adapter, params, gate, as_of):
    result = screen(adapter, SUBJECTS, params, gate, as_of=as_of)
    passers = result.passers()
    assert passers, "expected at least one passer from the fixture universe"
    classifications = {a.classification for a in passers}
    assert len(classifications) >= 2


def test_every_buy_cleared_all_three_conditions(adapter, params, gate, as_of):
    result = screen(adapter, SUBJECTS, params, gate, as_of=as_of)
    for assessment in result.passers():
        conditions = assessment.trigger.conditions
        assert all(c["met"] for c in conditions.values())
        assert assessment.conviction.total >= params.triggers.min_conviction_to_buy
        assert assessment.triangulation.both_confirm_undervalued


def test_health_failures_never_reach_valuation(adapter, params, gate, as_of):
    result = screen(adapter, SUBJECTS, params, gate, as_of=as_of)
    failures = result.health_failures()
    assert failures
    for assessment in failures:
        assert assessment.valuation is None
        assert assessment.triangulation is None
        assert assessment.conviction is None


def test_report_opens_with_utilisation_gap_and_expectation(adapter, params, gate, as_of):
    result = screen(adapter, SUBJECTS, params, gate, as_of=as_of)
    portfolio = Portfolio(total_value=1_000_000.0, ballast_value=300_000.0, positions=[])
    allocator = allocate(
        [
            (a.symbol, a.classification, a.candidate.profile.sector, a.conviction.total)
            for a in result.passers()
        ],
        portfolio,
        params,
    )
    text = weekly_report(result, params, allocator, portfolio)

    assert "SLEEVE UTILISATION VS TARGETS" in text
    assert "INDEX/BALLAST BELOW TARGET" in text
    assert EXPECTATION_STATEMENT in text
    assert "ALERT ONLY" in text
    # Both anchor readings appear side by side for every passer.
    assert "C1 own-history" in text and "C2 peers" in text


def test_report_names_the_method_with_every_fair_value(adapter, params, gate, as_of):
    result = screen(adapter, SUBJECTS, params, gate, as_of=as_of)
    text = weekly_report(result, params)
    for assessment in result.passers():
        assert f"via {assessment.valuation.label}" in text


def test_thesis_line_is_demanded_before_execution(adapter, params, gate, as_of):
    result = screen(adapter, SUBJECTS, params, gate, as_of=as_of)
    text = weekly_report(result, params)
    assert "NOT SET -- required before any execution" in text


def test_empty_screen_says_zero_passers_is_the_system_working(adapter, params, gate, as_of):
    result = screen(adapter, ["BADQUAL", "STALECO", "FLAGGED"], params, gate, as_of=as_of)
    text = weekly_report(result, params)
    assert "NO PASSERS THIS WEEK" in text
    assert "Unfilled slots stay in cash" in text


def test_coverage_probe_covers_every_classification_path(adapter, params, as_of):
    report = run_coverage_probe(adapter, params, FIXTURE_PROBE_TARGETS, as_of=as_of)
    paths = {r.classification_path for r in report.results}
    assert len(paths) >= 5
    modules = set(report.by_module())
    assert {"A1", "A2", "A3", "A4", "A5", "B", "C1", "C2"} <= modules
    assert "POINT-IN-TIME LIMITATIONS" in render_coverage_report(report)


def test_state_round_trip(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.upsert_holding("MATURE", CORE_STABLE, "Industrials", date(2026, 6, 30), 100_000.0, 105_000.0, 86.0, True)
    store.record_conviction("MATURE", date(2026, 6, 30), 86.0, {"health_margin": 20.0})
    store.record_alert(date(2026, 6, 30), "MATURE", "BUY", CORE_STABLE, 86.0, 172.0, 70.0)
    assert [r["symbol"] for r in store.open_holdings()] == ["MATURE"]
    assert store.conviction_at_purchase("MATURE") == 86.0
    assert len(store.alerts_for("MATURE")) == 1
    store.close()


def test_cli_screen_runs_and_writes_a_report(tmp_path, capsys):
    out = tmp_path / "reports"
    code = main(
        ["--adapter", "fixtures", "screen", "--as-of", "2026-06-30", "--out", str(out)]
    )
    assert code == 0
    written = list(out.glob("weekly-*.txt"))
    assert written and "ALERT ONLY" in written[0].read_text()


def test_cli_records_intent_then_logs_a_deviating_execution(tmp_path, capsys):
    db = str(tmp_path / "state.sqlite3")
    assert main([
        "--db", db, "record", "--symbol", "MATURE", "--classification", CORE_STABLE,
        "--method", "B1", "--conviction", "86", "--fair-value", "172",
        "--sleeve-pct", "8", "--intended-value", "100000",
        "--thesis", "operating margin below 12% for two consecutive quarters",
        "--on", "2026-06-30",
    ]) == 0

    # Without a reason, a large deviation is refused.
    assert main([
        "--db", db, "log-execution", "--symbol", "MATURE",
        "--actual-value", "160000", "--on", "2026-07-01",
    ]) == 1

    assert main([
        "--db", db, "log-execution", "--symbol", "MATURE", "--actual-value", "160000",
        "--reason", "conviction on the day", "--on", "2026-07-01",
    ]) == 0
    assert "SIZE DEVIATION" in capsys.readouterr().out


def test_cli_capabilities_surfaces_blocking_notices(capsys):
    assert main(["--adapter", "fixtures", "capabilities"]) == 0
    out = capsys.readouterr().out
    assert "capabilities:" in out
