"""Gate A4's missing half: EDGAR filing flags, and composing sources."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import requests

from gcfp.data.composite import CompositeAdapter
from gcfp.data.edgar import EdgarFilingFlags
from gcfp.data.quantconnect import QCDataAdapter
from gcfp.modules.a_health import evaluate_health, gate_a4_red_flags, build_trailing_window
from gcfp.types import FilingFlags, Verdict
from qc_fakes import make_fundamental

UA = "GCFP Research research@example.com"


def submissions(rows):
    """Shape of data.sec.gov/submissions/CIK##########.json."""
    return {
        "name": "Test Filer",
        "filings": {
            "recent": {
                "form": [r[0] for r in rows],
                "filingDate": [r[1] for r in rows],
                "items": [r[2] for r in rows],
                "accessionNumber": [f"acc-{i}" for i in range(len(rows))],
            }
        },
    }


def edgar():
    return EdgarFilingFlags(user_agent=UA)


# -- parsing the structured signals -----------------------------------------


def test_8k_item_402_is_a_restatement():
    payload = submissions([("8-K", "2026-03-01", "4.02,9.01")])
    flags = edgar().parse_submissions(payload, as_of=date(2026, 6, 30))
    assert flags.restatement_within_lookback is True
    assert any("4.02" in e for e in flags.evidence)


def test_8k_item_401_is_an_auditor_change():
    payload = submissions([("8-K", "2026-03-01", "4.01")])
    flags = edgar().parse_submissions(payload, as_of=date(2026, 6, 30))
    assert flags.auditor_change_within_lookback is True


def test_nt_forms_are_late_filings():
    for form in ("NT 10-K", "NT 10-Q"):
        payload = submissions([(form, "2026-05-01", "")])
        flags = edgar().parse_submissions(payload, as_of=date(2026, 6, 30))
        assert flags.delayed_filing is True, form


def test_ordinary_filings_are_not_flags():
    payload = submissions([("10-K", "2026-02-01", ""), ("10-Q", "2026-04-01", ""), ("4", "2026-04-02", "")])
    flags = edgar().parse_submissions(payload, as_of=date(2026, 6, 30))
    assert flags.restatement_within_lookback is False
    assert flags.auditor_change_within_lookback is False
    assert flags.delayed_filing is False


def test_an_8k_without_the_relevant_item_is_not_a_flag():
    payload = submissions([("8-K", "2026-03-01", "2.02,7.01")])  # results of operations
    flags = edgar().parse_submissions(payload, as_of=date(2026, 6, 30))
    assert flags.restatement_within_lookback is False
    assert flags.auditor_change_within_lookback is False


# -- lookback windows -------------------------------------------------------


def test_auditor_change_outside_twelve_months_does_not_count():
    payload = submissions([("8-K", "2020-01-01", "4.01")])
    flags = edgar().parse_submissions(payload, as_of=date(2026, 6, 30))
    assert flags.auditor_change_within_lookback is False


def test_restatement_inside_three_years_counts_and_outside_does_not():
    inside = submissions([("8-K", "2024-06-30", "4.02")])
    outside = submissions([("8-K", "2019-01-01", "4.02")])
    assert edgar().parse_submissions(inside, as_of=date(2026, 6, 30)).restatement_within_lookback is True
    assert edgar().parse_submissions(outside, as_of=date(2026, 6, 30)).restatement_within_lookback is False


def test_filings_after_the_as_of_date_are_ignored():
    """Point-in-time discipline: a backtest must not read tomorrow's 8-K."""
    payload = submissions([("8-K", "2026-09-01", "4.02")])
    flags = edgar().parse_submissions(payload, as_of=date(2026, 6, 30))
    assert flags.restatement_within_lookback is False


# -- absence is never a clean bill of health --------------------------------


def test_going_concern_is_not_sourced_from_edgar():
    """Text-searching 10-Ks for the phrase over-flags; left to the opinion code."""
    payload = submissions([("10-K", "2026-02-01", "")])
    assert edgar().parse_submissions(payload, as_of=date(2026, 6, 30)).going_concern_language is None


def test_empty_submissions_answer_nothing():
    flags = edgar().parse_submissions({}, as_of=date(2026, 6, 30))
    assert flags.restatement_within_lookback is None
    assert flags.delayed_filing is None


def test_network_failure_does_not_read_as_clean():
    class Failing(EdgarFilingFlags):
        def fetch_submissions(self, cik):
            raise requests.RequestException("connection refused")

    flags = Failing(user_agent=UA).get_flags("0000320193")
    assert flags.restatement_within_lookback is None
    assert any("unavailable" in e for e in flags.evidence)


def test_missing_cik_is_reported_not_assumed():
    flags = edgar().get_flags(None)
    assert flags.restatement_within_lookback is None
    assert any("no CIK" in e for e in flags.evidence)


def test_user_agent_is_mandatory():
    """SEC refuses anonymous requests; failing early beats being blocked."""
    with pytest.raises(ValueError, match="User-Agent"):
        EdgarFilingFlags(user_agent="")
    with pytest.raises(ValueError, match="User-Agent"):
        EdgarFilingFlags(user_agent="some-bot")  # no contact address


# -- merge semantics --------------------------------------------------------


def test_unknown_never_overwrites_known():
    known = FilingFlags(delayed_filing=True, going_concern_language=False)
    assert known.merge(FilingFlags()).delayed_filing is True
    assert known.merge(FilingFlags()).going_concern_language is False


def test_overlay_wins_where_both_have_an_opinion():
    base = FilingFlags(auditor_change_within_lookback=False)
    overlay = FilingFlags(auditor_change_within_lookback=True)
    assert base.merge(overlay).auditor_change_within_lookback is True


