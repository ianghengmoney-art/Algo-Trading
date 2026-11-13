"""The long-history premise check registered in docs/FACTOR_STRATEGY.md.

The SEC's machine-readable financials begin in 2009-2011, so the stock-level
backtest cannot see the 1990s, the dot-com unwind or 2008. Kenneth French's
data library can: monthly returns, from 1963, of portfolios sorted on the
very characteristics the factor strategy ranks on, built from CRSP and
Compustat and free of survivorship bias. This module downloads those
portfolios, forms the registered proxies, and reports them against the
market. It tests the strategy's premise, not the strategy itself.
"""

from __future__ import annotations

import io
import math
import re
import statistics
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

FRENCH_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/{name}_CSV.zip"

#: Registered in docs/FACTOR_STRATEGY.md.
COST_PER_YEAR = 0.005
THIRTY_YEARS = (199501, 202512)
DECADES = ((199501, 200412), (200501, 201412), (201501, 202512))
WINDOWS = (("dot-com unwind 2000-02", 200001, 200212),
           ("financial crisis 2008-09", 200801, 200912))

FILES = {
    "market": "F-F_Research_Data_Factors",
    "ep": "6_Portfolios_ME_EP_2x3",
    "cfp": "6_Portfolios_ME_CFP_2x3",
    "op": "6_Portfolios_ME_OP_2x3",
    "mom": "6_Portfolios_ME_Prior_12_2",
    "ac": "Portfolios_Formed_on_AC",
    "ni": "Portfolios_Formed_on_NI",
}

Series = dict[int, float]  # YYYYMM -> monthly return as a decimal


def parse_french_csv(text: str) -> dict[str, Series]:
    """The first monthly table of a French CSV: ``{column: {YYYYMM: return}}``.

    The files are a free-text preamble, then a header row starting with a
    comma, then ``YYYYMM, v1, v2, ...`` rows in percent, then a blank line
    before the next table (equal-weighted, annual, counts...). Missing values
    are -99.99 or -999.
    """
    columns: list[str] | None = None
    out: dict[str, Series] = {}
    started = False
    for raw in text.splitlines():
        line = raw.strip()
        if columns is None:
            if line.startswith(","):
                columns = [c.strip() for c in line.split(",")[1:]]
                out = {c: {} for c in columns}
            continue
        parts = [p.strip() for p in line.split(",")]
        if not parts or not re.fullmatch(r"\d{6}", parts[0]):
            if started:
                break
            continue
        started = True
        month = int(parts[0])
        for name, value in zip(columns, parts[1:]):
            try:
                v = float(value)
            except ValueError:
                continue
            if v <= -99.99:
                continue
            out[name][month] = v / 100.0
    return out


def fetch_french(
    name: str, get: Callable[[str], bytes], cache_dir: Path | None = None
) -> str:
    """The CSV inside one French zip, cached on disk for the day."""
    if cache_dir is not None:
        cache_dir = Path(cache_dir) / "french"
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{name}.csv"
        if path.exists():
            import datetime as _dt

            fetched = _dt.date.fromtimestamp(path.stat().st_mtime)
            if (_dt.date.today() - fetched).days < 7:
                return path.read_text(errors="replace")
    payload = get(FRENCH_URL.format(name=name))
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        member = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
        text = archive.read(member).decode("latin-1")
    if cache_dir is not None:
        (cache_dir / f"{name}.csv").write_text(text)
    return text


def pick(table: dict[str, Series], *, size: str | None, high: bool, tag: str) -> Series | None:
    """A column such as "BIG HiEP": size "BIG"/"SMALL" (or None for a
    univariate file), the high or low group, and the characteristic tag."""
    want_side = "hi" if high else "lo"
    for name, series in table.items():
        key = name.replace(" ", "").lower()
        if size is not None and not key.startswith(size.lower()):
            continue
        if tag.lower() in key and want_side in key:
            return series
    return None


def mean_series(parts: list[Series]) -> Series:
    """Month-by-month mean over the series that have that month."""
    parts = [p for p in parts if p]
    months = sorted(set().union(*parts)) if parts else []
    return {
        m: statistics.fmean(p[m] for p in parts if m in p)
        for m in months if any(m in p for p in parts)
    }


