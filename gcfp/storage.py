"""SQLite state.

§16 names what must be stored: holdings, alert history, conviction score
history with component breakdown, the intended-vs-actual size log, thesis
statements, the classification transition log, peer exclusion logs, the TAM
source log, FX rate history, the cooling-off log, and bucket utilisation
snapshots.

Every one of those is an audit trail rather than a cache.  Rows are inserted
and never updated: a conviction score that changed is two rows, not one row
edited, because Module H's whole job is comparing a score to what it was at
purchase.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    classification TEXT NOT NULL,
    opened_on TEXT NOT NULL,
    closed_on TEXT,
    shares REAL,
    cost_base_currency REAL,
    base_currency TEXT NOT NULL,
    thesis_invalidation TEXT NOT NULL,
    anchor_mode TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    raised_on TEXT NOT NULL,
    signal TEXT NOT NULL,
    classification TEXT NOT NULL,
    reasons TEXT NOT NULL,
    conditions TEXT NOT NULL,
    values_json TEXT NOT NULL,
    suppressed_until TEXT,
    config_fingerprint TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conviction_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    scored_on TEXT NOT NULL,
    total REAL NOT NULL,
    band TEXT NOT NULL,
    components TEXT NOT NULL,
    anchor_mode TEXT NOT NULL,
    capped_at REAL,
    config_fingerprint TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS size_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    logged_on TEXT NOT NULL,
    intended_amount_base REAL NOT NULL,
    actual_amount_base REAL NOT NULL,
    deviation REAL NOT NULL,
    size_deviation INTEGER NOT NULL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS classification_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    transitioned_on TEXT NOT NULL,
    old_tag TEXT NOT NULL,
    new_tag TEXT NOT NULL,
    trigger TEXT NOT NULL,
    retest_outcome TEXT
);

CREATE TABLE IF NOT EXISTS peer_exclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    evaluated_on TEXT NOT NULL,
    candidate TEXT NOT NULL,
    included INTEGER NOT NULL,
    reason TEXT NOT NULL,
    candidate_multiple REAL
);

CREATE TABLE IF NOT EXISTS tam_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    logged_on TEXT NOT NULL,
    source_name TEXT NOT NULL,
    published TEXT NOT NULL,
    figure REAL NOT NULL,
    currency TEXT NOT NULL,
    method TEXT NOT NULL,
    url TEXT
);

CREATE TABLE IF NOT EXISTS fx_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT NOT NULL,
    rate REAL NOT NULL,
    as_of TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cooling_off (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_on TEXT NOT NULL,
    change TEXT NOT NULL,
    is_sleeve_cap_increase INTEGER NOT NULL,
    eligible_on TEXT NOT NULL,
    rationale TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bucket_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_on TEXT NOT NULL,
    bucket TEXT NOT NULL,
    current_share REAL NOT NULL,
    target_low REAL NOT NULL,
    target_high REAL NOT NULL,
    total_value REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    as_of TEXT NOT NULL,
    gate TEXT NOT NULL,
    outcome TEXT NOT NULL,
    value REAL,
    threshold REAL,
    branch TEXT,
    reason TEXT,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_conviction_symbol ON conviction_history(symbol);
CREATE INDEX IF NOT EXISTS idx_alerts_symbol ON alerts(symbol);
CREATE INDEX IF NOT EXISTS idx_audit_symbol ON audit_entries(symbol, as_of);
CREATE INDEX IF NOT EXISTS idx_peer_symbol ON peer_exclusions(symbol, evaluated_on);
"""


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


