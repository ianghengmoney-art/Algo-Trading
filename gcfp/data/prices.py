"""Free price sources.

EDGAR holds filings, not quotes, so prices come from elsewhere.  Two free
options, deliberately both:

* **Stooq** serves plain CSV over a stable URL with no key.  It is the sturdier
  of the two and is tried first.
* **Yahoo** (the endpoint ``yfinance`` wraps) is richer — it carries splits and
  index quotes — but it is an unofficial interface that changes without notice.

Neither is a contract.  A free price feed *will* break at some point, so
:class:`FallbackPriceSource` tries them in order and reports which one answered,
and every source returns ``None`` rather than a guess when it cannot help.  A
silently stale price is worse than a missing one: A5 catches the missing one.
"""

from __future__ import annotations

import csv
import io
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from ..types import CorporateAction, CorporateActionType, PricePoint
from .adapter import DataUnavailable

STOOQ_URL = "https://stooq.com/q/d/l/"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

#: Yahoo's symbol for the 10-year US Treasury yield, quoted in percent.
TEN_YEAR_YIELD_SYMBOL = "^TNX"

#: How far back a split lookup reaches. One wide request per symbol, filtered
#: locally, instead of one request per window asked for.
SPLIT_HISTORY_START = date(1990, 1, 1)
#: The benchmark D5 momentum and the beta regression are measured against.
BENCHMARK_SYMBOL = "^GSPC"


class PriceSource(ABC):
    """Daily closes, and whatever corporate actions the source knows about."""

    name: str = "abstract"

    @abstractmethod
    def get_prices(
        self, symbol: str, start: date, end: date
    ) -> Sequence[PricePoint]:
        """Newest-first daily bars."""

    def get_splits(self, symbol: str, start: date, end: date) -> Sequence[CorporateAction]:
        """Splits only.

        A source that cannot report splits must raise rather than return an
        empty tuple: C1.1 reads an empty list as "no splits occurred", and a
        missed split leaves a step change in the multiple series that C1.2 then
        reports as a re-rating.
        """
        raise DataUnavailable("splits", f"{self.name} does not report splits")

    def get_index_level(self, symbol: str) -> float | None:
        """Latest level for an index or yield symbol, if the source has one."""
        raise DataUnavailable("index", f"{self.name} does not serve index quotes")


def _session(existing: Any = None) -> Any:
    if existing is not None:
        return existing
    import requests

    return requests.Session()


@dataclass
class StooqPriceSource(PriceSource):
    """Plain CSV, no key, no rate limit worth worrying about.

    Stooq suffixes US tickers with ``.us`` and serves oldest-first CSV.
    """

    session: Any = None
    timeout: float = 20.0
    max_retries: int = 3
    name: str = "stooq"

    def __post_init__(self) -> None:
        self.session = _session(self.session)

    def _fetch(self, params: dict[str, str]) -> str:
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(STOOQ_URL, params=params, timeout=self.timeout)
            except Exception as exc:
                last = exc
                time.sleep(2.0**attempt)
                continue
            if resp.status_code >= 500:
                last = RuntimeError(f"HTTP {resp.status_code}")
                time.sleep(2.0**attempt)
                continue
            resp.raise_for_status()
            return resp.text
        raise DataUnavailable("prices", f"stooq exhausted retries: {last}")

    def get_prices(
        self, symbol: str, start: date, end: date
    ) -> Sequence[PricePoint]:
        text = self._fetch(
            {
                "s": f"{symbol.lower()}.us",
                "i": "d",
                "d1": start.strftime("%Y%m%d"),
                "d2": end.strftime("%Y%m%d"),
            }
        )
        if not text.strip() or text.lstrip().lower().startswith("no data"):
            raise DataUnavailable("prices", f"stooq has no data for {symbol}")

        rows = list(csv.DictReader(io.StringIO(text)))
        out: list[PricePoint] = []
        for row in rows:
            try:
                day = datetime.strptime(row["Date"], "%Y-%m-%d").date()
                close = float(row["Close"])
            except (KeyError, ValueError, TypeError):
                continue
            volume = None
            try:
                volume = float(row.get("Volume") or 0) or None
            except (TypeError, ValueError):
                pass
            out.append(
                PricePoint(
                    price_date=day,
                    close=close,
                    # Stooq's series is already split-adjusted; it publishes no
                    # separate unadjusted close, so the two are the same here.
                    adjusted_close=close,
                    volume=volume,
                )
            )
        if not out:
            raise DataUnavailable("prices", f"stooq returned no usable rows for {symbol}")
        return tuple(sorted(out, key=lambda p: p.price_date, reverse=True))


