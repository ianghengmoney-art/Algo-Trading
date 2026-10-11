"""§13.4 — the benchmark suite.

Each variant exists to answer one question: **does this piece of the design
earn its complexity?**  A variant that beats the full system is not an
embarrassment, it is the protocol working — it means a component should be
removed.

    vs index                    does any of this beat buying the market?
    vs equal-weight passers     does Module F's sizing add anything?
    vs no momentum overlay      does D5 add anything?
    vs single-anchor valuation  does C's dual triangulation earn its cost?
    vs v3 circular conviction   do the D2/D3 corrections improve outcomes,
                                or are they only tidier?
    vs no-C5                    does SINGLE-ANCHOR MODE add value, or does it
                                just admit trades that should be refused?

The seventh benchmark the spec names — **vs old GCFP v2 rules** — is not
implemented, because the v2 rules were not supplied.  v4 supersedes v2 without
restating it, and inventing a plausible v2 would produce a strawman that v4
beats by construction.  :data:`MISSING_BENCHMARKS` records the gap so the
report states it rather than quietly showing six of seven.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date
from typing import Callable, Sequence

from ..classification import Classification
from ..config import Config
from ..modules import d_conviction
from ..modules.d_conviction import ComponentScore, ConvictionScore, band_for
from ..modules.f_sizing import PortfolioState
from ..pipeline import Evaluation

#: Benchmarks §13.4 requires that this build cannot run, and why.
MISSING_BENCHMARKS: dict[str, str] = {
    "vs old GCFP v2 rules": (
        "the v2 specification was not supplied. v4 states that it supersedes "
        "v2 without restating it, and a reconstructed v2 would be a strawman "
        "rather than a benchmark. Supply the v2 rules to enable this comparison."
    ),
}


@dataclass(frozen=True)
class Variant:
    """One benchmark configuration."""

    name: str
    proves: str
    config: Config
    signal_filter: Callable[[Evaluation], bool] | None = None
    sizer: Callable[[Evaluation, PortfolioState, Config], float] | None = None
    conviction_scorer: Callable[..., ConvictionScore] | None = None
    #: Set for benchmarks that are not run through the engine at all.
    passive_symbol: str | None = None

    @property
    def is_passive(self) -> bool:
        return self.passive_symbol is not None


# -- v3's circular conviction scoring -------------------------------------


def v3_circular_conviction(
    data,
    market,
    config: Config,
    classification: Classification,
    health,
    triangulation,
    actual_discount: float,
    gate_threshold: float,
    discount_rate,
    momentum_returns=None,
    ledger=None,
) -> ConvictionScore:
    """v3's scoring, reconstructed to measure what the correction bought.

    Two components differ from v4, and both re-measure what Module E's gate has
    already consumed:

    * **Valuation** scored the *absolute* discount rather than the excess above
      the gate, so a name barely clearing its gate still scored 15/30.
    * **Anchor agreement** awarded a flat 15 points for "both anchors confirm" —
      which the gate already requires, so every passer scored 15 and the
      component carried no information at all.

    Everything else is held identical to v4, so a difference in outcome is
    attributable to these two components and not to some other drift.
    """
    components = [
        d_conviction.score_health_margin(data, health, config),
        # v3: absolute discount, full marks at 50%.
        ComponentScore(
            "valuation (v3 absolute)",
            min(max(actual_discount / 0.50, 0.0), 1.0) * 30.0,
            30.0,
            f"absolute discount {actual_discount:.1%} — re-measures what the "
            "gate already required",
        ),
        # v3: flat award for a fact the gate already guarantees.
        ComponentScore(
            "anchor agreement (v3 flat)",
            15.0 if triangulation.both_confirm_undervaluation else 0.0,
            15.0,
            "flat 15 for both anchors confirming — a fact Module E's gate "
            "already requires of every passer",
        ),
        d_conviction.score_business_quality(
            data, market, config, classification, discount_rate, ledger
        ),
        d_conviction.score_momentum(
            data.profile.symbol, momentum_returns or {}, config
        ),
    ]
    total = sum(c.points for c in components)
    return ConvictionScore(
        symbol=data.profile.symbol,
        total=total,
        components=components,
        band=band_for(total, config),
        # v3 had no C5, so no cap.
        capped_at=None,
        flags=["v3 circular scoring — benchmark variant only"],
    )


# -- sizers ---------------------------------------------------------------


def equal_weight_sizer(
    evaluation: Evaluation, portfolio: PortfolioState, config: Config
) -> float:
    """Every passer gets the same weight, whatever its conviction.

    Isolates Module F: if this matches or beats regime sizing, the sizing rules
    are decoration.
    """
    target_names = (
        config.sizing.core_target_names[1] + config.sizing.growth_target_names[1]
    )
    investable = 1.0 - config.sizing.ballast_target[0]
    return investable / target_names


# -- construction ---------------------------------------------------------


def build_variants(base: Config, benchmark_symbol: str = "^GSPC") -> list[Variant]:
    """The six runnable benchmarks."""
    single_anchor_config = dataclasses.replace(
        base,
        anchors=dataclasses.replace(
            base.anchors,
            # No peer set can ever be assembled, so C2 is never computable and
            # every name runs on C1 alone — under C5's penalties, which is the
            # honest version of a single-anchor world.
            peer_min=10_000,
        ),
    )
    no_momentum_config = dataclasses.replace(
        base,
        conviction=dataclasses.replace(base.conviction, momentum_max=0.0),
    )

    return [
        Variant(
            name="vs index (buy and hold)",
            proves="whether any of this beats buying the market",
            config=base,
            passive_symbol=benchmark_symbol,
        ),
        Variant(
            name="vs equal-weight all passers",
            proves="that Module F's regime sizing earns its rules",
            config=base,
            sizer=equal_weight_sizer,
        ),
        Variant(
            name="vs no momentum overlay",
            proves="that D5's tiebreaker adds something",
            config=no_momentum_config,
        ),
        Variant(
            name="vs single-anchor valuation",
            proves="that C's dual triangulation earns its complexity",
            config=single_anchor_config,
        ),
        Variant(
            name="vs v3 circular conviction scoring",
            proves="that the D2/D3 corrections improve outcomes, not just tidiness",
            config=base,
            conviction_scorer=v3_circular_conviction,
        ),
        Variant(
            name="vs no-C5 (reject single-anchor names)",
            proves=(
                "that SINGLE-ANCHOR MODE adds value rather than admitting bad trades"
            ),
            config=base,
            signal_filter=lambda e: not e.anchor_mode.is_single,
        ),
    ]


def passive_curve(
    prices: Sequence[tuple[date, float]], initial_capital: float
) -> list[tuple[date, float]]:
    """Buy-and-hold equity curve for the index benchmark."""
    if not prices:
        return []
    start_price = prices[0][1]
    if start_price <= 0:
        return []
    shares = initial_capital / start_price
    return [(day, shares * price) for day, price in prices]


__all__ = [
    "Variant",
    "MISSING_BENCHMARKS",
    "build_variants",
    "equal_weight_sizer",
    "passive_curve",
    "v3_circular_conviction",
]