@dataclass
class Store:
    """A thin, explicit wrapper over SQLite.

    No ORM: the tables are an audit record whose shape is fixed by the spec,
    and hand-written SQL keeps that shape visible.
    """

    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- writes -----------------------------------------------------------
    def record_alert(self, signal, config_fingerprint: str, raised_on: date | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO alerts (symbol, raised_on, signal, classification, "
                "reasons, conditions, values_json, suppressed_until, config_fingerprint) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    signal.symbol,
                    (raised_on or date.today()).isoformat(),
                    signal.signal.value,
                    signal.classification.value,
                    _json(signal.reasons),
                    _json(signal.conditions),
                    _json(signal.values),
                    signal.suppressed_until.isoformat() if signal.suppressed_until else None,
                    config_fingerprint,
                ),
            )

    def record_conviction(
        self, score, anchor_mode: str, config_fingerprint: str, scored_on: date | None = None
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO conviction_history (symbol, scored_on, total, band, "
                "components, anchor_mode, capped_at, config_fingerprint) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    score.symbol,
                    (scored_on or date.today()).isoformat(),
                    score.total,
                    score.band,
                    _json({c.name: c.points for c in score.components}),
                    anchor_mode,
                    score.capped_at,
                    config_fingerprint,
                ),
            )

    def record_peer_decisions(
        self, symbol: str, decisions: Sequence, evaluated_on: date | None = None
    ) -> None:
        """C2's exclusion log — the thing that makes peer selection auditable."""
        stamp = (evaluated_on or date.today()).isoformat()
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO peer_exclusions (symbol, evaluated_on, candidate, "
                "included, reason, candidate_multiple) VALUES (?,?,?,?,?,?)",
                [
                    (
                        symbol,
                        stamp,
                        d.candidate.symbol,
                        int(d.included),
                        d.reason,
                        d.candidate.multiple,
                    )
                    for d in decisions
                ],
            )

    def record_tam_sources(
        self, symbol: str, sources: Sequence, logged_on: date | None = None
    ) -> None:
        stamp = (logged_on or date.today()).isoformat()
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO tam_sources (symbol, logged_on, source_name, published, "
                "figure, currency, method, url) VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        symbol,
                        stamp,
                        s.name,
                        s.published.isoformat(),
                        s.figure,
                        s.currency,
                        s.method,
                        s.url,
                    )
                    for s in sources
                ],
            )

    def record_execution(self, record) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO size_log (symbol, logged_on, intended_amount_base, "
                "actual_amount_base, deviation, size_deviation, reason) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    record.symbol,
                    record.executed_on.isoformat(),
                    record.intended_amount_base,
                    record.actual_amount_base,
                    record.deviation,
                    int(record.size_deviation),
                    record.reason,
                ),
            )

    def record_transition(
        self, protocol, trigger: str = "A6 reassigned the tag"
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO classification_transitions (symbol, transitioned_on, "
                "old_tag, new_tag, trigger, retest_outcome) VALUES (?,?,?,?,?,?)",
                (
                    protocol.symbol,
                    protocol.triggered_on.isoformat(),
                    protocol.old_tag.value,
                    protocol.new_tag.value,
                    trigger,
                    protocol.outcome,
                ),
            )

    def record_fx(self, table) -> None:
        stamp = (table.as_of or date.today()).isoformat()
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO fx_rates (pair, rate, as_of) VALUES (?,?,?)",
                [(pair, rate, stamp) for pair, rate in table.rates.items()],
            )

    def record_cooling_off(self, record) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO cooling_off (requested_on, change, "
                "is_sleeve_cap_increase, eligible_on, rationale) VALUES (?,?,?,?,?)",
                (
                    record.requested_on.isoformat(),
                    record.change,
                    int(record.is_sleeve_cap_increase),
                    record.eligible_on.isoformat(),
                    record.rationale,
                ),
            )

    def record_bucket_snapshot(
        self, states: Sequence, total_value: float, snapshot_on: date | None = None
    ) -> None:
        stamp = (snapshot_on or date.today()).isoformat()
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO bucket_snapshots (snapshot_on, bucket, current_share, "
                "target_low, target_high, total_value) VALUES (?,?,?,?,?,?)",
                [
                    (stamp, s.bucket.value, s.current_share, s.target_low, s.target_high, total_value)
                    for s in states
                ],
            )

    def record_audit(self, ledger) -> None:
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO audit_entries (symbol, as_of, gate, outcome, value, "
                "threshold, branch, reason, detail) VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        ledger.symbol,
                        ledger.as_of.isoformat(),
                        e.gate,
                        e.outcome.value,
                        e.value,
                        e.threshold,
                        e.branch,
                        e.reason,
                        _json(e.detail),
                    )
                    for e in ledger.entries
                ],
            )

    # -- reads ------------------------------------------------------------
    def conviction_at_purchase(self, symbol: str) -> float | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT total FROM conviction_history WHERE symbol = ? "
                "ORDER BY scored_on ASC, id ASC LIMIT 1",
                (symbol,),
            ).fetchone()
        return row["total"] if row else None

    def latest_conviction(self, symbol: str) -> float | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT total FROM conviction_history WHERE symbol = ? "
                "ORDER BY scored_on DESC, id DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        return row["total"] if row else None

    def single_anchor_rate(self) -> float | None:
        """Module I's break criterion: SINGLE-ANCHOR MODE above 40% of
        evaluated candidates means the anchors are not available at the
        assumed rate."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, "
                "SUM(CASE WHEN anchor_mode LIKE 'SINGLE%' THEN 1 ELSE 0 END) AS single "
                "FROM conviction_history"
            ).fetchone()
        if not row or not row["n"]:
            return None
        return (row["single"] or 0) / row["n"]

    def count(self, table: str) -> int:
        with self.connect() as conn:
            return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


__all__ = ["Store", "SCHEMA"]
