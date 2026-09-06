"""Instrumentation: turning "nothing passed" into "here is exactly why".

A screen that reports *zero passers* is ambiguous in the worst possible way.
It means either

* the market is expensive and the gates are correctly refusing (Prime
  Directive 6 — the system working), or
* a single unmapped XBRL tag killed four thousand companies at A5 before any
  of them reached a valuation.

Those two situations produce **identical output** without instrumentation, and
they call for opposite responses.  Everything in this module exists to tell
them apart.

None of this changes a verdict.  It records why verdicts happened, which is
the only way to know whether the system is being disciplined or broken.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Sequence

from .classification import Classification
from .config import Config
from .ledger import Outcome
from .modules.c_anchors import PeerDecision
from .types import TaxonomyLevel
from .pipeline import Evaluation

# -- 0.1 rejection taxonomy ----------------------------------------------

#: Reasons ordered by how early they occur.  A name is attributed to the
#: *first* thing that stopped it, because that is the only one you can act on:
#: fixing a later gate changes nothing while an earlier one still rejects.
STAGE_ORDER: tuple[str, ...] = (
    "UNREACHABLE",
    "UNIVERSE",
    "A5",
    "A1",
    "A2",
    "A3",
    "A4",
    "A6",
    "B",
    "C5",
    "C",
    "D",
    "E",
    "F",
    "PASSED",
)


@dataclass(frozen=True)
class Rejection:
    """One name's cause of death."""

    symbol: str
    stage: str
    #: A short, aggregatable cause — an XBRL field name, a gate branch, a
    #: condition — never free prose, so thousands of these can be counted.
    cause: str
    detail: str = ""

    def as_report_line(self) -> str:
        line = f"{self.symbol}: {self.stage} — {self.cause}"
        return f"{line} ({self.detail})" if self.detail else line


@dataclass
class RejectionLedger:
    """Every screened name, and where it died."""

    rejections: list[Rejection] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)

    def record(self, evaluation: Evaluation) -> Rejection | None:
        """Attribute one evaluation to its first blocking cause."""
        if evaluation.is_buy:
            self.passed.append(evaluation.symbol)
            return None

        rejection = _attribute(evaluation)
        self.rejections.append(rejection)
        return rejection

    def record_unreachable(self, symbol: str, reason: str) -> None:
        self.rejections.append(
            Rejection(symbol, "UNREACHABLE", "source could not describe it", reason)
        )

    @property
    def total(self) -> int:
        return len(self.rejections) + len(self.passed)

    def by_stage(self) -> list[tuple[str, int]]:
        counts = Counter(r.stage for r in self.rejections)
        ordered = [(s, counts[s]) for s in STAGE_ORDER if counts.get(s)]
        # Anything with an unrecognised stage still gets reported.
        ordered.extend(
            (s, n) for s, n in counts.items() if s not in STAGE_ORDER
        )
        return ordered

    def causes_for(self, stage: str, limit: int = 8) -> list[tuple[str, int]]:
        counts = Counter(r.cause for r in self.rejections if r.stage == stage)
        return counts.most_common(limit)

    @property
    def dominant_cause(self) -> tuple[str, str, int] | None:
        """The single cause killing the most names.

        This is the number worth acting on: if one absent field accounts for
        40% of the universe, that is a parsing fix worth thousands of
        candidates, not a market judgement.
        """
        if not self.rejections:
            return None
        counts = Counter((r.stage, r.cause) for r in self.rejections)
        (stage, cause), n = counts.most_common(1)[0]
        return stage, cause, n

    def as_report_lines(self) -> list[str]:
        lines = [
            "REJECTION TAXONOMY",
            f"  screened {self.total} · passed {len(self.passed)} · "
            f"rejected {len(self.rejections)}",
        ]
        if not self.rejections:
            lines.append("  nothing was rejected")
            return lines

        for stage, count in self.by_stage():
            share = count / self.total if self.total else 0.0
            lines.append(f"  {stage:<12} {count:>6}  ({share:.1%})")
            for cause, n in self.causes_for(stage):
                lines.append(f"      {n:>6}  {cause}")

        dominant = self.dominant_cause
        if dominant:
            stage, cause, n = dominant
            share = n / self.total if self.total else 0.0
            lines.append("")
            lines.append(
                f"  DOMINANT CAUSE: {stage} / {cause} — {n} names ({share:.1%})"
            )
            if share > 0.25 and stage in ("A5", "UNREACHABLE"):
                lines.append(
                    "  ** More than a quarter of the universe died on a data "
                    "problem, not a health or valuation judgement. Fix this "
                    "before reading anything else in the report: the screen is "
                    "not measuring the market, it is measuring the parser. **"
                )
            elif stage == "E":
                lines.append(
                    "  Names are reaching the buy gate and being refused on "
                    "price. That is the system working (Prime Directive 6), "
                    "not a fault."
                )
        return lines