def common(parts: list[Series]) -> Series:
    """Equal-weight mix over months every part has."""
    parts = [p for p in parts if p]
    if not parts:
        return {}
    months = sorted(set.intersection(*(set(p) for p in parts)))
    return {m: statistics.fmean(p[m] for p in parts) for m in months}


def net_of_costs(series: Series, per_year: float = COST_PER_YEAR) -> Series:
    monthly = (1 - per_year) ** (1 / 12) - 1
    return {m: (1 + r) * (1 + monthly) - 1 for m, r in series.items()}


def window(series: Series, start: int, end: int) -> Series:
    return {m: r for m, r in series.items() if start <= m <= end}


def cagr(series: Series) -> float | None:
    if len(series) < 12:
        return None
    growth = math.prod(1 + r for r in series.values())
    return growth ** (12 / len(series)) - 1 if growth > 0 else -1.0


def max_drawdown(series: Series) -> float:
    level = peak = 1.0
    worst = 0.0
    for m in sorted(series):
        level *= 1 + series[m]
        peak = max(peak, level)
        worst = min(worst, level / peak - 1)
    return worst


def t_stat(excess: list[float]) -> float | None:
    if len(excess) < 12:
        return None
    sd = statistics.stdev(excess)
    return statistics.fmean(excess) / (sd / len(excess) ** 0.5) if sd > 0 else None


@dataclass
class Proxy:
    name: str
    series: Series
    note: str = ""


def build_proxies(tables: dict[str, dict[str, Series]]) -> tuple[Series, list[Proxy]]:
    """The market, and the registered proxies in big and small stocks."""
    factors = tables["market"]
    market = {
        m: factors["Mkt-RF"][m] + factors["RF"][m]
        for m in factors.get("Mkt-RF", {}) if m in factors.get("RF", {})
    }
    proxies: list[Proxy] = []
    for size, label in (("BIG", "big companies"), ("SMALL", "small companies")):
        value = mean_series([
            pick(tables.get("ep", {}), size=size, high=True, tag="ep") or {},
            pick(tables.get("cfp", {}), size=size, high=True, tag="cfp") or {},
        ])
        profit = pick(tables.get("op", {}), size=size, high=True, tag="op") or {}
        momentum = pick(tables.get("mom", {}), size=size, high=True, tag="prior") or {}
        a = common([value, profit, momentum])
        proxies.append(Proxy(f"Proxy A, {label}", net_of_costs(a)))

        extra, missing = [], []
        accruals = pick(tables.get("ac", {}), size=None, high=False, tag="20")
        (extra if accruals else missing).append(accruals or "low accruals")
        issuance = tables.get("ni", {}).get("<= 0") or pick(
            tables.get("ni", {}), size=None, high=False, tag="20"
        )
        (extra if issuance else missing).append(issuance or "low net issuance")
        b = common([value, profit, momentum, *[e for e in extra if isinstance(e, dict)]])
        note = (
            "low accruals and buybacks from all-size value-weighted portfolios "
            "(dominated by large companies)"
            + (f"; missing: {', '.join(m for m in missing if isinstance(m, str))}"
               if any(isinstance(m, str) for m in missing) else "")
        )
        proxies.append(Proxy(f"Proxy B, {label}", net_of_costs(b), note))
    return market, proxies


def compare(proxy: Series, market: Series, start: int, end: int) -> dict:
    p, m = window(proxy, start, end), window(market, start, end)
    months = sorted(set(p) & set(m))
    p = {k: p[k] for k in months}
    m = {k: m[k] for k in months}
    pc, mc = cagr(p), cagr(m)
    return {
        "months": len(months),
        "first": months[0] if months else None,
        "last": months[-1] if months else None,
        "proxy": pc,
        "market": mc,
        "excess": (pc - mc) if pc is not None and mc is not None else None,
        "t": t_stat([p[k] - m[k] for k in months]),
        "proxy_dd": max_drawdown(p) if p else None,
        "market_dd": max_drawdown(m) if m else None,
    }


def _pct(x: float | None) -> str:
    return f"{x:+.2%}" if x is not None else "n/a"


