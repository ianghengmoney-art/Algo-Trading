"""Universe construction, grouping statistics, and peer finding.

Three jobs the gates assume are already done, and which no free source hands
you finished:

* **The universe.**  A2 needs "the entire universe grouped by industry code",
  which means someone has to build that universe first.
* **Group medians.**  A2 compares a company's leverage to its grouping's
  median, and the ladder escalates on how many members had the metric
  *computable* — so the count travels with the median.
* **Peers.**  C2 wants 4-8 genuine peers.  Without a vendor peer list, they are
  found by screening the universe on grouping, size and growth — which is
  better than a vendor list anyway, because the screen is visible and the
  rejections are logged.

Building the universe is the expensive step: it touches every filer once.  The
snapshot is cached to disk so a weekly screen pays for it once a week, not
once per name.
"""

from __future__ import annotations

import json
import random
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence, Callable

from .analytics import group_median
from .classification import Classification
from .config import Config
from .data.adapter import DataAdapter, DataUnavailable
from .data.taxonomy import group_key
from .modules import a_health, c_anchors
from .types import CompanyData, MarketData, ReportingFrequency, TaxonomyLevel


@dataclass
class UniverseMember:
    """One company's screening facts.

    Deliberately small: this is held for a few thousand names at once, and
    everything here is either a screen input or a grouping statistic input.
    """

    symbol: str
    name: str
    market_cap: float | None
    adv_3m_usd: float | None
    sector: str | None
    industry: str | None
    sub_industry: str | None
    net_debt_to_ebitda: float | None
    gross_margin_stdev: float | None
    revenue_growth: float | None
    beta: float | None
    is_reit: bool = False
    is_bank: bool = False
    is_insurer: bool = False
    #: Files 20-F or 40-F rather than 10-Q.  Defaulted so universe snapshots
    #: saved before the field existed still load.
    is_foreign_filer: bool = False
    #: The source's industry code (SIC on EDGAR), for the wider grouping C2
    #: can fall back to when ``anchors.peer_group_fallback`` is on.
    industry_code: str | None = None
    #: Why this name was dropped, when it was.
    excluded_reason: str | None = None

    @property
    def included(self) -> bool:
        return self.excluded_reason is None

    def grouping_at(self, level: TaxonomyLevel) -> str | None:
        if level is TaxonomyLevel.SUB_INDUSTRY:
            return self.sub_industry
        if level is TaxonomyLevel.INDUSTRY:
            return self.industry
        if level is TaxonomyLevel.SECTOR:
            return self.sector
        return None


@dataclass
class Universe:
    """A screened snapshot, plus the grouping statistics A2 and D4 read."""

    as_of: date
    members: list[UniverseMember] = field(default_factory=list)
    source: str = "unknown"
    #: Names the source could not describe at all, kept so a shrinking
    #: universe is visible rather than silent.
    unreachable: list[str] = field(default_factory=list)

    @property
    def included(self) -> list[UniverseMember]:
        return [m for m in self.members if m.included]

    def by_symbol(self, symbol: str) -> UniverseMember | None:
        for member in self.members:
            if member.symbol == symbol:
                return member
        return None

    def group(self, key: str, level: TaxonomyLevel) -> list[UniverseMember]:
        return [m for m in self.included if m.grouping_at(level) == key]

    def market_data(self, base: MarketData | None = None) -> MarketData:
        """Fold the grouping statistics into a :class:`MarketData`.

        Medians are computed at every rung, because A2's ladder may land on
        any of them and the escalation decision needs each rung's computable
        count to be already known.
        """
        base = base or MarketData()
        leverage: dict[str, float] = {}
        counts: dict[str, int] = {}
        betas: dict[str, float] = {}
        margin_stdev: dict[str, float] = {}
        levels: dict[str, TaxonomyLevel] = {}

        for level in (
            TaxonomyLevel.SUB_INDUSTRY,
            TaxonomyLevel.INDUSTRY,
            TaxonomyLevel.SECTOR,
        ):
            keys = {
                m.grouping_at(level) for m in self.included if m.grouping_at(level)
            }
            for key in keys:
                members = self.group(key, level)
                median, count = group_median([m.net_debt_to_ebitda for m in members])
                # Rungs are walked finest-first, so the first median recorded
                # for a label is the finest one available.  A label that
                # appears at two rungs (an industry named like its sector)
                # keeps the finer.
                if median is not None and key not in leverage:
                    leverage[key] = median
                    counts[key] = count
                    levels[key] = level
                beta_median, _ = group_median([m.beta for m in members])
                if beta_median is not None and key not in betas:
                    betas[key] = beta_median
                stdev_median, _ = group_median(
                    [m.gross_margin_stdev for m in members]
                )
                if stdev_median is not None and key not in margin_stdev:
                    margin_stdev[key] = stdev_median

        return MarketData(
            risk_free_rate=base.risk_free_rate,
            risk_free_rate_date=base.risk_free_rate_date,
            fx_rates=dict(base.fx_rates),
            fx_rate_timestamp=base.fx_rate_timestamp,
            group_net_debt_ebitda_median=leverage,
            group_beta_median=betas,
            group_gross_margin_stdev_median=margin_stdev,
            group_member_counts=counts,
            group_taxonomy_level=levels,
            sector_cap_rates=dict(base.sector_cap_rates),
        )

    # -- persistence ------------------------------------------------------
    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "as_of": self.as_of.isoformat(),
                    "source": self.source,
                    "unreachable": self.unreachable,
                    "members": [asdict(m) for m in self.members],
                },
                indent=1,
            )
        )
        return path

    @classmethod
    def load(cls, path: Path) -> "Universe":
        payload = json.loads(Path(path).read_text())
        return cls(
            as_of=datetime.strptime(payload["as_of"], "%Y-%m-%d").date(),
            source=payload.get("source", "unknown"),
            unreachable=payload.get("unreachable", []),
            members=[UniverseMember(**m) for m in payload.get("members", [])],
        )

    def is_stale(self, max_age_days: int = 7, as_of: date | None = None) -> bool:
        return ((as_of or date.today()) - self.as_of).days > max_age_days