def _attribute(evaluation: Evaluation) -> Rejection:
    """Find the first thing that stopped this name, and name it precisely."""
    symbol = evaluation.symbol

    # Module A: report the first blocking gate, in the order they run.
    for gate in evaluation.health.gates:
        if gate.passed:
            continue
        if gate.gate == "A5":
            fields = gate.detail.get("missing_fields") or []
            cause = (
                f"missing: {fields[0]}" if fields else (gate.reason or "data integrity")
            )
            return Rejection(symbol, "A5", cause, "; ".join(fields[1:]))
        if gate.outcome is Outcome.NOT_COMPUTABLE:
            # "not computable" alone hides the actionable part. The reason's
            # first clause names the input that was missing, which is what a
            # thousand identical rejections should be grouped by.
            reason = gate.reason or "not computable"
            cause = reason.split(";")[0].split(",")[0].strip()[:60]
            return Rejection(symbol, gate.gate, cause or "not computable", reason[:120])
        return Rejection(
            symbol,
            gate.gate,
            gate.branch or "failed",
            (gate.reason or "")[:120],
        )

    if evaluation.stopped_at:
        stage = evaluation.stopped_at
        reason = evaluation.stop_reason or ""
        if stage == "C5":
            return Rejection(symbol, "C5", "both anchors uncomputable", reason[:120])
        cause = reason.split(":")[0][:60] if reason else "stopped"
        return Rejection(symbol, stage, cause, reason[:120])

    # Reached a verdict but did not buy: attribute to the unmet buy condition.
    signal = evaluation.signal
    if signal is not None:
        unmet = [name for name, met in signal.conditions.items() if not met]
        if unmet:
            # Condition names embed their numbers; strip to the stable part.
            first = unmet[0]
            for key in ("discount", "anchors confirm", "conviction"):
                if key in first:
                    return Rejection(symbol, "E", f"{key} not met", first)
            return Rejection(symbol, "E", "buy condition not met", first)
        if evaluation.sizing is not None and not evaluation.sizing.qualified:
            return Rejection(
                symbol, "F", "no headroom", "; ".join(evaluation.sizing.constraints)
            )
        return Rejection(symbol, "E", signal.signal.value, "")

    return Rejection(symbol, "UNKNOWN", "no verdict and no stop reason")


# -- 0.2 peer availability ------------------------------------------------

#: Which C2 screen a rejection reason belongs to.  Matched on the phrases
#: ``select_peers`` writes, so a wording change there shows up here as
#: "other" rather than silently miscounting.
_PEER_CAUSES: tuple[tuple[str, str], ...] = (
    ("multiple not computable", "multiple not computable"),
    ("different grouping", "grouping mismatch"),
    ("market cap", "size band"),
    ("revenue growth", "growth band"),
    ("growth unavailable", "growth unavailable"),
    ("trimmed to peer_max", "trimmed (surplus)"),
)


def classify_peer_reason(reason: str) -> str:
    for needle, label in _PEER_CAUSES:
        if needle in reason:
            return label
    return "other"