def report(market: Series, proxies: list[Proxy]) -> tuple[list[str], bool | None]:
    """The registered report, and whether big-company Proxy A supports the
    premise (None when the data was insufficient to say)."""
    lines = [
        "=" * 78,
        "LONG-HISTORY PREMISE CHECK — Kenneth French data library (CRSP/Compustat)",
        "=" * 78,
        "Tests the idea behind the factor strategy, not the strategy itself.",
        f"Each proxy is net of an assumed {COST_PER_YEAR:.1%}/yr trading cost.",
        "Market = French's US market return including dividends (Mkt-RF + RF).",
        "",
    ]
    first = min(market) if market else None
    periods = [("30 years 1995-2025", *THIRTY_YEARS)]
    if first:
        periods.append(("full history", first, 999912))
    verdict: bool | None = None
    for proxy in proxies:
        lines.append(proxy.name + (f"  ({proxy.note})" if proxy.note else ""))
        rows = {}
        for label, start, end in periods:
            r = compare(proxy.series, market, start, end)
            rows[label] = r
            span = (f"{str(r['first'])[:4]}-{str(r['last'])[:4]}"
                    if r["first"] else "no data")
            lines.append(
                f"  {label:26s} ({span}): proxy {_pct(r['proxy'])}/yr · market "
                f"{_pct(r['market'])}/yr · excess {_pct(r['excess'])}/yr · t "
                + (f"{r['t']:+.2f}" if r['t'] is not None else "n/a")
                + f" · worst fall {_pct(r['proxy_dd'])} vs {_pct(r['market_dd'])}"
            )
        decade_wins = 0
        decade_rows = []
        for start, end in DECADES:
            r = compare(proxy.series, market, start, end)
            ahead = r["excess"] is not None and r["excess"] > 0
            decade_wins += ahead
            decade_rows.append(
                f"    {str(start)[:4]}-{str(end)[:4]}: proxy {_pct(r['proxy'])} · "
                f"market {_pct(r['market'])} · excess {_pct(r['excess'])}"
            )
        lines.append("  by decade:")
        lines += decade_rows
        for name, start, end in WINDOWS:
            r = compare(proxy.series, market, start, end)
            lines.append(
                f"  {name}: proxy {_pct(r['proxy'])}/yr vs market "
                f"{_pct(r['market'])}/yr (worst fall {_pct(r['proxy_dd'])} vs "
                f"{_pct(r['market_dd'])})"
            )
        if proxy.name == "Proxy A, big companies":
            thirty = rows.get("30 years 1995-2025", {}).get("excess")
            full = next(
                (v.get("excess") for k, v in rows.items() if k.startswith("full")), None
            )
            if thirty is None or full is None:
                verdict = None
            else:
                verdict = thirty > 0 and full > 0 and decade_wins >= 2
            lines.append(
                f"  REGISTERED CONDITIONS: beats the market 1995-2025 "
                f"[{'PASS' if thirty and thirty > 0 else 'FAIL'}] · over the full "
                f"history [{'PASS' if full and full > 0 else 'FAIL'}] · in at least "
                f"2 of 3 decades [{'PASS' if decade_wins >= 2 else 'FAIL'}: "
                f"{decade_wins} of 3]"
            )
        lines.append("")
    lines.append(
        "VERDICT: "
        + ("SUPPORTS the premise — the idea held over 30+ years, not just 2015-2025."
           if verdict else
           "DOES NOT SUPPORT the premise — the stock-level result may be luck or "
           "specific to this decade." if verdict is False else
           "NOT DETERMINED — the data could not be fetched or was too short.")
    )
    lines.append("=" * 78)
    return lines, verdict


def run(get: Callable[[str], bytes], cache_dir: Path | None = None) -> tuple[str, bool | None]:
    """Download, build and report. Files that fail are skipped and named."""
    tables: dict[str, dict[str, Series]] = {}
    failed: list[str] = []
    for key, name in FILES.items():
        try:
            tables[key] = parse_french_csv(fetch_french(name, get, cache_dir))
        except Exception as exc:
            failed.append(f"{name} ({type(exc).__name__}: {exc})")
    if "market" not in tables:
        return ("LONG-HISTORY PREMISE CHECK: not run — the market file could not be "
                "fetched: " + "; ".join(failed) + "\n"), None
    market, proxies = build_proxies(tables)
    lines, verdict = report(market, proxies)
    if failed:
        lines.insert(-1, "Files that could not be fetched: " + "; ".join(failed))
    return "\n".join(lines) + "\n", verdict


__all__ = [
    "build_proxies",
    "compare",
    "fetch_french",
    "parse_french_csv",
    "report",
    "run",
]