def test_evidence_from_both_sources_is_kept():
    merged = FilingFlags(evidence=("QC: x",)).merge(FilingFlags(evidence=("EDGAR: y",)))
    assert merged.evidence == ("QC: x", "EDGAR: y")


# -- audit opinion ----------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [("UQ", False), ("AO", True), ("DS", True), ("UE", None), ("QM", None), ("UA", None)],
)
def test_audit_opinion_maps_only_the_unambiguous_ends(code, expected):
    assert QCDataAdapter._audit_opinion([_with_opinion(code)])[1] is expected


def _with_opinion(code):
    f = make_fundamental()
    f.financial_statements.auditor_report_status = _Field(code)
    return f


class _Field:
    def __init__(self, value):
        self._value = value

    def has_period_value(self, period):
        return period == "12M"

    def get_period_value(self, period):
        return self._value

    @property
    def has_value(self):
        return True

    @property
    def value(self):
        return self._value


# -- composition ------------------------------------------------------------


def _qc_adapter(rows, as_of=date(2026, 6, 30)):
    """One subject plus enough same-sector names for gate A2 to take a median."""
    universe = {str(rows[-1].symbol): rows[-1]}
    for i in range(5):
        peer = make_fundamental(symbol=f"PEER{i}", revenue=8_000_000_000.0 + i * 1e8)
        universe[f"PEER{i}"] = peer
    return QCDataAdapter(
        universe,
        history_provider=lambda s, y: rows if s == "TEST" else [universe[s]],
        as_of=as_of,
    )


class StubEdgar:
    def __init__(self, flags):
        self._flags = flags

    def get_flags(self, cik, as_of=None):
        return self._flags


def _history(opinion="UQ", years=6):
    rows = []
    for i in range(years):
        f = make_fundamental(
            end_time=date(2026, 6, 30) - timedelta(days=365 * i),
            period_end=date(2025 - i, 12, 31),
            revenue=9_000_000_000.0 * (0.93 ** i),
        )
        f.financial_statements.auditor_report_status = _Field(opinion)
        rows.append(f)
    return list(reversed(rows))


def test_composite_carries_the_cik_through_to_the_overlay():
    rows = _history()
    seen = {}

    class Recording:
        def get_flags(self, cik, as_of=None):
            seen["cik"] = cik
            return FilingFlags()

    CompositeAdapter(_qc_adapter(rows), Recording()).get_filing_flags("TEST")
    assert seen["cik"] == "0000018230"


def test_a4_is_a_data_gap_on_quantconnect_alone(params):
    """The state before this overlay existed."""
    adapter = _qc_adapter(_history())
    result = evaluate_health(adapter.load_candidate("TEST"), params, as_of=date(2026, 6, 30))
    assert result.gate("A4").verdict is Verdict.DATA_GAP


def test_a4_passes_once_edgar_supplies_the_missing_flags(params):
    """The headline: a candidate can reach a full PASS for the first time."""
    clean = FilingFlags(
        restatement_within_lookback=False,
        auditor_change_within_lookback=False,
        delayed_filing=False,
        evidence=("EDGAR: no 4.02, 4.01 or NT filings in the lookback windows",),
    )
    adapter = CompositeAdapter(_qc_adapter(_history(opinion="UQ")), StubEdgar(clean))
    candidate = adapter.load_candidate("TEST")

    outcome = gate_a4_red_flags(candidate, build_trailing_window(candidate), params, None)
    assert outcome.verdict is Verdict.PASS, outcome.reason

    result = evaluate_health(candidate, params, as_of=date(2026, 6, 30))
    assert result.verdict is Verdict.PASS, result.reasons
    assert result.classification is not None


def test_a_restatement_still_fails_the_gate(params):
    flagged = FilingFlags(
        restatement_within_lookback=True,
        auditor_change_within_lookback=False,
        delayed_filing=False,
    )
    adapter = CompositeAdapter(_qc_adapter(_history()), StubEdgar(flagged))
    candidate = adapter.load_candidate("TEST")
    outcome = gate_a4_red_flags(candidate, build_trailing_window(candidate), params, None)
    assert outcome.verdict is Verdict.FAIL
    assert "restatement" in outcome.reason


def test_an_ambiguous_audit_opinion_keeps_the_gate_open(params):
    """UE covers going concern and unrelated matters; guessing is refused."""
    clean = FilingFlags(
        restatement_within_lookback=False,
        auditor_change_within_lookback=False,
        delayed_filing=False,
    )
    adapter = CompositeAdapter(_qc_adapter(_history(opinion="UE")), StubEdgar(clean))
    candidate = adapter.load_candidate("TEST")
    outcome = gate_a4_red_flags(candidate, build_trailing_window(candidate), params, None)
    assert outcome.verdict is Verdict.DATA_GAP
    assert "going concern" in outcome.reason


def test_edgar_outage_degrades_rather_than_passing(params):
    outage = FilingFlags(evidence=("EDGAR: unavailable (connection refused)",))
    adapter = CompositeAdapter(_qc_adapter(_history()), StubEdgar(outage))
    candidate = adapter.load_candidate("TEST")
    outcome = gate_a4_red_flags(candidate, build_trailing_window(candidate), params, None)
    assert outcome.verdict is Verdict.DATA_GAP


def test_composite_passes_through_to_the_primary():
    rows = _history()
    adapter = CompositeAdapter(_qc_adapter(rows), StubEdgar(FilingFlags()))
    assert adapter.get_profile("TEST").cik == "0000018230"
    assert adapter.get_annual_financials("TEST")
    assert "edgar" in adapter.name