@dataclass
class PeerDiagnostic:
    """Why C2 could or could not assemble a peer set, across a whole screen.

    C5 exists because a peer set sometimes cannot be built.  What it does not
    say is *which* of C2's three screens is doing the rejecting — and that is
    the difference between "this universe genuinely has no comparables" and
    "one band is set too tight".
    """

    attempts: int = 0
    formed: int = 0
    #: Counted per rejected candidate, not per subject: the question is which
    #: screen removes the most peers.
    causes: Counter = field(default_factory=Counter)
    #: Peer-set sizes actually achieved, for the shortfall distribution.
    set_sizes: list[int] = field(default_factory=list)

    def record(self, decisions: Sequence[PeerDecision], config: Config) -> None:
        self.attempts += 1
        kept = sum(1 for d in decisions if d.included)
        self.set_sizes.append(kept)
        if kept >= config.anchors.peer_min:
            self.formed += 1
        for decision in decisions:
            if not decision.included:
                self.causes[classify_peer_reason(decision.reason)] += 1

    @property
    def formation_rate(self) -> float | None:
        return self.formed / self.attempts if self.attempts else None

    @property
    def binding_constraint(self) -> tuple[str, int] | None:
        """The screen removing the most candidates."""
        return self.causes.most_common(1)[0] if self.causes else None

    @property
    def median_set_size(self) -> float | None:
        return statistics.median(self.set_sizes) if self.set_sizes else None

    def as_report_lines(self, config: Config) -> list[str]:
        lines = ["C2 PEER AVAILABILITY"]
        if not self.attempts:
            lines.append("  no peer sets attempted")
            return lines

        rate = self.formation_rate or 0.0
        lines.append(
            f"  formed a set (>= {config.anchors.peer_min} peers): "
            f"{self.formed}/{self.attempts} ({rate:.1%})"
        )
        if self.median_set_size is not None:
            lines.append(f"  median peers found: {self.median_set_size:.0f}")

        total_rejected = sum(self.causes.values())
        if total_rejected:
            lines.append("  candidates removed, by screen:")
            for cause, n in self.causes.most_common():
                lines.append(f"    {n:>6}  ({n / total_rejected:>5.1%})  {cause}")

        binding = self.binding_constraint
        if binding and rate < 0.5:
            cause, n = binding
            lines.append("")
            lines.append(
                f"  BINDING CONSTRAINT: {cause} ({n} candidates removed)."
            )
            lines.append(
                "  Fewer than half of names can form a peer set, so most run in "
                "SINGLE-ANCHOR MODE — a 10pp higher buy bar and conviction "
                "capped at 70. If this holds on real data it is a design-level "
                "finding (§18 stop condition 4), not a data gap: the screen "
                "above is the thing to change, and that is an annual-review "
                "decision."
            )
        return lines


# -- 0.4 conviction independence -----------------------------------------


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / (var_x * var_y) ** 0.5


@dataclass
class ConvictionIndependence:
    """Are the five score components measuring five things, or fewer?

    D's design assumes each component carries information the others do not —
    that is the whole correction v4 made to v3.  If two components correlate
    almost perfectly across a real universe, the 100-point scale is narrower
    than it looks and the weights are not doing what they appear to.
    """

    #: Component name -> its score across every evaluation seen.
    samples: dict[str, list[float]] = field(default_factory=dict)
    #: Correlation above this counts as redundant.
    threshold: float = 0.80
    #: Below this many scored names, correlations are noise: three points can
    #: produce r = -1.00 by coincidence. Reporting that as a finding would be
    #: exactly the overfitting §13.7 warns about, in a different costume.
    min_samples: int = 20

    def record(self, evaluation: Evaluation) -> None:
        if evaluation.conviction is None:
            return
        for component in evaluation.conviction.components:
            self.samples.setdefault(component.name, []).append(component.points)

    @property
    def sample_count(self) -> int:
        return min((len(v) for v in self.samples.values()), default=0)

    def correlations(self) -> list[tuple[str, str, float]]:
        names = sorted(self.samples)
        out: list[tuple[str, str, float]] = []
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                xs, ys = self.samples[a], self.samples[b]
                size = min(len(xs), len(ys))
                r = _pearson(xs[:size], ys[:size])
                if r is not None:
                    out.append((a, b, r))
        return sorted(out, key=lambda t: -abs(t[2]))

    @property
    def has_enough_samples(self) -> bool:
        return self.sample_count >= self.min_samples

    @property
    def redundant_pairs(self) -> list[tuple[str, str, float]]:
        if not self.has_enough_samples:
            return []
        return [t for t in self.correlations() if abs(t[2]) >= self.threshold]

    def as_report_lines(self) -> list[str]:
        lines = ["CONVICTION COMPONENT INDEPENDENCE"]
        if self.sample_count < 3:
            lines.append(
                f"  only {self.sample_count} scored name(s) — too few to correlate"
            )
            return lines

        lines.append(f"  scored names: {self.sample_count}")
        if not self.has_enough_samples:
            lines.append(
                f"  correlations shown for reference only — fewer than "
                f"{self.min_samples} scored names, so any value here is noise "
                "and no redundancy verdict is given."
            )
        for a, b, r in self.correlations():
            marker = (
                "  <- REDUNDANT"
                if self.has_enough_samples and abs(r) >= self.threshold
                else ""
            )
            lines.append(f"    {r:+.2f}  {a} <-> {b}{marker}")

        redundant = self.redundant_pairs
        if redundant:
            lines.append("")
            lines.append(
                f"  {len(redundant)} pair(s) correlate above {self.threshold:.2f}. "
                "Those components are measuring substantially the same thing, so "
                "the 100-point scale carries less information than its weights "
                "imply — and the double-counting D2/D3 were corrected to remove "
                "may have reappeared elsewhere."
            )
        return lines


