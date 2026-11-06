"""The nine prime directives are never relaxable, so they get their own tests.

These are the assertions that should fail loudly if a future change quietly
trades one away — which is the failure mode the directives exist to prevent.
"""

from __future__ import annotations

import ast
import re
from datetime import date
from pathlib import Path

import pytest

from gcfp.classification import Classification
from gcfp.config import DEFAULT_CONFIG
from gcfp.ledger import AuditLedger, Outcome
from gcfp.modules import a_health, b_valuation
from gcfp.modules.b_valuation import MethodRefused

PACKAGE = Path(__file__).resolve().parent.parent / "gcfp"

#: Libraries that can place an order.  Directive 1 says the system must be
#: *incapable* of executing, which means none of these may appear anywhere in
#: the dependency tree — not merely that they go unused.
TRADING_LIBRARIES = {
    "alpaca", "alpaca_trade_api", "ib_insync", "ibapi", "ccxt", "robin_stocks",
    "tda", "td_ameritrade", "schwab", "oandapyV20", "backtrader", "zipline",
    "freqtrade", "interactivebrokers", "kiteconnect", "binance", "coinbase",
    "e_trade", "pyetrade", "tradier", "questrade", "fix", "quickfix",
}


def _python_files():
    return [p for p in PACKAGE.rglob("*.py")]


def test_directive_1_no_trading_library_in_the_dependency_tree():
    """No trading library anywhere, in imports or in requirements."""
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                if name.lower() in TRADING_LIBRARIES:
                    offenders.append(f"{path.name}: {name}")
    assert not offenders, f"trading libraries imported: {offenders}"

    requirements = PACKAGE.parent / "requirements.txt"
    if requirements.exists():
        listed = {
            re.split(r"[=<>\[]", line.strip())[0].lower().replace("-", "_")
            for line in requirements.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        }
        assert not (listed & TRADING_LIBRARIES), (
            f"trading libraries in requirements: {listed & TRADING_LIBRARIES}"
        )


def test_directive_1_signals_carry_no_execution_surface():
    """A Signal is a message. It must expose nothing order-shaped."""
    from gcfp.modules.e_triggers import Signal

    forbidden = {"execute", "submit", "place_order", "send", "broker", "order", "fill"}
    surface = {name.lower() for name in dir(Signal) if not name.startswith("_")}
    assert not (surface & forbidden), f"Signal exposes {surface & forbidden}"


def test_directive_2_health_before_value(market, config):
    """A name failing Module A never reaches valuation."""
    from tests.conftest import build_company, stable_profile
    from gcfp.modules.f_sizing import PortfolioState
    from gcfp.modules.k_currency import FxTable
    from gcfp.pipeline import CandidateInputs, evaluate_candidate

    # Going-concern language is an A4 disqualifier.
    sick = build_company(going_concern_language=True)
    ev = evaluate_candidate(
        sick, market, config, CandidateInputs(current_multiple=13.0),
        PortfolioState(total_value=1e6), FxTable({"USDSGD": 1.29}, date.today()),
    )
    assert ev.stopped_at == "A"
    assert ev.fair_value is None, "valuation ran on a company that failed Module A"


def test_directive_3_method_refuses_never_approximates(market, config):
    """P/E on negative earnings and DCF on negative cash flow are refused."""
    from tests.conftest import build_company

    negative_fcf = build_company(
        annual_kw=dict(net_income=-2e9, operating_cash_flow=-1e9, free_cash_flow=-3e9)
    )
    with pytest.raises(MethodRefused):
        b_valuation.value_b1_core_stable(negative_fcf, market, config)

    # A bank may not be routed to a DCF, whatever the caller asks for.
    assert b_valuation.METHOD_FOR[Classification.FINANCIAL_BANK] == "B3"
    assert b_valuation.METHOD_FOR[Classification.REIT] == "B4"


def test_directive_3_pe_on_negative_earnings_is_none_not_a_number():
    """compute_current_multiple returns None rather than a nonsense P/E."""
    from gcfp.modules.c_anchors import compute_current_multiple
    from tests.conftest import build_company

    loss_making = build_company(quarterly_kw=dict(net_income=-500e6))
    assert compute_current_multiple(loss_making, "trailing_pe") is None


def test_directive_4_anchors_are_never_averaged(healthy_company, config):
    """A divergent pair produces no combined verdict."""
    from gcfp.modules import c_anchors

    c1 = c_anchors.AnchorReading("C1", True, 14.0, 20.0, implied_discount=0.30)
    c2 = c_anchors.AnchorReading("C2", True, 14.0, 15.0, implied_discount=-0.10)
    result = c_anchors.triangulate(healthy_company, config, c1, c2)

    assert result.anchors_disagree
    # The conservative reading is one of the two, never their mean.
    assert result.conservative_discount == pytest.approx(-0.10)
    assert result.conservative_discount != pytest.approx((0.30 - 0.10) / 2)


