"""Module A6's tags, and the regime split every downstream module keys off."""

from __future__ import annotations

from enum import Enum


class Classification(str, Enum):
    """Exactly one of these is assigned per company, and it determines
    the valuation method (Module B), the anchor multiple (C1), the buy
    threshold (E), and the sizing regime (F)."""

    CORE_STABLE = "CORE-STABLE"
    CORE_GROWTH = "CORE-GROWTH"
    SPEC_GROWTH = "SPEC-GROWTH"
    FINANCIAL_BANK = "FINANCIAL-BANK"
    REIT = "REIT"
    INSURER = "INSURER"

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.value


#: A6 ambiguity rule.  Structure-based tags outrank profitability-based ones,
#: because a REIT's accounting makes the CORE-STABLE tests meaningless
#: regardless of its growth rate.  Earlier in this tuple wins.
AMBIGUITY_PRIORITY: tuple[Classification, ...] = (
    Classification.REIT,
    Classification.INSURER,
    Classification.FINANCIAL_BANK,
    Classification.SPEC_GROWTH,
    Classification.CORE_GROWTH,
    Classification.CORE_STABLE,
)


class Regime(str, Enum):
    """Module F1.  The two sleeves never blend."""

    CORE = "CORE"
    GROWTH = "GROWTH"


#: Which sizing regime each classification belongs to (F1).
REGIME_OF: dict[Classification, Regime] = {
    Classification.CORE_STABLE: Regime.CORE,
    Classification.FINANCIAL_BANK: Regime.CORE,
    Classification.REIT: Regime.CORE,
    Classification.INSURER: Regime.CORE,
    Classification.CORE_GROWTH: Regime.GROWTH,
    Classification.SPEC_GROWTH: Regime.GROWTH,
}

#: Classifications whose A5 data-freshness requirement is the tighter one,
#: and whose universe screen demands the higher ADV floor.
GROWTH_ROUTED: frozenset[Classification] = frozenset(
    {Classification.CORE_GROWTH, Classification.SPEC_GROWTH}
)


def regime_of(classification: Classification) -> Regime:
    return REGIME_OF[classification]


def is_growth_routed(classification: Classification) -> bool:
    return classification in GROWTH_ROUTED


__all__ = [
    "Classification",
    "Regime",
    "AMBIGUITY_PRIORITY",
    "REGIME_OF",
    "GROWTH_ROUTED",
    "regime_of",
    "is_growth_routed",
]
