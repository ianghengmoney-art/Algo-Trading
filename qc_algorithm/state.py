"""ObjectStore-backed state.

`gcfp/state/db.py` writes SQLite to local disk, which does not survive a QC
redeploy -- the container is replaced, and with it every recommendation record,
conviction history and cooling-off entry. ObjectStore is QC's durable store, so
the audit trail moves there.

The schema is deliberately the same shape as the SQLite one. Module G's whole
argument is that the intended-versus-actual record has to outlive the decision
that produced it; a store that quietly resets on redeploy would make the
discipline metric read zero forever.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date
from typing import Any, Optional

PREFIX = "gcfp"

HOLDINGS = f"{PREFIX}/holdings.json"
RECOMMENDATIONS = f"{PREFIX}/recommendations.json"
EXECUTIONS = f"{PREFIX}/executions.json"
CONVICTION = f"{PREFIX}/conviction_history.json"
RUNS = f"{PREFIX}/run_log.json"
COOLING_OFF = f"{PREFIX}/cooling_off.json"


def _encode(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value


class ObjectStoreState:
    """Durable append-only records for a running algorithm.

    ``object_store`` is QC's ``algorithm.object_store``. It is injected rather
    than imported so this class is testable against a dict-backed fake.
    """

    def __init__(self, object_store: Any, prefix: str = PREFIX) -> None:
        self._store = object_store
        self._prefix = prefix

    def _key(self, key: str) -> str:
        return key.replace(PREFIX, self._prefix, 1)

    def _read(self, key: str, default: Any) -> Any:
        key = self._key(key)
        try:
            if not self._store.contains_key(key):
                return default
            raw = self._store.read(key)
        except Exception:
            return default
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default

    def _write(self, key: str, value: Any) -> None:
        self._store.save(self._key(key), json.dumps(_encode(value), default=str))

    def _append(self, key: str, row: Any) -> None:
        rows = self._read(key, [])
        rows.append(_encode(row))
        self._write(key, rows)

    # -- holdings ---------------------------------------------------------
    def holdings(self) -> dict:
        return self._read(HOLDINGS, {})

    def record_holding(
        self,
        symbol: str,
        classification: str,
        sector: Optional[str],
        opened_on: date,
        conviction_at_purchase: float,
        anchors_agreed_at_purchase: bool,
    ) -> None:
        book = self.holdings()
        book[symbol] = {
            "classification": classification,
            "sector": sector,
            "opened_on": opened_on.isoformat(),
            "conviction_at_purchase": conviction_at_purchase,
            "classification_at_purchase": classification,
            "anchors_agreed_at_purchase": anchors_agreed_at_purchase,
        }
        self._write(HOLDINGS, book)

    def close_holding(self, symbol: str, closed_on: date, reason: str) -> None:
        book = self.holdings()
        entry = book.pop(symbol, None)
        if entry is not None:
            entry.update({"closed_on": closed_on.isoformat(), "close_reason": reason})
            self._append(f"{PREFIX}/closed_holdings.json", entry)
            self._write(HOLDINGS, book)

    def conviction_at_purchase(self, symbol: str) -> Optional[float]:
        entry = self.holdings().get(symbol)
        return entry.get("conviction_at_purchase") if entry else None

    # -- audit trail ------------------------------------------------------
    def record_recommendation(self, record: Any) -> None:
        """Immutable by convention: recommendations are only ever appended."""
        self._append(RECOMMENDATIONS, record)

    def recommendations(self) -> list:
        return self._read(RECOMMENDATIONS, [])

    def record_execution(self, row: Any) -> None:
        self._append(EXECUTIONS, row)

    def executions(self) -> list:
        return self._read(EXECUTIONS, [])

    def size_deviation_count(self) -> int:
        return sum(1 for row in self.executions() if row.get("size_deviation"))

    def record_conviction(self, symbol: str, observed_on: date, total: float, breakdown: Any) -> None:
        self._append(
            CONVICTION,
            {
                "symbol": symbol,
                "observed_on": observed_on.isoformat(),
                "total": total,
                "breakdown": _encode(breakdown),
            },
        )

    def record_run(self, ran_on: date, universe_size: int, passers: int, notices: list[str]) -> None:
        self._append(
            RUNS,
            {
                "ran_on": ran_on.isoformat(),
                "universe_size": universe_size,
                "passers": passers,
                "notices": list(notices),
            },
        )

    def record_cooling_off(self, entry: Any) -> None:
        self._append(COOLING_OFF, entry)