@dataclass
class YahooPriceSource(PriceSource):
    """Richer, but unofficial and liable to change without warning."""

    session: Any = None
    timeout: float = 20.0
    max_retries: int = 3
    name: str = "yahoo"

    def __post_init__(self) -> None:
        self.session = _session(self.session)

    def _fetch(self, symbol: str, params: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(
                    YAHOO_CHART_URL.format(symbol=symbol),
                    params=params,
                    timeout=self.timeout,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; gcfp/4.0)"},
                )
            except Exception as exc:
                last = exc
                time.sleep(2.0**attempt)
                continue
            if resp.status_code == 404:
                raise DataUnavailable("prices", f"yahoo has no symbol {symbol}")
            if resp.status_code in (429,) or resp.status_code >= 500:
                last = RuntimeError(f"HTTP {resp.status_code}")
                time.sleep(2.0 ** (attempt + 1))
                continue
            resp.raise_for_status()
            payload = resp.json()
            result = (payload.get("chart") or {}).get("result") or []
            if not result:
                raise DataUnavailable("prices", f"yahoo returned no result for {symbol}")
            return result[0]
        raise DataUnavailable("prices", f"yahoo exhausted retries: {last}")

    def _chart(self, symbol: str, start: date, end: date) -> dict[str, Any]:
        return self._fetch(
            symbol,
            {
                "period1": int(datetime(start.year, start.month, start.day).timestamp()),
                "period2": int(datetime(end.year, end.month, end.day).timestamp()) + 86400,
                "interval": "1d",
                "events": "div,split",
            },
        )

    def get_prices(
        self, symbol: str, start: date, end: date
    ) -> Sequence[PricePoint]:
        result = self._chart(symbol, start, end)
        stamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        adjclose_block = ((result.get("indicators") or {}).get("adjclose") or [{}])[0]
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []
        adjcloses = adjclose_block.get("adjclose") or []

        out: list[PricePoint] = []
        for i, stamp in enumerate(stamps):
            close = closes[i] if i < len(closes) else None
            if close is None:
                continue  # Yahoo pads holidays and halts with nulls.
            out.append(
                PricePoint(
                    price_date=datetime.utcfromtimestamp(stamp).date(),
                    close=float(close),
                    adjusted_close=(
                        float(adjcloses[i])
                        if i < len(adjcloses) and adjcloses[i] is not None
                        else None
                    ),
                    volume=(
                        float(volumes[i])
                        if i < len(volumes) and volumes[i] is not None
                        else None
                    ),
                )
            )
        if not out:
            raise DataUnavailable("prices", f"yahoo returned no usable bars for {symbol}")
        return tuple(sorted(out, key=lambda p: p.price_date, reverse=True))

    def get_splits(
        self, symbol: str, start: date, end: date
    ) -> Sequence[CorporateAction]:
        result = self._chart(symbol, start, end)
        splits = ((result.get("events") or {}).get("splits") or {}).values()
        out: list[CorporateAction] = []
        for split in splits:
            stamp = split.get("date")
            numerator = split.get("numerator")
            denominator = split.get("denominator")
            if stamp is None or not denominator:
                continue
            ratio = float(numerator) / float(denominator)
            out.append(
                CorporateAction(
                    action_type=(
                        CorporateActionType.SPLIT
                        if ratio >= 1
                        else CorporateActionType.REVERSE_SPLIT
                    ),
                    effective_date=datetime.utcfromtimestamp(stamp).date(),
                    ratio=ratio,
                    description=split.get("splitRatio"),
                )
            )
        return tuple(out)

    def get_index_level(self, symbol: str) -> float | None:
        end = date.today()
        prices = self.get_prices(symbol, end - timedelta(days=10), end)
        return prices[0].close if prices else None