def _gross_margin_stdev(data: CompanyData) -> float | None:
    margins = []
    for row in data.trailing_quarters(8):
        if row.gross_profit is None or not row.revenue or row.revenue <= 0:
            continue
        margins.append(row.gross_profit / row.revenue)
    return statistics.stdev(margins) if len(margins) >= 8 else None


def _revenue_growth(data: CompanyData) -> float | None:
    return c_anchors.revenue_growth_yoy(data)


def build_universe(
    adapter: DataAdapter,
    symbols: Sequence[str],
    config: Config,
    *,
    as_of: date | None = None,
    progress: bool = False,
) -> Universe:
    """Screen ``symbols`` down to the investable universe.

    Applies the market-cap and liquidity floors and records a reason for every
    exclusion, so a universe that shrinks unexpectedly can be explained rather
    than re-run.

    This is the slow step — one pass over every filer. The result is meant to
    be cached and reused for a week.
    """
    as_of = as_of or date.today()
    universe = Universe(as_of=as_of, source=adapter.name)

    for index, symbol in enumerate(symbols):
        # Every 10, not every 100: each name can take several seconds when it
        # is not cached, so a 100-name gap was ten minutes of silence that
        # looked exactly like a hang.
        if progress and index % 10 == 0:
            print(f"  universe: {index}/{len(symbols)} ({symbol})", flush=True)
        try:
            # Pin the price window to the evaluation date. Without this the
            # adapter's default window is relative to *today*, which silently
            # returns no prices for any historical date — and a name with no
            # price has no market cap, so a whole backtest would screen to
            # nothing for a reason that looks like a data gap.
            data = adapter.load_company(
                symbol,
                price_start=as_of - timedelta(days=730),
                price_end=as_of,
            )
        except DataUnavailable as exc:
            universe.unreachable.append(f"{symbol}: {exc}")
            continue
        except Exception as exc:  # provider-specific transport failures
            universe.unreachable.append(f"{symbol}: {type(exc).__name__}: {exc}")
            continue

        profile = data.profile
        member = UniverseMember(
            symbol=symbol,
            name=profile.name,
            market_cap=profile.market_cap,
            adv_3m_usd=profile.adv_3m_usd,
            sector=profile.sector,
            industry=profile.industry,
            sub_industry=profile.sub_industry,
            net_debt_to_ebitda=a_health.net_debt_to_ebitda(data),
            gross_margin_stdev=_gross_margin_stdev(data),
            revenue_growth=_revenue_growth(data),
            beta=profile.beta,
            is_reit=profile.is_reit,
            is_bank=profile.is_bank,
            is_insurer=profile.is_insurer,
            is_foreign_filer=(
                profile.reporting_frequency is ReportingFrequency.SEMIANNUAL
            ),
            industry_code=profile.industry_code,
        )

        # Structural exclusions first, so the logged reason names the real
        # cause rather than whichever liquidity test happened to fail.
        if config.universe.exclude_reits and member.is_reit:
            member.excluded_reason = (
                "REIT — excluded by configuration: AFFO is not available from "
                "this data source, so the REIT path cannot value it"
            )
        elif config.universe.exclude_foreign_filers and member.is_foreign_filer:
            member.excluded_reason = (
                "foreign filer (20-F/40-F) — excluded by configuration: no "
                "quarterly reports, so the TTM gates and C1 cannot run"
            )
        elif member.market_cap is None:
            member.excluded_reason = "market cap not computable"
        elif member.market_cap < config.universe.min_market_cap_usd:
            member.excluded_reason = (
                f"market cap {member.market_cap:,.0f} below "
                f"{config.universe.min_market_cap_usd:,.0f}"
            )
        elif member.adv_3m_usd is None:
            member.excluded_reason = "3-month ADV not computable"
        elif member.adv_3m_usd < config.universe.min_adv_usd:
            member.excluded_reason = (
                f"ADV {member.adv_3m_usd:,.0f} below "
                f"{config.universe.min_adv_usd:,.0f}"
            )

        universe.members.append(member)

    return universe


