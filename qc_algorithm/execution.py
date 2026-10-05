"""The execution boundary -- the only module permitted to place orders.

Split deliberately into two halves:

  * ``plan_orders`` is pure. It turns Module F sizing decisions and Module E/H
    verdicts into explicit intents, and is unit-tested without QuantConnect.
    Every decision about *what* to do lives here, where it can be asserted on.
  * ``apply_orders`` is the thin, untestable-by-unit-test half that actually
    calls the broker API. It contains no decisions at all.

If that split ever blurs, the guarantee blurs with it: the point is that a
reader can audit the trading behaviour of this system by reading one pure
function, and confirm the other is a loop with no branches worth hiding in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from gcfp.config import GROWTH_SLEEVE, Params, SLEEVE_OF
from gcfp.modules.f_sizing import QUALIFIED_NO_HEADROOM, SizingDecision

OPEN = "OPEN"
TRIM = "TRIM"
EXIT = "EXIT"


@dataclass(frozen=True)
class OrderIntent:
    """One intended change, in percent of total portfolio."""

    symbol: str
    action: str
    target_weight_pct: float
    reason: str
    classification: Optional[str] = None
    conviction: Optional[float] = None

    @property
    def target_fraction(self) -> float:
        return self.target_weight_pct / 100.0


@dataclass
class ExecutionPlan:
    intents: list[OrderIntent] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    def by_action(self, action: str) -> list[OrderIntent]:
        return [i for i in self.intents if i.action == action]

    def describe(self) -> list[str]:
        lines = [
            f"{i.action:5s} {i.symbol:8s} -> {i.target_weight_pct:5.2f}% of portfolio  ({i.reason})"
            for i in self.intents
        ]
        lines += [f"SKIP  {sym}: {why}" for sym, why in self.skipped]
        return lines


def plan_orders(
    sizing_decisions: Sequence[SizingDecision],
    exits: Iterable[str],
    trims: Sequence[tuple[str, float]],
    current_weights_pct: dict[str, float],
    params: Params,
    current_growth_weight_pct: float = 0.0,
) -> ExecutionPlan:
    """Turn GCFP verdicts into order intents.

    Exits are applied first and unconditionally: a Module A failure or a
    both-anchors overvaluation verdict is not something to net against a new
    buy in the same name, and processing them first means a symbol can never
    be both exited and opened in one pass.

    ``trims`` carries the Module G TRIM-TO-CAP flags as ``(symbol, target
    weight)``. Under the Singapore default profile these carry no tax cost, so
    there is no reason to defer one.
    """
    plan = ExecutionPlan()
    exited: set[str] = set()

    for symbol in exits:
        plan.intents.append(
            OrderIntent(symbol, EXIT, 0.0, "SELL verdict: Module A failure or both anchors overvalued")
        )
        exited.add(symbol)

    for symbol, target_pct in trims:
        if symbol in exited:
            continue
        plan.intents.append(
            OrderIntent(symbol, TRIM, target_pct, "TRIM-TO-CAP: grown past the sleeve hard cap")
        )

    trimmed = {i.symbol for i in plan.intents if i.action == TRIM}

    for decision in sizing_decisions:
        symbol = decision.symbol
        if symbol in exited:
            plan.skipped.append((symbol, "exited this cycle; not re-opened in the same pass"))
            continue
        if symbol in trimmed:
            plan.skipped.append((symbol, "already being trimmed to cap this cycle"))
            continue
        if decision.status == QUALIFIED_NO_HEADROOM:
            plan.skipped.append((symbol, "QUALIFIED -- NO HEADROOM; held in cash, not forced in"))
            continue
        if decision.status != "SIZED" or decision.intended_portfolio_pct <= 0:
            plan.skipped.append((symbol, "; ".join(decision.reasons) or "not sized"))
            continue

        held = current_weights_pct.get(symbol, 0.0)
        if held > 0:
            plan.skipped.append((symbol, f"already held at {held:.2f}%; sizing up is not automated"))
            continue

        plan.intents.append(
            OrderIntent(
                symbol,
                OPEN,
                decision.intended_portfolio_pct,
                f"BUY: {decision.intended_sleeve_pct:.1f}% of the {decision.sleeve} sleeve",
                classification=decision.classification,
            )
        )

    _guard_growth_sleeve(plan, current_growth_weight_pct, params)
    return plan


def _guard_growth_sleeve(plan: ExecutionPlan, current_growth_weight_pct: float, params: Params) -> None:
    """Refuse any plan that would push the growth sleeve past its hard cap.

    Module F already sizes inside the cap, so this is a second, independent
    check at the last point before capital moves. Sizing drift toward
    CORE-sized growth positions is failure mode #9, and a cap enforced only
    at the point where sizing is computed is a cap enforced in one place.

    Openings are dropped lowest-conviction-first until the projection fits,
    rather than the whole batch being refused -- a batch that overshoots by a
    fraction of a percent should not cost the highest-conviction name in it.
    """
    cap = params.sizing.growth_sleeve_max_pct_of_portfolio
    opening = [
        i for i in plan.intents
        if i.action == OPEN and SLEEVE_OF.get(i.classification) == GROWTH_SLEEVE
    ]
    if not opening:
        return

    projected = current_growth_weight_pct + sum(i.target_weight_pct for i in opening)
    if projected <= cap:
        return

    # Smallest first: these carry the least conviction under Module F's bands.
    for intent in sorted(opening, key=lambda i: i.target_weight_pct):
        if projected <= cap:
            break
        plan.intents.remove(intent)
        plan.skipped.append(
            (intent.symbol, f"growth sleeve would reach {projected:.1f}% against a {cap:.0f}% hard cap")
        )
        projected -= intent.target_weight_pct

    plan.notices.append(
        f"GROWTH SLEEVE CAP ENFORCED AT THE EXECUTION BOUNDARY -- new growth exposure trimmed to "
        f"land inside the {cap:.0f}% cap."
    )


# ---------------------------------------------------------------------------
# The only code in this repository that can move capital.
# ---------------------------------------------------------------------------


def apply_orders(algorithm: Any, plan: ExecutionPlan, live_enabled: bool = True) -> list[str]:
    """Submit the planned intents. No decisions are taken here.

    ``live_enabled=False`` runs the whole pipeline and logs what it would have
    done without submitting anything, which is how the algorithm is expected to
    be run first.
    """
    log: list[str] = []
    for intent in plan.intents:
        if not live_enabled:
            log.append(f"DRY RUN would {intent.action} {intent.symbol} -> {intent.target_weight_pct:.2f}%")
            continue

        if intent.action == EXIT:
            algorithm.liquidate(intent.symbol, tag=intent.reason)
        else:
            algorithm.set_holdings(intent.symbol, intent.target_fraction, tag=intent.reason)
        log.append(f"{intent.action} {intent.symbol} -> {intent.target_weight_pct:.2f}% ({intent.reason})")
    return log
