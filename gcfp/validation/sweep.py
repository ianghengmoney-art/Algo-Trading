"""Parameter sweep.

Trust plateaus, distrust spikes, pick mid-plateau. The sweep exists to find the
plateau, and the plateau detector exists so that "pick mid-plateau" is a
procedure rather than an intention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence


@dataclass
class SweepPoint:
    value: float
    score: float


@dataclass
class SweepResult:
    parameter: str
    classification: Optional[str]
    points: list[SweepPoint] = field(default_factory=list)
    plateau: tuple[float, float] | None = None
    recommended: Optional[float] = None
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        scope = f" [{self.classification}]" if self.classification else ""
        head = f"{self.parameter}{scope}: "
        if self.recommended is None:
            return head + "no plateau found -- " + "; ".join(self.notes)
        return (
            head
            + f"plateau {self.plateau[0]:g} to {self.plateau[1]:g}, recommend {self.recommended:g}"
        )


def find_plateau(points: Sequence[SweepPoint], tolerance_pct: float = 10.0) -> tuple[Optional[tuple[float, float]], list[str]]:
    """Longest run of adjacent values scoring within tolerance of the best.

    A single spiking value is exactly what overfitting looks like, so the
    recommendation comes from the middle of the widest stable region rather
    than from the argmax.
    """
    notes: list[str] = []
    if len(points) < 3:
        return None, ["fewer than three sweep points -- no plateau can be identified"]

    best = max(p.score for p in points)
    if best <= 0:
        return None, ["best score is not positive"]
    floor = best * (1.0 - tolerance_pct / 100.0)

    longest: tuple[int, int] | None = None
    start: Optional[int] = None
    for i, point in enumerate(points):
        if point.score >= floor:
            if start is None:
                start = i
            if longest is None or (i - start) > (longest[1] - longest[0]):
                longest = (start, i)
        else:
            start = None

    if longest is None or longest[1] == longest[0]:
        notes.append(
            "best score is an isolated spike, not a plateau -- distrust it and do not adopt "
            "the argmax"
        )
        return None, notes
    return (points[longest[0]].value, points[longest[1]].value), notes


def sweep(
    parameter: str,
    values: Sequence[float],
    evaluate: Callable[[float], float],
    classification: Optional[str] = None,
    tolerance_pct: float = 10.0,
) -> SweepResult:
    points = [SweepPoint(value, evaluate(value)) for value in values]
    points.sort(key=lambda p: p.value)
    plateau, notes = find_plateau(points, tolerance_pct)
    result = SweepResult(parameter, classification, points, plateau, None, notes)
    if plateau:
        inside = [p.value for p in points if plateau[0] <= p.value <= plateau[1]]
        result.recommended = inside[len(inside) // 2]
    return result
