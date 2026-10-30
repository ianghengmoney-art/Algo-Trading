"""§13.7 — the parameter sweep.

    "Parameter sweep across X, Y, conviction floor, the 30% divergence
    threshold, and the D2 excess-scaling denominator, separately per
    classification. **Trust plateaus, distrust spikes.**"

That last instruction is the whole point, and it is the part a sweep usually
gets wrong.  A parameter value that is best by a wide margin over its immediate
neighbours has almost certainly fitted the noise in this particular history: it
will not survive contact with a different decade.  A value sitting in the
middle of a broad region where results barely change is a real setting.

So this module does not report "the best value".  It reports whether the best
value sits on a **plateau** or on a **spike**, and says which — because a
sweep that only names a winner invites exactly the overfitting the instruction
warns against.

§13.3's "no tuning on the holdout" is enforced here rather than trusted:
:func:`run_sweep` refuses a period that overlaps the holdout.
"""

from __future__ import annotations

import dataclasses
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Sequence

from ..classification import Classification
from ..config import Config
from .engine import BacktestResult, WalkForwardSplit
from .metrics import annualised_return, distribution


class HoldoutLeak(Exception):
    """Raised when a sweep would touch the holdout period.

    §13.3 says no tuning on the holdout.  A leak here does not fail loudly on
    its own — it produces a better-looking result — so it is made an error.
    """


@dataclass(frozen=True)
class SweepPoint:
    """One parameter value and what it produced."""

    value: float
    annualised: float | None
    max_drawdown: float | None
    loss_rate: float | None
    closed_positions: int

    @property
    def score(self) -> float:
        """The single number the plateau analysis ranks on.

        Annualised return, penalised by drawdown depth: a parameter that buys
        half a point of return with twenty points of drawdown has not improved
        anything, and ranking on return alone would say it had.
        """
        if self.annualised is None:
            return float("-inf")
        penalty = abs(self.max_drawdown or 0.0) * 0.5
        return self.annualised - penalty


