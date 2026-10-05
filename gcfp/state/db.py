"""Local SQLite state.

Holdings, alert history, conviction score history, the intended-versus-actual
size log, thesis statements, the cooling-off log and sleeve utilisation.

Everything here is a record of what the system said and what the operator did.
Nothing here can act.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS holdings (
    symbol TEXT PRIMARY KEY,
    classification TEXT NOT NULL,
    sector TEXT,
    opened_on TEXT NOT NULL,
    cost_value REAL NOT NULL,
    current_value REAL,
    conviction_at_purchase REAL,
    classification_at_purchase TEXT,
    anchors_agreed_at_purchase INTEGER,
    closed_on TEXT,
    close_reason TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raised_on TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    classification TEXT,
    conviction REAL,
    fair_value REAL,
    price REAL,
    deferred INTEGER DEFAULT 0,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS conviction_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    observed_on TEXT NOT NULL,
    total REAL NOT NULL,
    breakdown TEXT,
    UNIQUE(symbol, observed_on)
);

CREATE TABLE IF NOT EXISTS recommendations (
    symbol TEXT NOT NULL,
    recommended_on TEXT NOT NULL,
    classification TEXT NOT NULL,
    conviction_score REAL NOT NULL,
    fair_value_per_share REAL NOT NULL,
    valuation_method TEXT NOT NULL,
    own_history_reading_pct REAL,
    peer_reading_pct REAL,
    intended_sleeve_pct REAL NOT NULL,
    intended_value REAL NOT NULL,
    currency TEXT NOT NULL,
    thesis_invalidation TEXT NOT NULL,
    PRIMARY KEY (symbol, recommended_on)
);

CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    recommended_on TEXT NOT NULL,
    executed_on TEXT NOT NULL,
    actual_value REAL NOT NULL,
    deviation_pct REAL,
    size_deviation INTEGER NOT NULL DEFAULT 0,
    reason_for_deviation TEXT
);

CREATE TABLE IF NOT EXISTS cooling_off (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_on TEXT NOT NULL,
    change_description TEXT NOT NULL,
    is_sleeve_cap_increase INTEGER NOT NULL,
    required_days INTEGER NOT NULL,
    eligible_on TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sleeve_utilisation (
    observed_on TEXT NOT NULL,
    sleeve TEXT NOT NULL,
    value REAL,
    capacity REAL,
    names INTEGER,
    pct_of_portfolio REAL,
    PRIMARY KEY (observed_on, sleeve)
);

CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_on TEXT NOT NULL,
    adapter TEXT NOT NULL,
    params_revision TEXT NOT NULL,
    universe_size INTEGER,
    passers INTEGER,
    notices TEXT
);
"""


def _encode(value: Any) -> str:
    if is_dataclass(value):
        value = asdict(value)
    return json.dumps(value, default=str)


