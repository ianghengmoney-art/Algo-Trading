"""A universe that includes the companies that later died.

The SEC ticker index lists companies that are registered *today*.  A
universe drawn from it holds only survivors, and a backtest over survivors
measures survival rather than the strategy: every bankruptcy, every
take-under, every quiet delisting has been removed before the first trade.

EDGAR's quarterly full index does not have that problem.  ``xbrl.idx`` lists
every XBRL filing made in a quarter, by CIK, whether or not the filer still
exists.  Reading it across the backtest period gives the set of companies that
were *actually filing* at each date — which is what a screen run on that date
would have seen.

What this cannot fix, and the report says so: a dead company is only
tradeable in the backtest if some price feed still serves its history.  The
free feeds mostly do not.  :func:`survivorship_coverage` measures how many of
the dead names could be priced, so the remaining bias is a number rather
than a footnote.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable

FULL_INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/xbrl.idx"

#: Periodic reports.  A company filing one of these was a live reporting
#: issuer on that date; 8-Ks and proxies alone do not make it screenable.
PERIODIC_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "10-KT", "10-QT"})

#: A filer counts as live if a periodic report landed within this many days.
#: A 10-K is due within 90 days of year end and quarters are 3 months apart,
#: so a healthy filer never goes this long without one; 400 days tolerates a
#: single late annual report without letting a dead filer linger.
DEFAULT_LOOKBACK_DAYS = 400

PIT_SAMPLE_SEED = 20240101


def parse_xbrl_index(text: str) -> dict[int, list[str]]:
    """``{cik: [ISO filing dates of periodic reports]}`` from one ``xbrl.idx``.

    The file is a free-text preamble, a header line, a line of dashes, then
    ``CIK|Company Name|Form Type|Date Filed|Filename`` rows.
    """
    out: dict[int, list[str]] = {}
    in_body = False
    for line in text.splitlines():
        if not in_body:
            if line.startswith("----"):
                in_body = True
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        cik_text, _, form, filed = (p.strip() for p in parts[:4])
        if form.upper() not in PERIODIC_FORMS or not cik_text.isdigit():
            continue
        try:
            date.fromisoformat(filed)
        except ValueError:
            continue
        out.setdefault(int(cik_text), []).append(filed)
    return out


def quarters_between(start: date, end: date) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    year, quarter = start.year, (start.month - 1) // 3 + 1
    while (year, quarter) <= (end.year, (end.month - 1) // 3 + 1):
        out.append((year, quarter))
        quarter += 1
        if quarter == 5:
            year, quarter = year + 1, 1
    return out


@dataclass
class FilerIndex:
    """Who filed periodic reports, and when, over a span of quarters."""

    #: ``{cik: sorted filing dates}``
    filings: dict[int, list[date]] = field(default_factory=dict)
    lookback_days: int = DEFAULT_LOOKBACK_DAYS

    @classmethod
    def from_quarters(cls, quarters: dict[tuple[int, int], dict[int, list[str]]],
                      lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> "FilerIndex":
        merged: dict[int, set[date]] = {}
        for rows in quarters.values():
            for cik, dates in rows.items():
                merged.setdefault(cik, set()).update(date.fromisoformat(d) for d in dates)
        return cls({cik: sorted(ds) for cik, ds in merged.items()}, lookback_days)

    def is_live(self, cik: int, as_of: date) -> bool:
        """Had this filer made a periodic report recently, as of ``as_of``?

        Uses only filings dated on or before ``as_of`` — nothing later can
        leak in, so a company that will die next year still counts as live
        today, exactly as a screen run today would have seen it.
        """
        floor = as_of - timedelta(days=self.lookback_days)
        return any(floor <= d <= as_of for d in self.filings.get(cik, ()))

    def ever_live(self, start: date, end: date) -> list[int]:
        """Every CIK that was live at some point in ``[start, end]``."""
        floor = start - timedelta(days=self.lookback_days)
        return sorted(
            cik for cik, ds in self.filings.items()
            if any(floor <= d <= end for d in ds)
        )

    def last_filing(self, cik: int) -> date | None:
        ds = self.filings.get(cik)
        return ds[-1] if ds else None

    def stopped_filing(self, cik: int, end: date) -> bool:
        """Did this filer go quiet before ``end``?  That is a death, a take-
        over, or a deregistration — the names a survivors-only list omits."""
        last = self.last_filing(cik)
        return last is not None and last < end - timedelta(days=self.lookback_days)


def load_filer_index(
    fetch_text: Callable[[str], str],
    start: date,
    end: date,
    *,
    cache_dir: Path | None = None,
    today: date | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    progress: Callable[[str], None] | None = None,
) -> FilerIndex:
    """Read ``xbrl.idx`` for every quarter from the lookback before ``start``
    through ``end``.

    Only the periodic-report rows are kept, as a small JSON per quarter.
    Closed quarters never change, so they are cached for good; the current
    quarter is cached for the day.
    """
    today = today or date.today()
    span_start = start - timedelta(days=lookback_days)
    span_end = min(end, today)
    current = (today.year, (today.month - 1) // 3 + 1)
    cache = Path(cache_dir) / "full_index" if cache_dir else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)

    quarters: dict[tuple[int, int], dict[int, list[str]]] = {}
    wanted = quarters_between(span_start, span_end)
    for n, (year, quarter) in enumerate(wanted, 1):
        stamp = f"_{today.isoformat()}" if (year, quarter) == current else ""
        path = cache / f"{year}Q{quarter}{stamp}.json" if cache else None
        if path and path.exists():
            raw = json.loads(path.read_text())
            rows = {int(k): v for k, v in raw.items()}
        else:
            text = fetch_text(FULL_INDEX_URL.format(year=year, quarter=quarter))
            rows = parse_xbrl_index(text)
            if path:
                path.write_text(json.dumps({str(k): v for k, v in rows.items()}))
        quarters[(year, quarter)] = rows
        if progress:
            progress(f"  filing index {year} Q{quarter} ({n}/{len(wanted)}): "
                     f"{len(rows)} periodic filers")
    return FilerIndex.from_quarters(quarters, lookback_days)


def cik_symbol(cik: int) -> str:
    return f"CIK{cik:010d}"


def symbol_cik(symbol: str) -> int | None:
    text = symbol.strip().upper()
    if text.startswith("CIK") and text[3:].isdigit():
        return int(text[3:])
    return None


def sample_symbols(index: FilerIndex, start: date, end: date, size: int,
                   seed: int = PIT_SAMPLE_SEED) -> list[str]:
    """A seeded random sample of everyone who filed during the period.

    Drawn from the whole period, not from today's list, so companies that
    died during it are in the sample at the rate they occurred.
    """
    pool = index.ever_live(start, end)
    rng = random.Random(seed)
    chosen = pool if size >= len(pool) else rng.sample(pool, size)
    return [cik_symbol(c) for c in sorted(chosen)]


@dataclass
class SurvivorshipCoverage:
    sampled: int
    died: int
    died_priced: int
    survived: int
    survived_priced: int

    @property
    def died_priced_rate(self) -> float | None:
        return self.died_priced / self.died if self.died else None

    def lines(self) -> list[str]:
        out = [
            "SURVIVORSHIP COVERAGE",
            f"  sampled from the SEC filing index (incl. dead companies): {self.sampled}",
            f"  stopped filing during the period (died/acquired/delisted): {self.died}",
            f"    of which a price history was found: {self.died_priced}",
            f"  still filing at the end: {self.survived} "
            f"(priced: {self.survived_priced})",
        ]
        rate = self.died_priced_rate
        if rate is not None and rate < 0.8:
            out.append(
                f"  WARNING: only {rate:.0%} of the companies that died could be "
                "priced. The unpriced ones could never be bought in this "
                "backtest, so the result still leans toward survivors. Treat "
                "the CAGR as an upper bound."
            )
        return out


def survivorship_coverage(index: FilerIndex, symbols: Iterable[str], end: date,
                          has_prices: Callable[[str], bool]) -> SurvivorshipCoverage:
    died = died_priced = survived = survived_priced = sampled = 0
    for symbol in symbols:
        cik = symbol_cik(symbol)
        if cik is None:
            continue
        sampled += 1
        priced = has_prices(symbol)
        if index.stopped_filing(cik, end):
            died += 1
            died_priced += priced
        else:
            survived += 1
            survived_priced += priced
    return SurvivorshipCoverage(sampled, died, died_priced, survived, survived_priced)
