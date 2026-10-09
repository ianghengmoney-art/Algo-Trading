"""Factor attribution: how much of a strategy's return is known factor
exposure (beta) and how much is left over (alpha).

A strategy that ranks stocks on value, profitability and momentum will earn
those factors' premia whether or not it picks stocks well. The question an
allocator asks is what remains after them. This regresses the strategy's
monthly excess return on the factors Kenneth French publishes — the market,
size (SMB), value (HML), profitability (RMW), investment (CMA) and momentum
(Mom) — built from CRSP and Compustat with no survivorship bias:

    r - rf = alpha + b1 (Mkt-RF) + b2 SMB + b3 HML + b4 RMW + b5 CMA + b6 Mom + e

The intercept, alpha, is the return the factors do not explain. Standard
errors are Newey-West (3 lags), which allow for the autocorrelation and
uneven volatility monthly returns have. Pure Python, like the rest of the
package: no numpy.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .longhistory import Series, fetch_french, parse_french_csv

FIVE_FACTORS = "F-F_Research_Data_5_Factors_2x3"
MOMENTUM = "F-F_Momentum_Factor"
NEWEY_WEST_LAGS = 3

MODELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("CAPM (market only)", ("Mkt-RF",)),
    ("Fama-French 3 + momentum", ("Mkt-RF", "SMB", "HML", "Mom")),
    ("Fama-French 5 + momentum", ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom")),
)

FACTOR_NAMES = {
    "Mkt-RF": "market",
    "SMB": "small size",
    "HML": "value (book/price)",
    "RMW": "profitability",
    "CMA": "conservative investment",
    "Mom": "momentum",
}


# --------------------------------------------------------------------- algebra


def _inverse(m: list[list[float]]) -> list[list[float]]:
    """Gauss-Jordan inverse of a small square matrix."""
    n = len(m)
    a = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(m)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-14:
            raise ValueError("singular matrix: factors are collinear or too few months")
        a[col], a[pivot] = a[pivot], a[col]
        p = a[col][col]
        a[col] = [v / p for v in a[col]]
        for r in range(n):
            if r != col and a[r][col]:
                f = a[r][col]
                a[r] = [v - f * w for v, w in zip(a[r], a[col])]
    return [row[n:] for row in a]


@dataclass
class Fit:
    names: list[str]          # "alpha" first, then the factors
    coef: list[float]
    se: list[float]
    r2: float
    months: int
    residual_vol: float       # annualised

    def t(self, i: int) -> float:
        return self.coef[i] / self.se[i] if self.se[i] > 0 else float("nan")

    @property
    def alpha_per_year(self) -> float:
        return self.coef[0] * 12


def ols(y: list[float], xs: list[list[float]], lags: int = NEWEY_WEST_LAGS,
        names: list[str] | None = None) -> Fit:
    """y on an intercept and the columns ``xs`` (each a list as long as y),
    with Newey-West standard errors."""
    n, k = len(y), len(xs) + 1
    if n <= k + 2:
        raise ValueError(f"{n} months is too few for {k} coefficients")
    rows = [[1.0] + [x[i] for x in xs] for i in range(n)]
    xtx = [[sum(r[a] * r[b] for r in rows) for b in range(k)] for a in range(k)]
    xty = [sum(r[a] * yi for r, yi in zip(rows, y)) for a in range(k)]
    inv = _inverse(xtx)
    coef = [sum(inv[a][b] * xty[b] for b in range(k)) for a in range(k)]
    resid = [yi - sum(c * v for c, v in zip(coef, r)) for r, yi in zip(rows, y)]

    # Newey-West "meat": Bartlett-weighted autocovariances of x·e, each lag
    # entering as Γ + Γ'.
    meat = [[0.0] * k for _ in range(k)]
    for lag in range(lags + 1):
        w = 1.0 if lag == 0 else 1.0 - lag / (lags + 1)
        gamma = [[0.0] * k for _ in range(k)]
        for t in range(lag, n):
            u = resid[t] * resid[t - lag]
            ra, rb = rows[t], rows[t - lag]
            for a in range(k):
                for b in range(k):
                    gamma[a][b] += u * ra[a] * rb[b]
        for a in range(k):
            for b in range(k):
                meat[a][b] += w * (gamma[a][b] if lag == 0 else gamma[a][b] + gamma[b][a])
    cov = [[sum(inv[a][i] * meat[i][j] * inv[j][b] for i in range(k) for j in range(k))
            for b in range(k)] for a in range(k)]
    se = [math.sqrt(max(cov[a][a], 0.0)) for a in range(k)]

    mean_y = sum(y) / n
    ss_tot = sum((v - mean_y) ** 2 for v in y)
    ss_res = sum(e * e for e in resid)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    resid_sd = math.sqrt(ss_res / (n - k))
    return Fit(names=["alpha"] + (names or [f"x{i}" for i in range(1, k)]),
               coef=coef, se=se, r2=r2, months=n,
               residual_vol=resid_sd * math.sqrt(12))


# ------------------------------------------------------------------ the data


def load_curves(path: Path) -> dict[str, Series]:
    """Monthly returns from a run's ``*-curves.csv``: the strategy, the S&P
    500 total return and the equal-weight universe, keyed YYYYMM."""
    with Path(path).open() as fh:
        rows = list(csv.DictReader(fh))
    columns = {
        "strategy": "strategy_value",
        "S&P 500 TR": "sp500_level",
        "equal-weight universe": "equal_weight_universe",
    }
    out: dict[str, Series] = {name: {} for name in columns}
    for prev, row in zip(rows, rows[1:]):
        month = int(row["date"][:7].replace("-", ""))
        for name, col in columns.items():
            try:
                a, b = float(prev[col]), float(row[col])
            except (KeyError, ValueError):
                continue
            if a > 0:
                out[name][month] = b / a - 1
    return out


def load_factors(get: Callable[[str], bytes], cache_dir: Path | None) -> dict[str, Series]:
    """The five Fama-French factors, momentum and the risk-free rate."""
    five = parse_french_csv(fetch_french(FIVE_FACTORS, get, cache_dir))
    mom = parse_french_csv(fetch_french(MOMENTUM, get, cache_dir))
    table = {name.strip(): series for name, series in five.items()}
    for name, series in mom.items():
        table[name.strip()] = series
    missing = [n for n in ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF", "Mom")
               if n not in table]
    if missing:
        raise ValueError(f"French files lack {missing}")
    return table


def attribute(returns: Series, factors: dict[str, Series]) -> list[tuple[str, Fit]]:
    """Each model in MODELS fitted over the months both sides have."""
    out = []
    for label, names in MODELS:
        months = sorted(m for m in returns
                        if all(m in factors[n] for n in names) and m in factors["RF"])
        y = [returns[m] - factors["RF"][m] for m in months]
        xs = [[factors[n][m] for m in months] for n in names]
        out.append((label, ols(y, xs, names=list(names))))
    return out


# ------------------------------------------------------------------ the report


def _fit_lines(fit: Fit) -> list[str]:
    lines = [
        f"    alpha {fit.alpha_per_year:+.2%}/yr (t {fit.t(0):+.2f}) · "
        f"R² {fit.r2:.2f} · unexplained volatility {fit.residual_vol:.1%}/yr · "
        f"{fit.months} months"
    ]
    loads = []
    for i, name in enumerate(fit.names[1:], start=1):
        loads.append(f"{FACTOR_NAMES.get(name, name)} {fit.coef[i]:+.2f} (t {fit.t(i):+.1f})")
    lines.append("    loadings: " + " · ".join(loads))
    return lines


def report(runs: list[tuple[str, Path]], factors: dict[str, Series]) -> str:
    last = max(factors["Mkt-RF"])
    lines = [
        "=" * 78,
        "FACTOR ATTRIBUTION — how much of each run is known factors (beta), "
        "how much is left (alpha)",
        "=" * 78,
        "Monthly returns regressed on Kenneth French's US factors (CRSP/Compustat,",
        "no survivorship bias). Newey-West standard errors, 3 lags. Alpha is the",
        "return the factors do not explain, per year; |t| >= 2 is the usual bar",
        "for 'probably not luck', higher still allowing for the variants tried.",
        f"French data through {last // 100}-{last % 100:02d}; months after that are not used.",
    ]
    for label, path in runs:
        try:
            series = load_curves(path)
        except OSError as exc:
            lines += ["", f"{label}: curves not readable ({exc})"]
            continue
        lines += ["", f"{label}  [{path}]"]
        for name in ("strategy", "S&P 500 TR", "equal-weight universe"):
            if len(series.get(name, {})) < 24:
                continue
            fits = attribute(series[name], factors)
            if name == "strategy":
                for model, fit in fits:
                    lines.append(f"  {model}")
                    lines.extend(_fit_lines(fit))
            else:
                # Plumbing check and context: the full model only.
                model, fit = fits[-1]
                lines.append(f"  {name}, for comparison ({model})")
                lines.extend(_fit_lines(fit))
    lines += [
        "",
        "READING IT",
        "  The S&P 500 rows are a plumbing check: alpha near 0, market loading",
        "  near 1, R² near 1. If they are not, the data are misaligned.",
        "  A strategy whose alpha shrinks toward 0 as factors are added earned",
        "  those factors' premia, which index funds sell for a fraction of the",
        "  cost. What survives the last model, if |t| >= 2, is stock selection.",
        "=" * 78,
    ]
    return "\n".join(lines) + "\n"


def run(runs: list[tuple[str, Path]], get: Callable[[str], bytes],
        cache_dir: Path | None = None) -> str:
    return report(runs, load_factors(get, cache_dir))