class Store:
    """Thin, explicit persistence layer.

    Deliberately not an ORM: the tables here are an audit trail, and a literal
    schema in one place is easier to check against the spec than a model
    hierarchy.
    """

    def __init__(self, path: str | Path = "gcfp.sqlite3") -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # -- holdings ---------------------------------------------------------
    def upsert_holding(
        self,
        symbol: str,
        classification: str,
        sector: Optional[str],
        opened_on: date,
        cost_value: float,
        current_value: Optional[float] = None,
        conviction_at_purchase: Optional[float] = None,
        anchors_agreed_at_purchase: Optional[bool] = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO holdings
                   (symbol, classification, sector, opened_on, cost_value, current_value,
                    conviction_at_purchase, classification_at_purchase, anchors_agreed_at_purchase)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol) DO UPDATE SET
                     classification=excluded.classification,
                     sector=excluded.sector,
                     current_value=excluded.current_value""",
                (
                    symbol, classification, sector, opened_on.isoformat(), cost_value,
                    current_value, conviction_at_purchase, classification,
                    None if anchors_agreed_at_purchase is None else int(anchors_agreed_at_purchase),
                ),
            )

    def open_holdings(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM holdings WHERE closed_on IS NULL"))

    def close_holding(self, symbol: str, closed_on: date, reason: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE holdings SET closed_on=?, close_reason=? WHERE symbol=?",
                (closed_on.isoformat(), reason, symbol),
            )

    # -- alerts -----------------------------------------------------------
    def record_alert(
        self,
        raised_on: date,
        symbol: str,
        action: str,
        classification: Optional[str] = None,
        conviction: Optional[float] = None,
        fair_value: Optional[float] = None,
        price: Optional[float] = None,
        deferred: bool = False,
        detail: Any = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO alerts
                   (raised_on, symbol, action, classification, conviction, fair_value, price,
                    deferred, detail)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    raised_on.isoformat(), symbol, action, classification, conviction,
                    fair_value, price, int(deferred), _encode(detail) if detail is not None else None,
                ),
            )

    def alerts_for(self, symbol: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM alerts WHERE symbol=? ORDER BY raised_on DESC", (symbol,)
            )
        )

    # -- conviction history ----------------------------------------------
    def record_conviction(self, symbol: str, observed_on: date, total: float, breakdown: Any) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO conviction_history (symbol, observed_on, total, breakdown)
                   VALUES (?,?,?,?)
                   ON CONFLICT(symbol, observed_on) DO UPDATE SET
                     total=excluded.total, breakdown=excluded.breakdown""",
                (symbol, observed_on.isoformat(), total, _encode(breakdown)),
            )

    def conviction_at_purchase(self, symbol: str) -> Optional[float]:
        row = self._conn.execute(
            "SELECT conviction_at_purchase FROM holdings WHERE symbol=?", (symbol,)
        ).fetchone()
        return row["conviction_at_purchase"] if row else None

    def conviction_history(self, symbol: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM conviction_history WHERE symbol=? ORDER BY observed_on", (symbol,)
            )
        )

    # -- recommendations and executions -----------------------------------
    def record_recommendation(self, record: Any) -> None:
        """Immutable by construction: re-recording the same (symbol, date) is refused."""
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO recommendations
                   (symbol, recommended_on, classification, conviction_score,
                    fair_value_per_share, valuation_method, own_history_reading_pct,
                    peer_reading_pct, intended_sleeve_pct, intended_value, currency,
                    thesis_invalidation)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.symbol, record.recommended_on.isoformat(), record.classification,
                    record.conviction_score, record.fair_value_per_share, record.valuation_method,
                    record.own_history_reading_pct, record.peer_reading_pct,
                    record.intended_sleeve_pct, record.intended_value, record.currency,
                    record.thesis_invalidation,
                ),
            )

    def recommendations(self, symbol: Optional[str] = None) -> list[sqlite3.Row]:
        if symbol:
            return list(
                self._conn.execute(
                    "SELECT * FROM recommendations WHERE symbol=? ORDER BY recommended_on DESC",
                    (symbol,),
                )
            )
        return list(self._conn.execute("SELECT * FROM recommendations ORDER BY recommended_on DESC"))

    def record_execution(self, recommended_on: date, execution: Any) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO executions
                   (symbol, recommended_on, executed_on, actual_value, deviation_pct,
                    size_deviation, reason_for_deviation)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    execution.symbol, recommended_on.isoformat(), execution.executed_on.isoformat(),
                    execution.actual_value, execution.deviation_pct, int(execution.size_deviation),
                    execution.reason_for_deviation,
                ),
            )

    def size_deviation_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM executions WHERE size_deviation=1"
        ).fetchone()
        return int(row["n"])

    def executions(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM executions ORDER BY executed_on"))

    # -- discipline -------------------------------------------------------
    def record_cooling_off(self, entry: Any) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO cooling_off
                   (requested_on, change_description, is_sleeve_cap_increase, required_days,
                    eligible_on, resolved)
                   VALUES (?,?,?,?,?,?)""",
                (
                    entry.requested_on.isoformat(), entry.change_description,
                    int(entry.is_sleeve_cap_increase), entry.required_days,
                    entry.eligible_on.isoformat(), int(entry.resolved),
                ),
            )

    def pending_cooling_off(self, as_of: date) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM cooling_off WHERE resolved=0 AND eligible_on > ? ORDER BY eligible_on",
                (as_of.isoformat(),),
            )
        )

    # -- sleeve utilisation and runs --------------------------------------
    def record_sleeve_utilisation(self, observed_on: date, sleeve: str, stats: dict) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO sleeve_utilisation
                   (observed_on, sleeve, value, capacity, names, pct_of_portfolio)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(observed_on, sleeve) DO UPDATE SET
                     value=excluded.value, capacity=excluded.capacity,
                     names=excluded.names, pct_of_portfolio=excluded.pct_of_portfolio""",
                (
                    observed_on.isoformat(), sleeve, stats.get("value"), stats.get("capacity"),
                    stats.get("names"), stats.get("pct_of_portfolio"),
                ),
            )

    def record_run(
        self,
        ran_on: date,
        adapter: str,
        params_revision: str,
        universe_size: int,
        passers: int,
        notices: Sequence[str],
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO run_log (ran_on, adapter, params_revision, universe_size, passers, notices)
                   VALUES (?,?,?,?,?,?)""",
                (ran_on.isoformat(), adapter, params_revision, universe_size, passers, _encode(list(notices))),
            )
