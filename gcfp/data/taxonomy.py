"""Peer grouping, and what to do when GICS is not on offer.

A2 and C2 both need companies grouped, and the spec writes both against GICS
sub-industry codes.  Two facts make that a place where a system quietly goes
wrong:

* Vendors ship their own taxonomies.  FMP, for instance, returns a ``sector``
  and an ``industry`` string of its own devising and no GICS code at all.
  Treating a vendor ``industry`` as though it were a GICS sub-industry silently
  widens A2's peer set and changes the median it is measured against.
* A2 already has a fallback ladder (sub-industry -> industry -> sector) for
  thin groupings.  The level actually used must be logged either way.

So the grouping a gate used is always a :class:`Grouping`, which carries the
rung it came from and whether that rung is genuinely GICS.  §18 stop condition
3 is evaluated off exactly this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..types import CompanyProfile, TaxonomyLevel

#: A2's ladder, finest first.
FALLBACK_LADDER: tuple[TaxonomyLevel, ...] = (
    TaxonomyLevel.SUB_INDUSTRY,
    TaxonomyLevel.INDUSTRY,
    TaxonomyLevel.SECTOR,
)


@dataclass(frozen=True)
class Grouping:
    """One resolved peer grouping, with its provenance attached."""

    key: str
    level: TaxonomyLevel
    is_gics: bool
    #: How many members had the metric in question computable.  A2 escalates
    #: when this falls below its minimum.
    member_count: int = 0
    #: False when the ladder was exhausted without any rung reaching the
    #: minimum.  Using such a grouping's median anyway would defeat the
    #: escalation: the reason for escalating was that the median was not
    #: trustworthy at that size.
    meets_minimum: bool = True

    @property
    def is_substitute(self) -> bool:
        """True when this is a vendor taxonomy standing in for GICS."""
        return not self.is_gics and self.level is not TaxonomyLevel.UNAVAILABLE

    def as_log_line(self) -> str:
        provenance = "GICS" if self.is_gics else "VENDOR-SUBSTITUTE"
        line = (
            f"grouping={self.key} level={self.level.value} "
            f"taxonomy={provenance} members={self.member_count}"
        )
        if not self.meets_minimum:
            line += " BELOW-MINIMUM (ladder exhausted)"
        return line


def group_key(profile: CompanyProfile, level: TaxonomyLevel) -> str | None:
    """The grouping label for a profile at one rung, or ``None`` if absent."""
    if level is TaxonomyLevel.SUB_INDUSTRY:
        return profile.gics_sub_industry_code or profile.sub_industry
    if level is TaxonomyLevel.INDUSTRY:
        return profile.industry
    if level is TaxonomyLevel.SECTOR:
        return profile.sector
    return None


def resolve_grouping(
    profile: CompanyProfile,
    member_counts: dict[str, int],
    minimum_members: int,
) -> Grouping:
    """Walk A2's ladder until a rung has enough computable members.

    ``member_counts`` maps a grouping label to how many of its members had the
    metric computable — the spec's escalation trigger is computability, not
    mere membership, so a sub-industry with 30 names but 5 computable
    net-debt/EBITDA figures escalates.
    """
    for level in FALLBACK_LADDER:
        key = group_key(profile, level)
        if not key:
            continue
        count = member_counts.get(key, 0)
        if count >= minimum_members:
            return Grouping(
                key=key,
                level=level,
                is_gics=profile.taxonomy_is_gics,
                member_count=count,
                meets_minimum=True,
            )

    # The ladder was exhausted without any rung reaching the minimum.  Return
    # the finest rung that exists at all so the caller can log a real label,
    # but mark it: A2 must report NOT_COMPUTABLE rather than fall back on the
    # very median the escalation was meant to avoid.
    for level in FALLBACK_LADDER:
        key = group_key(profile, level)
        if key:
            return Grouping(
                key=key,
                level=level,
                is_gics=profile.taxonomy_is_gics,
                member_count=member_counts.get(key, 0),
                meets_minimum=False,
            )

    return Grouping(
        key="<none>",
        level=TaxonomyLevel.UNAVAILABLE,
        is_gics=False,
        member_count=0,
        meets_minimum=False,
    )


@dataclass(frozen=True)
class TaxonomyAvailability:
    """§18 stop condition 3's finding, computed over the probe's test set."""

    gics_available: bool
    finest_level_available: TaxonomyLevel
    substitute_taxonomy: str | None
    detail: str

    @property
    def blocks_build(self) -> bool:
        """A2 and C2 both fail outright only if no rung at all exists."""
        return self.finest_level_available is TaxonomyLevel.UNAVAILABLE

    def as_report_line(self) -> str:
        if self.blocks_build:
            return (
                "STOP CONDITION 3 TRIPPED — no industry taxonomy available at any "
                "level; A2 sector-relative leverage and C2 peer matching both fail."
            )
        if self.gics_available:
            return (
                f"GICS available at {self.finest_level_available.value}; "
                "A2 and C2 run as specified."
            )
        return (
            f"GICS NOT available. Finest rung offered is "
            f"{self.finest_level_available.value} from "
            f"{self.substitute_taxonomy or 'an unnamed vendor taxonomy'}. "
            "A2 and C2 run against a substitute grouping — every result is "
            "logged VENDOR-SUBSTITUTE and the peer-gameability break criterion "
            "(Module I) applies with more force, not less."
        )


def assess_taxonomy(profiles: Sequence[CompanyProfile]) -> TaxonomyAvailability:
    """Summarise what the source actually offers across a set of companies."""
    if not profiles:
        return TaxonomyAvailability(
            gics_available=False,
            finest_level_available=TaxonomyLevel.UNAVAILABLE,
            substitute_taxonomy=None,
            detail="no profiles supplied",
        )

    gics = all(p.taxonomy_is_gics and p.gics_sub_industry_code for p in profiles)

    finest = TaxonomyLevel.UNAVAILABLE
    for level in reversed(FALLBACK_LADDER):
        if all(group_key(p, level) for p in profiles):
            finest = level
    # ``reversed`` walks sector -> industry -> sub_industry, so the last rung
    # that every profile satisfies ends up in ``finest``.

    vendor = None
    if not gics:
        exchanges = {p.exchange for p in profiles if p.exchange}
        vendor = "provider-defined taxonomy"
        if exchanges:
            vendor = f"provider-defined taxonomy (profiles from {', '.join(sorted(exchanges))})"

    missing = [p.symbol for p in profiles if not group_key(p, finest)] if finest is not TaxonomyLevel.UNAVAILABLE else [p.symbol for p in profiles]
    detail = (
        f"{len(profiles) - len(missing)}/{len(profiles)} profiles carry a "
        f"{finest.value} label"
        if finest is not TaxonomyLevel.UNAVAILABLE
        else "no profile carried any taxonomy label"
    )

    return TaxonomyAvailability(
        gics_available=gics,
        finest_level_available=finest,
        substitute_taxonomy=vendor,
        detail=detail,
    )


__all__ = [
    "Grouping",
    "TaxonomyAvailability",
    "FALLBACK_LADDER",
    "group_key",
    "resolve_grouping",
    "assess_taxonomy",
]