def industry_group_key(member: UniverseMember) -> str | None:
    """The wider grouping C2 may fall back to: the two-digit SIC major
    group (e.g. "SIC 35xx", industrial machinery) around a four-digit
    industry (e.g. 3531, construction machinery)."""
    code = (member.industry_code or "").strip()
    return f"SIC {code[:2]}xx" if len(code) >= 2 and code[:2].isdigit() else None


def find_peers(
    universe: Universe,
    subject: UniverseMember,
    config: Config,
    adapter: DataAdapter,
    multiple: str,
    *,
    level: TaxonomyLevel = TaxonomyLevel.INDUSTRY,
    max_candidates: int = 40,
    as_of: date | None = None,
    grouping: Callable[[UniverseMember], str | None] | None = None,
) -> list[c_anchors.PeerCandidate]:
    """Assemble C2 candidates from the universe.

    Returns *candidates*, not a peer set: C2 applies the screen and logs a
    decision for every one.  The pre-filter here is only to keep the number of
    price fetches sane — it narrows by grouping and size, which C2 would reject
    on anyway, and never by anything C2 does not itself test.
    """
    if grouping is not None:
        key = grouping(subject)
        members = [m for m in universe.included if key and grouping(m) == key]
    else:
        key = subject.grouping_at(level)
        members = universe.group(key, level) if key else []
    if not key:
        return []

    low = config.anchors.peer_market_cap_low
    high = config.anchors.peer_market_cap_high

    nearby = [
        m for m in members
        if m.symbol != subject.symbol
        and m.market_cap
        and subject.market_cap
        and low <= m.market_cap / subject.market_cap <= high
    ]
    # Closest by size first, so the fetch budget is spent on the most likely
    # peers rather than on the edges of the band.
    nearby.sort(
        key=lambda m: abs((m.market_cap or 0) / (subject.market_cap or 1) - 1.0)
    )

    candidates: list[c_anchors.PeerCandidate] = []
    load_kwargs = (
        {"price_start": as_of - timedelta(days=730), "price_end": as_of}
        if as_of is not None
        else {}
    )
    for member in nearby[:max_candidates]:
        try:
            peer_data = adapter.load_company(member.symbol, **load_kwargs)
        except Exception:
            continue
        candidates.append(
            c_anchors.PeerCandidate(
                symbol=member.symbol,
                multiple=c_anchors.compute_current_multiple(peer_data, multiple),
                market_cap=member.market_cap,
                revenue_growth=member.revenue_growth,
                group=key,
            )
        )
    return candidates


#: Seed for the sampling below.  Fixed so two runs of the same size screen
#: the same names and their results can be compared; changing it is how you
#: deliberately look at a different slice.
SAMPLE_SEED = 20240101


def default_symbol_list(adapter: DataAdapter, limit: int | None = None) -> list[str]:
    """Every ticker the source indexes, as the raw input to the screen.

    EDGAR indexes every filer, which is the whole point — including the ones
    that later delisted, which is what makes a point-in-time backtest possible
    at all.

    ``limit`` takes a **random sample**, not the first N alphabetically.  The
    alphabetical slice is a trap: ``--limit 500`` screened A through ABX and
    reported its rejection rates as though they described the market.  Nothing
    about a company's ticker predicts its fundamentals, so a seeded sample is
    a genuine cross-section, and the seed keeps two runs of the same size
    comparable.  The sample is returned sorted so the run order is stable.
    """
    getter = getattr(adapter, "all_tickers", None)
    if getter is None:
        inner = getattr(adapter, "fundamentals", None)
        getter = getattr(inner, "all_tickers", None) if inner else None
    if getter is None:
        raise DataUnavailable(
            "symbol_list", f"{adapter.name} cannot enumerate tickers"
        )
    symbols = sorted(getter().keys())
    if limit is None or limit >= len(symbols):
        return symbols
    return sorted(random.Random(SAMPLE_SEED).sample(symbols, limit))


__all__ = [
    "Universe",
    "UniverseMember",
    "build_universe",
    "find_peers",
    "default_symbol_list",
    "SAMPLE_SEED",
]