@dataclass
class ParameterSweep:
    """One parameter swept across its range."""

    parameter: str
    classification: Classification | None
    points: list[SweepPoint] = field(default_factory=list)
    #: Results within this fraction of the sweep's own score *range* count as
    #: "the same".  Scaling to the range rather than to the best score matters:
    #: the score is a return net of a drawdown penalty and sits near zero, so a
    #: fraction-of-best tolerance collapses to nothing and every sweep reads as
    #: a spike.
    plateau_tolerance: float = 0.10
    #: Floor for the tolerance, so a sweep whose values barely differ does not
    #: report a one-point plateau on floating-point noise.
    min_tolerance: float = 0.005

    @property
    def usable(self) -> list[SweepPoint]:
        return [p for p in self.points if p.annualised is not None]

    @property
    def best(self) -> SweepPoint | None:
        rows = self.usable
        return max(rows, key=lambda p: p.score) if rows else None

    @property
    def plateau(self) -> list[SweepPoint]:
        """The contiguous run around the best value whose results barely differ."""
        best = self.best
        if best is None:
            return []
        ordered = sorted(self.usable, key=lambda p: p.value)
        index = next(i for i, p in enumerate(ordered) if p.value == best.value)
        worst = min(p.score for p in ordered)
        span = max(
            (best.score - worst) * self.plateau_tolerance, self.min_tolerance
        )

        run = [ordered[index]]
        for point in reversed(ordered[:index]):
            if abs(point.score - best.score) <= span:
                run.insert(0, point)
            else:
                break
        for point in ordered[index + 1 :]:
            if abs(point.score - best.score) <= span:
                run.append(point)
            else:
                break
        return run

    @property
    def is_spike(self) -> bool:
        """True when the best value stands alone.

        A single value beating both neighbours by more than the tolerance is
        the signature of a fit to this history's noise.
        """
        return len(self.plateau) <= 1 and len(self.usable) > 2

    @property
    def recommended(self) -> float | None:
        """The middle of the plateau, not the peak.

        Choosing the peak of a plateau is choosing noise within a region where
        the result does not really vary; the middle is the value most likely to
        survive a different period.
        """
        plateau = self.plateau
        if not plateau:
            return None
        if self.is_spike:
            return None
        return plateau[len(plateau) // 2].value

    def as_report_lines(self) -> list[str]:
        scope = f" [{self.classification.value}]" if self.classification else ""
        lines = [f"  {self.parameter}{scope}"]
        if not self.usable:
            lines.append("    no usable results")
            return lines

        plateau = self.plateau
        is_spike = self.is_spike
        best = self.best
        for point in sorted(self.usable, key=lambda p: p.value):
            marker = ""
            if best and point.value == best.value:
                marker = "  <- best"
            if point in plateau and not is_spike:
                marker += " (plateau)"
            lines.append(
                f"    {point.value:>8.3f}: {point.annualised:+.2%}/yr "
                f"dd {point.max_drawdown:+.1%} "
                f"loss-rate {point.loss_rate:.0%} "
                f"n={point.closed_positions}{marker}"
                if point.max_drawdown is not None and point.loss_rate is not None
                else f"    {point.value:>8.3f}: {point.annualised:+.2%}/yr{marker}"
            )

        if is_spike:
            lines.append(
                "    SPIKE — the best value stands alone. Distrust it: this is "
                "the signature of a fit to this period's noise. Keep the "
                "existing setting."
            )
        elif self.recommended is not None:
            lines.append(
                f"    PLATEAU of {len(plateau)} values — recommend "
                f"{self.recommended:.3f} (the middle, not the peak)"
            )
        return lines


#: The five parameters §13.7 names, and how to set each one on a Config.
def _with_buy_threshold(config: Config, classification: Classification, value: float) -> Config:
    discounts = dict(config.triggers.buy_discount)
    discounts[classification] = value
    return dataclasses.replace(
        config, triggers=dataclasses.replace(config.triggers, buy_discount=discounts)
    )


def _with_sell_threshold(config: Config, classification: Classification, value: float) -> Config:
    premiums = dict(config.triggers.sell_premium)
    premiums[classification] = value
    return dataclasses.replace(
        config, triggers=dataclasses.replace(config.triggers, sell_premium=premiums)
    )


def _with_conviction_floor(config: Config, _c: Classification | None, value: float) -> Config:
    return dataclasses.replace(
        config,
        triggers=dataclasses.replace(config.triggers, min_conviction_to_buy=value),
    )


def _with_divergence(config: Config, _c: Classification | None, value: float) -> Config:
    return dataclasses.replace(
        config, anchors=dataclasses.replace(config.anchors, divergence_flag=value)
    )


def _with_d2_denominator(config: Config, _c: Classification | None, value: float) -> Config:
    return dataclasses.replace(
        config,
        conviction=dataclasses.replace(
            config.conviction, valuation_excess_denominator=value
        ),
    )


@dataclass(frozen=True)
class SweptParameter:
    name: str
    values: tuple[float, ...]
    apply: Callable[[Config, Classification | None, float], Config]
    #: Whether the spec asks for this one to be swept per classification.
    per_classification: bool


SWEPT_PARAMETERS: tuple[SweptParameter, ...] = (
    SweptParameter(
        "X (buy discount)",
        (0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45),
        _with_buy_threshold,
        True,
    ),
    SweptParameter(
        "Y (sell premium)",
        (0.10, 0.15, 0.20, 0.25, 0.30, 0.35),
        _with_sell_threshold,
        True,
    ),
    SweptParameter(
        "conviction floor",
        (50.0, 55.0, 60.0, 65.0, 70.0, 75.0),
        _with_conviction_floor,
        False,
    ),
    SweptParameter(
        "C4 divergence threshold",
        (0.20, 0.25, 0.30, 0.35, 0.40, 0.50),
        _with_divergence,
        False,
    ),
    SweptParameter(
        "D2 excess-scaling denominator",
        (0.15, 0.20, 0.25, 0.30, 0.35),
        _with_d2_denominator,
        False,
    ),
)


@dataclass
class SweepReport:
    sweeps: list[ParameterSweep] = field(default_factory=list)
    period: str = ""

    @property
    def spikes(self) -> list[ParameterSweep]:
        return [s for s in self.sweeps if s.is_spike]

    def as_report_lines(self) -> list[str]:
        lines = ["PARAMETER SWEEP (§13.7)", f"  period: {self.period}"]
        lines.append(
            "  Trust plateaus, distrust spikes. A value that only wins by a "
            "wide margin over its neighbours has fitted this period's noise."
        )
        for sweep in self.sweeps:
            lines.extend(sweep.as_report_lines())
        if self.spikes:
            lines.append("")
            lines.append(
                f"  {len(self.spikes)} parameter(s) produced a spike rather than "
                "a plateau. No change is recommended for those; the existing "
                "settings stand."
            )
        return lines


def run_sweep(
    runner: Callable[[Config], BacktestResult],
    base: Config,
    *,
    split: WalkForwardSplit | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
    parameters: Sequence[SweptParameter] = SWEPT_PARAMETERS,
    classifications: Sequence[Classification] = tuple(Classification),
    progress: bool = False,
) -> SweepReport:
    """Sweep each parameter, on the training period only.

    ``runner`` takes a Config and returns a completed :class:`BacktestResult`,
    so the caller decides how the backtest is wired and this module only varies
    the parameters.
    """
    if split is not None and period_end is not None:
        if period_end >= split.test_start:
            raise HoldoutLeak(
                f"sweep period ends {period_end.isoformat()}, which reaches into "
                f"the holdout starting {split.test_start.isoformat()}. §13.3: no "
                "tuning on the holdout."
            )

    report = SweepReport(
        period=(
            f"{period_start.isoformat()}..{period_end.isoformat()}"
            if period_start and period_end
            else "unspecified"
        )
    )

    for parameter in parameters:
        scopes: Sequence[Classification | None] = (
            classifications if parameter.per_classification else (None,)
        )
        for scope in scopes:
            sweep = ParameterSweep(parameter=parameter.name, classification=scope)
            for value in parameter.values:
                if progress:
                    label = f"{parameter.name}{f' [{scope.value}]' if scope else ''}"
                    print(f"  sweeping {label} = {value}", flush=True)
                config = parameter.apply(base, scope, value)
                result = runner(config)
                curve = result.equity_curve
                stats = distribution(result.book.closed)
                from .metrics import max_drawdown as _mdd

                episode = _mdd(curve)
                sweep.points.append(
                    SweepPoint(
                        value=value,
                        annualised=annualised_return(curve),
                        max_drawdown=episode.depth if episode else None,
                        loss_rate=stats.loss_rate,
                        closed_positions=stats.count,
                    )
                )
            report.sweeps.append(sweep)

    return report


__all__ = [
    "HoldoutLeak",
    "ParameterSweep",
    "SweepPoint",
    "SweepReport",
    "SweptParameter",
    "SWEPT_PARAMETERS",
    "run_sweep",
]