@dataclass
class FallbackPriceSource(PriceSource):
    """Try each source in order; report which one answered.

    A free feed breaking is a matter of when, not if, so the fallback is part
    of the design rather than an afterthought.  ``last_source_used`` is
    recorded so a report can say where a price came from — two sources
    disagreeing is a thing worth being able to notice.
    """

    sources: Sequence[PriceSource] = ()
    name: str = "fallback"
    last_source_used: str | None = field(default=None, repr=False)
    failures: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.sources:
            self.sources = (StooqPriceSource(), YahooPriceSource())

    def _try(self, method: str, *args, **kwargs):
        errors: list[str] = []
        for source in self.sources:
            try:
                result = getattr(source, method)(*args, **kwargs)
            except Exception as exc:
                errors.append(f"{source.name}: {type(exc).__name__}: {exc}")
                self.failures[source.name] = str(exc)
                continue
            self.last_source_used = source.name
            return result
        raise DataUnavailable(method, "; ".join(errors) or "no sources configured")

    def get_prices(self, symbol: str, start: date, end: date) -> Sequence[PricePoint]:
        return self._try("get_prices", symbol, start, end)

    def get_splits(self, symbol: str, start: date, end: date) -> Sequence[CorporateAction]:
        return self._try("get_splits", symbol, start, end)

    def get_index_level(self, symbol: str) -> float | None:
        return self._try("get_index_level", symbol)