def test_directive_5_price_alone_never_triggers(config):
    """Momentum is scored but is never a gate, and a price move alone is not
    a sell trigger."""
    from gcfp.modules.d_conviction import score_momentum
    from gcfp.modules.e_triggers import SignalType, evaluate_sell
    from gcfp.modules.c_anchors import AnchorMode, AnchorReading, TriangulationResult

    score = score_momentum("A", {"A": 0.9, "B": 0.1, "C": 0.2}, config)
    assert score.max_points == 10.0, "momentum must stay a tiebreaker, not a gate"

    # Price far above fair value, but the anchors do not confirm.
    fv = b_valuation.FairValue(
        symbol="X", classification=Classification.CORE_STABLE, method="B1",
        fair_value_per_share=100.0, currency="USD",
    )
    tri = TriangulationResult(
        symbol="X",
        c1=AnchorReading("C1", True, 20.0, 18.0, implied_discount=0.10),
        c2=AnchorReading("C2", True, 20.0, 18.0, implied_discount=0.10),
        c3_pegy=None, c3_flag=False, mode=AnchorMode.DUAL,
        anchors_disagree=False, divergence=0.0, conservative_discount=0.10,
    )
    signal = evaluate_sell(
        "X", Classification.CORE_STABLE, 200.0, fv, tri, config, module_a_failed=False
    )
    assert signal.signal is not SignalType.SELL, (
        "a 100% premium alone triggered a sell without anchor confirmation"
    )


def test_directive_6_no_forced_deployment(config):
    """A thin screen holds the gap in ballast rather than concentrating."""
    from gcfp.classification import Regime
    from gcfp.modules.f_sizing import below_minimum_handling

    message = below_minimum_handling(2, Regime.CORE, config)
    assert message is not None
    assert "BALLAST" in message
    assert below_minimum_handling(8, Regime.CORE, config) is None


def test_directive_7_sleeve_regimes_never_blend():
    """Every classification maps to exactly one regime and one bucket."""
    from gcfp.classification import REGIME_OF, Regime
    from gcfp.modules.f_sizing import Bucket, bucket_for

    assert set(REGIME_OF) == set(Classification)
    for classification in Classification:
        bucket = bucket_for(classification)
        assert bucket is not Bucket.BALLAST, (
            f"{classification.value} was counted as ballast; a stock pick is "
            "never ballast (F2)"
        )
        if REGIME_OF[classification] is Regime.CORE:
            assert bucket is Bucket.CORE_PICKS
        else:
            assert bucket is Bucket.GROWTH_PICKS


def test_directive_8_parameters_have_a_single_source_and_a_fingerprint(config):
    """A parameter change is visible as a fingerprint change."""
    import dataclasses

    changed = dataclasses.replace(
        config, triggers=dataclasses.replace(config.triggers, min_conviction_to_buy=50.0)
    )
    assert changed.fingerprint != config.fingerprint


def test_directive_8_cooling_off_is_enforced(config):
    """A sleeve cap increase waits longer than an ordinary parameter change."""
    from gcfp.modules.i_expectations import cooling_off_active, request_parameter_change

    ordinary = request_parameter_change("lower X to 20%", "market looks cheap", config)
    sleeve = request_parameter_change(
        "raise growth sleeve to 25%", "good run", config, is_sleeve_cap_increase=True
    )
    assert (sleeve.eligible_on - sleeve.requested_on).days == 90
    assert (ordinary.eligible_on - ordinary.requested_on).days == 30
    assert cooling_off_active(sleeve)


def test_directive_9_every_gate_logs_its_computed_value(healthy_company, market, config):
    """No gate reports a bare PASS/FAIL."""
    ledger = AuditLedger(healthy_company.profile.symbol, date.today())
    assessment = a_health.run_module_a(healthy_company, market, config, ledger)

    for gate in assessment.gates:
        if gate.outcome is Outcome.NOT_COMPUTABLE:
            assert gate.reason, f"{gate.gate} is NOT_COMPUTABLE without a reason"
        else:
            assert gate.value is not None or gate.detail, (
                f"{gate.gate} reported {gate.outcome.value} with no computed value"
            )
        assert gate.describe()


def test_directive_9_a1_logs_which_branch_applied(market, config):
    """A1 explicitly requires logging which branch applied."""
    from tests.conftest import build_company

    on_ratio = a_health.gate_a1_solvency(build_company(), config)
    assert on_ratio.branch == "current_ratio"

    # Structurally negative working capital, positive operating cash flow.
    on_cash = a_health.gate_a1_solvency(
        build_company(
            quarterly_kw=dict(total_current_assets=8e9, total_current_liabilities=14e9)
        ),
        config,
    )
    assert on_cash.passed
    assert on_cash.branch == "operating_cash_flow"


def test_the_quantconnect_algorithm_refuses_to_trade_live():
    """quantconnect/main.py runs on a platform that can trade through a linked
    broker. It must refuse live mode, so it can only ever be a backtest."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "quantconnect" / "main.py").read_text()
    init = source[source.index("def initialize(self):"):]
    first_lines = init[: init.index("self.variant")]
    assert "if self.live_mode:" in first_lines and "raise" in first_lines
