"""The audit ledger.

Prime Directive 9: every gate logs its computed value, not just PASS/FAIL.
Auditability is a hard requirement, not a nicety.

Every gate in this system returns a :class:`GateResult` carrying the number it
computed, the threshold it was compared against, and which branch of the rule
applied.  A ``PASS`` with no number attached is not an acceptable output — the
whole point is that a reader can later ask "how close was that?" and get an
answer without re-running anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Iterable, Iterator


class Outcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    #: Computed, informative, but never on its own a reason to reject.
    FLAG = "FLAG"
    #: The input needed to decide was absent.  Never imputed (A5).
    NOT_COMPUTABLE = "NOT_COMPUTABLE"

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.value


@dataclass(frozen=True)
class GateResult:
    """One gate's verdict, with the arithmetic that produced it."""

    gate: str
    outcome: Outcome
    #: The number the gate actually computed.  ``None`` only when the gate
    #: was NOT_COMPUTABLE, in which case ``reason`` says what was missing.
    value: float | None = None
    threshold: float | None = None
    #: Which arm of a multi-branch rule applied, e.g. A1's "current_ratio" vs
    #: "operating_cash_flow".  A1 explicitly requires logging this.
    branch: str | None = None
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASS

    @property
    def failed(self) -> bool:
        return self.outcome is Outcome.FAIL

    @property
    def computable(self) -> bool:
        return self.outcome is not Outcome.NOT_COMPUTABLE

    def describe(self) -> str:
        """One audit line: the value, the threshold, and how it was reached."""
        bits = [f"{self.gate}: {self.outcome.value}"]
        if self.value is not None:
            bits.append(f"value={_fmt(self.value)}")
        if self.threshold is not None:
            bits.append(f"threshold={_fmt(self.threshold)}")
        if self.branch:
            bits.append(f"branch={self.branch}")
        if self.reason:
            bits.append(f"reason={self.reason}")
        for key, val in self.detail.items():
            bits.append(f"{key}={val}")
        return " · ".join(bits)


def _fmt(value: float) -> str:
    if isinstance(value, float):
        if value != value:  # NaN
            return "nan"
        if abs(value) >= 1_000_000:
            return f"{value:,.0f}"
        return f"{value:,.4g}"
    return str(value)


@dataclass
class AuditLedger:
    """An append-only record for one evaluation of one company."""

    symbol: str
    as_of: date
    entries: list[GateResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def record(self, result: GateResult) -> GateResult:
        self.entries.append(result)
        return result

    def record_all(self, results: Iterable[GateResult]) -> list[GateResult]:
        collected = list(results)
        self.entries.extend(collected)
        return collected

    def note(self, message: str) -> None:
        """Free-text audit line for things that are not gate verdicts —
        peer exclusions, TAM sources, substitutions, fallback levels."""
        self.notes.append(message)

    def get(self, gate: str) -> GateResult | None:
        for entry in self.entries:
            if entry.gate == gate:
                return entry
        return None

    def failures(self) -> list[GateResult]:
        return [e for e in self.entries if e.failed]

    def flags(self) -> list[GateResult]:
        return [e for e in self.entries if e.outcome is Outcome.FLAG]

    def not_computable(self) -> list[GateResult]:
        return [e for e in self.entries if not e.computable]

    def __iter__(self) -> Iterator[GateResult]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def render(self) -> str:
        lines = [f"AUDIT · {self.symbol} · as of {self.as_of.isoformat()}"]
        lines.extend(f"  {e.describe()}" for e in self.entries)
        lines.extend(f"  note: {n}" for n in self.notes)
        return "\n".join(lines)


def gate_pass(gate: str, value: float | None = None, **kw: Any) -> GateResult:
    return GateResult(gate=gate, outcome=Outcome.PASS, value=value, **kw)


def gate_fail(gate: str, value: float | None = None, **kw: Any) -> GateResult:
    return GateResult(gate=gate, outcome=Outcome.FAIL, value=value, **kw)


def gate_flag(gate: str, value: float | None = None, **kw: Any) -> GateResult:
    return GateResult(gate=gate, outcome=Outcome.FLAG, value=value, **kw)


def gate_uncomputable(gate: str, reason: str, **kw: Any) -> GateResult:
    return GateResult(
        gate=gate, outcome=Outcome.NOT_COMPUTABLE, reason=reason, **kw
    )


__all__ = [
    "Outcome",
    "GateResult",
    "AuditLedger",
    "gate_pass",
    "gate_fail",
    "gate_flag",
    "gate_uncomputable",
]