# -- assembly -------------------------------------------------------------


@dataclass
class ScreenDiagnostics:
    """Everything the instrumentation collected during one screen."""

    rejections: RejectionLedger = field(default_factory=RejectionLedger)
    peers: PeerDiagnostic = field(default_factory=PeerDiagnostic)
    conviction: ConvictionIndependence = field(default_factory=ConvictionIndependence)

    def record(
        self,
        evaluation: Evaluation,
        peer_decisions: Sequence[PeerDecision] | None,
        config: Config,
    ) -> None:
        self.rejections.record(evaluation)
        self.conviction.record(evaluation)
        if peer_decisions:
            self.peers.record(peer_decisions, config)

    def as_report_lines(self, config: Config) -> list[str]:
        lines: list[str] = []
        lines.extend(self.rejections.as_report_lines())
        lines.append("")
        lines.extend(self.peers.as_report_lines(config))
        lines.append("")
        lines.extend(self.conviction.as_report_lines())
        return lines


@dataclass
class UniversePeerAvailability:
    """How many names in the whole universe could form a C2 peer set.

    Stop condition 4 asks whether the dual-anchor premise holds for a data
    source and universe.  It has been answering that from the §18 roster —
    nine companies chosen precisely because they are extreme.  The largest
    chipmaker on earth has no size-matched peer; neither does the largest US
    bank, nor a near-monopoly land trust.  A high single-anchor rate across
    that roster is expected and says nothing about the market.

    This measures the same question across every name in the universe sample,
    using market caps and industries already fetched, so it costs no requests.
    It is an *upper bound* on peer availability: it applies C2's grouping and
    size screens, which are the ones computable from a universe row, and not
    the growth band or the multiple test, which need fundamentals.  A name
    counted here as having peers may still fail C2; a name counted as having
    none cannot pass it.
    """

    #: Names assessed — every included universe member with a market cap.
    assessed: int = 0
    #: How many same-industry names inside the size band each one has.
    counts: list[int] = field(default_factory=list)
    #: Names with no same-industry company at all in the sample.
    no_industry_peers: int = 0
    #: How many names the sample holds in each industry.  Without this the
    #: headline rate cannot be trusted: a sample carrying eight names per
    #: industry cannot produce four size-band peers for anyone, and would
    #: report scarcity that belongs to the sample rather than the market.
    group_sizes: dict[str, int] = field(default_factory=dict)
    #: Each subject's in-band peers as a share of its industry's sampled
    #: members.  This is the sample-independent quantity — it estimates what
    #: fraction of an industry sits within the size band of a given member,
    #: and does not shrink just because fewer names were fetched.
    in_band_shares: list[float] = field(default_factory=list)
    #: How many symbols were handed to the universe build, so the report can
    #: say what ``--peer-sample`` would actually settle the question rather
    #: than leaving the reader to guess.
    requested: int = 0

    @property
    def would_be_single_anchor(self) -> int:
        return sum(1 for c in self.counts if c < self._minimum)

    @property
    def rate(self) -> float | None:
        if not self.assessed:
            return None
        return self.would_be_single_anchor / self.assessed

    _minimum: int = 4

    @staticmethod
    def _median(values: Sequence[float]) -> float:
        return statistics.median(values) if values else 0.0

    @property
    def median_group_size(self) -> float:
        return self._median(list(self.group_sizes.values()))

    @property
    def median_in_band_share(self) -> float:
        return self._median(self.in_band_shares)

    @property
    def industry_size_needed(self) -> float | None:
        """How many listed names an industry needs before a peer set forms.

        The sample-independent answer to stop condition 4.  At the observed
        size dispersion, a member finds ``share`` of its industry inside the
        band, so it needs ``peer_min / share`` industry members to reach the
        minimum.  Compare that against how many names a real industry has —
        not against how many this sample happened to fetch.
        """
        share = self.median_in_band_share
        if share <= 0:
            return None
        return self._minimum / share

    @property
    def suggested_peer_sample(self) -> int | None:
        """The ``--peer-sample`` that would make this measurement meaningful.

        Scales the sample that was actually requested by how far short the
        per-industry depth fell.  Most fetched names never reach the universe
        because they fail the size or liquidity floors, so this works from the
        observed survival rate rather than assuming one.
        """
        needed = self.industry_size_needed
        if not needed or not self.requested or self.median_group_size <= 0:
            return None
        if self.median_group_size >= needed:
            return None
        scale = needed / self.median_group_size
        return int(round(self.requested * scale / 50.0) * 50) or None

    @property
    def sample_limited(self) -> bool:
        """Whether the sample is too thin for the headline rate to mean much."""
        needed = self.industry_size_needed
        return needed is not None and self.median_group_size < needed

    def report_lines(self) -> list[str]:
        if not self.assessed:
            return ["  universe peer availability: not measured"]
        pct = (self.rate or 0.0) * 100
        median_peers = self._median([float(c) for c in self.counts])
        lines = [
            f"  assessed {self.assessed} universe names (not just the roster)",
            f"  would fall into SINGLE-ANCHOR MODE: {pct:.0f}%",
            f"  median size-band peers available: {median_peers:.0f}",
            f"  no same-industry name at all: {self.no_industry_peers}",
            f"  median names sampled per industry: {self.median_group_size:.0f}",
        ]
        needed = self.industry_size_needed
        if needed is not None:
            lines.append(
                f"  size dispersion: {self.median_in_band_share:.0%} of an "
                f"industry's members fall inside one member's size band, so an "
                f"industry needs about {needed:.0f} listed names before a "
                f"{self._minimum}-name peer set is available"
            )
        if self.sample_limited:
            lines.append(
                "  SAMPLE-LIMITED — this sample carries fewer names per "
                "industry than that, so the rate above measures the sample, "
                "not the market, and must not be read as a design finding."
            )
            suggested = self.suggested_peer_sample
            if suggested:
                lines.append(f"  re-run with --peer-sample {suggested}")
        else:
            lines.append(
                "  the sample carries enough names per industry for this rate "
                "to describe the market rather than the sample"
            )
        lines.append(
            "  upper bound — grouping and size only; the growth band and the "
            "multiple test can only reduce these counts further"
        )
        return lines


