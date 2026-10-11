"""Factor attribution: the regression recovers what it should."""

from __future__ import annotations

import math
import random

import pytest

from gcfp.backtest import attribution


def factors(months: list[int], seed: int = 1) -> dict[str, dict[int, float]]:
    rnd = random.Random(seed)
    out = {n: {} for n in ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom", "RF")}
    for m in months:
        for n in out:
            out[n][m] = 0.002 if n == "RF" else rnd.gauss(0.005, 0.04)
    return out


def month_keys(n: int) -> list[int]:
    keys, y, mo = [], 2015, 1
    for _ in range(n):
        keys.append(y * 100 + mo)
        mo += 1
        if mo == 13:
            y, mo = y + 1, 1
    return keys


class TestOls:
    def test_recovers_known_coefficients_exactly_without_noise(self):
        rnd = random.Random(3)
        x1 = [rnd.gauss(0, 1) for _ in range(60)]
        x2 = [rnd.gauss(0, 1) for _ in range(60)]
        y = [0.01 + 0.8 * a - 0.3 * b for a, b in zip(x1, x2)]
        fit = attribution.ols(y, [x1, x2])
        assert fit.coef == pytest.approx([0.01, 0.8, -0.3], abs=1e-10)
        assert fit.r2 == pytest.approx(1.0)

    def test_noise_gives_finite_standard_errors_and_sensible_t(self):
        rnd = random.Random(4)
        x = [rnd.gauss(0, 0.04) for _ in range(240)]
        y = [0.0 + 1.0 * a + rnd.gauss(0, 0.02) for a in x]
        fit = attribution.ols(y, [x])
        assert fit.coef[1] == pytest.approx(1.0, abs=0.1)
        assert abs(fit.t(0)) < 3
        assert fit.t(1) > 10
        assert all(math.isfinite(s) and s > 0 for s in fit.se)

    def test_collinear_factors_are_refused(self):
        x = [float(i % 7) for i in range(40)]
        with pytest.raises(ValueError):
            attribution.ols([float(i) for i in range(40)], [x, [2 * v for v in x]])


class TestAttribute:
    def test_a_pure_factor_portfolio_has_no_alpha(self):
        months = month_keys(120)
        f = factors(months)
        returns = {m: f["RF"][m] + f["Mkt-RF"][m] + 0.5 * f["HML"][m] + 0.4 * f["Mom"][m]
                   for m in months}
        (_, capm), _, (_, full) = attribution.attribute(returns, f)
        assert full.alpha_per_year == pytest.approx(0.0, abs=1e-9)
        assert full.coef[full.names.index("HML")] == pytest.approx(0.5)
        assert full.coef[full.names.index("Mom")] == pytest.approx(0.4)
        # The market-only model mistakes the factor premia for alpha.
        assert capm.alpha_per_year > 0.01

    def test_report_reads_a_curves_file(self, tmp_path):
        months = month_keys(36)
        f = factors(months, seed=2)
        path = tmp_path / "x-curves.csv"
        rows = ["date,strategy_value,sp500_level,equal_weight_universe,holdings"]
        value, index = 100.0, 1000.0
        rows.append("2014-12-31,100,1000,1.0,30")
        for m in months:
            index *= 1 + f["Mkt-RF"][m] + f["RF"][m]
            value *= 1 + f["Mkt-RF"][m] + f["RF"][m] + 0.3 * f["SMB"][m]
            rows.append(f"{m // 100}-{m % 100:02d}-28,{value},{index},1.0,30")
        path.write_text("\n".join(rows) + "\n")
        text = attribution.report([("test run", path)], f)
        assert "Fama-French 5 + momentum" in text
        assert "S&P 500 TR, for comparison" in text
        assert "alpha +0.00%/yr" in text


class TestDeflatedSharpe:
    def test_more_trials_lower_the_probability(self):
        rnd = random.Random(5)
        returns = [rnd.gauss(0.008, 0.04) for _ in range(128)]
        _, one = attribution.deflated_sharpe(returns, 1)
        _, eight = attribution.deflated_sharpe(returns, 8)
        assert 0 < eight < one < 1

    def test_noise_is_not_mistaken_for_skill(self):
        rnd = random.Random(6)
        returns = [rnd.gauss(0.0, 0.04) for _ in range(128)]
        _, p = attribution.deflated_sharpe(returns, 8)
        assert p < 0.5

    def test_too_few_months_gives_nothing(self):
        assert attribution.deflated_sharpe([0.01] * 10, 8) is None