@dataclass
class CachedPriceSource(PriceSource):
    """A disk cache in front of another price source.

    Without it an interrupted run started from nothing on the price side:
    SEC filings were already cached, but every price series was downloaded
    again, so a laptop going to sleep halfway through a 1,400-company probe
    cost the whole run.  With it, re-running the same command picks up where
    the last one stopped — everything already fetched comes off disk.

    A cached series is reused when it covers the requested window and either
    the window ends before the day it was fetched (history does not change)
    or it was fetched today (anything reaching the present is refreshed
    daily).  A symbol the source could not price is remembered for the day
    too, so a re-run does not retry hundreds of dead tickers.
    """

    inner: PriceSource = field(default=None)  # type: ignore[assignment]
    cache_dir: Path = field(default=Path(".cache/prices"))
    name: str = field(default="", init=False)
    #: Parsed files kept in memory. A backtest asks for the same company's
    #: prices several times a month for ten years; re-reading and re-parsing
    #: the file each time was most of the CPU in a run.
    _memory: dict[str, dict | None] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.inner is None:
            raise ValueError("CachedPriceSource needs a source to wrap")
        # Reports name the underlying feed, not the cache.
        self.name = self.inner.name
        self.cache_dir = Path(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in symbol.upper())
        return self.cache_dir / f"{safe}.json"

    def _load(self, symbol: str) -> dict | None:
        if symbol in self._memory:
            return self._memory[symbol]
        path = self._path(symbol)
        payload = None
        if path.exists():
            try:
                payload = json.loads(path.read_text())
            except (OSError, ValueError):
                payload = None
        self._memory[symbol] = payload
        return payload

    def get_prices(self, symbol: str, start: date, end: date) -> Sequence[PricePoint]:
        today = date.today().isoformat()
        cached = self._load(symbol)
        if cached is not None:
            fetched_on = cached.get("fetched_on", "")
            current = fetched_on == today or end.isoformat() < fetched_on
            if cached.get("unavailable") and fetched_on == today:
                raise DataUnavailable("prices", cached["unavailable"])
            if (
                current
                and not cached.get("unavailable")
                and cached["start"] <= start.isoformat()
                and cached["end"] >= end.isoformat()
            ):
                return [
                    PricePoint(
                        price_date=date.fromisoformat(r[0]),
                        close=r[1],
                        adjusted_close=r[2],
                        volume=r[3],
                    )
                    for r in cached["rows"]
                    if start.isoformat() <= r[0] <= end.isoformat()
                ]
            # Widen to the union so a later, longer request does not refetch
            # what a shorter one already had.
            if current and not cached.get("unavailable"):
                start = min(start, date.fromisoformat(cached["start"]))
                end = max(end, date.fromisoformat(cached["end"]))

        try:
            points = list(self.inner.get_prices(symbol, start, end))
        except DataUnavailable as exc:
            self._save(symbol, {"fetched_on": today, "unavailable": str(exc)})
            raise
        self._save(
            symbol,
            {
                "fetched_on": today,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "rows": [
                    [p.price_date.isoformat(), p.close, p.adjusted_close, p.volume]
                    for p in points
                ],
            },
        )
        return points

    def _save(self, symbol: str, payload: dict) -> None:
        self._memory[symbol] = payload
        try:
            self._path(symbol).write_text(json.dumps(payload))
        except OSError:
            pass  # a cache that cannot be written is a slower run, not a wrong one

    def get_splits(self, symbol: str, start: date, end: date) -> Sequence[CorporateAction]:
        """Splits, fetched once per symbol per day over the whole history and
        filtered to the window.

        Uncached, a backtest asked the feed for the same company's splits at
        every monthly rebalance; with the feed throttling and the client
        backing off, those calls alone could outlast a six-hour job.
        """
        key = f"{symbol}.splits"
        today = date.today().isoformat()
        cached = self._load(key)
        stale = cached is None or (
            cached.get("fetched_on") != today
            # A past "unavailable" is a rate limit or an outage, not a fact.
            and (cached.get("unavailable") or end.isoformat() >= cached.get("fetched_on", ""))
        )
        if stale:
            try:
                actions = self.inner.get_splits(symbol, SPLIT_HISTORY_START, date.today())
            except DataUnavailable as exc:
                cached = {"fetched_on": today, "unavailable": str(exc)}
            else:
                cached = {
                    "fetched_on": today,
                    "rows": [
                        [a.action_type.value, a.effective_date.isoformat(), a.ratio,
                         a.description]
                        for a in actions
                    ],
                }
            self._save(key, cached)
        if cached.get("unavailable"):
            raise DataUnavailable("splits", cached["unavailable"])
        return tuple(
            CorporateAction(
                action_type=CorporateActionType(r[0]),
                effective_date=date.fromisoformat(r[1]),
                ratio=r[2],
                description=r[3],
            )
            for r in cached["rows"]
            if start.isoformat() <= r[1] <= end.isoformat()
        )

    def get_index_level(self, symbol: str) -> float | None:
        # A live yield, read once per run; caching it would only risk staleness.
        return self.inner.get_index_level(symbol)


def average_dollar_volume(prices: Sequence[PricePoint], days: int = 63) -> float | None:
    """Dollar ADV over roughly three months of trading.

    The universe screen is written in dollars; a share count would let a
    penny stock through on volume alone.
    """
    usable = [
        p for p in sorted(prices, key=lambda p: p.price_date, reverse=True)[:days]
        if p.volume is not None and p.close
    ]
    if len(usable) < days // 2:
        return None
    return sum(p.close * (p.volume or 0.0) for p in usable) / len(usable)


__all__ = [
    "PriceSource",
    "StooqPriceSource",
    "YahooPriceSource",
    "FallbackPriceSource",
    "CachedPriceSource",
    "average_dollar_volume",
    "TEN_YEAR_YIELD_SYMBOL",
    "BENCHMARK_SYMBOL",
]