def measure_universe_peer_availability(
    universe: "Universe", config: Config
) -> UniversePeerAvailability:
    """Apply C2's computable screens to every universe member.

    Costs nothing: market cap and industry are already on each row.
    """
    out = UniversePeerAvailability(_minimum=config.anchors.peer_min)
    low = config.anchors.peer_market_cap_low
    high = config.anchors.peer_market_cap_high

    members = [m for m in universe.included if m.market_cap]
    by_group: dict[str, list[float]] = {}
    for m in members:
        key = m.grouping_at(TaxonomyLevel.INDUSTRY) or m.sector
        if key:
            by_group.setdefault(key, []).append(m.market_cap)

    for m in members:
        key = m.grouping_at(TaxonomyLevel.INDUSTRY) or m.sector
        caps = by_group.get(key or "", [])
        if len(caps) <= 1:
            out.no_industry_peers += 1
            out.counts.append(0)
            out.assessed += 1
            continue
        in_band = sum(
            1
            for cap in caps
            if cap is not m.market_cap and low <= cap / m.market_cap <= high
        )
        out.counts.append(in_band)
        out.in_band_shares.append(in_band / (len(caps) - 1))
        out.assessed += 1

    out.group_sizes = {k: len(v) for k, v in by_group.items()}
    return out


__all__ = [
    "UniversePeerAvailability",
    "measure_universe_peer_availability",
    "ConvictionIndependence",
    "PeerDiagnostic",
    "Rejection",
    "RejectionLedger",
    "ScreenDiagnostics",
    "STAGE_ORDER",
    "classify_peer_reason",
]
