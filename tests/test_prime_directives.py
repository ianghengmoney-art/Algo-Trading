"""Prime Directive 1, re-scoped for the QuantConnect paper-trading deployment.

The original spec required the whole system to be incapable of placing an
order. Running on QC paper trading is a deliberate, operator-chosen departure:
the algorithm submits simulated orders with no human in the loop.

Rather than delete the guarantee, it is narrowed and made structural:

  * ``gcfp/`` remains a pure decision engine that cannot execute. Unchanged.
  * ``qc_algorithm/execution.py`` is the single permitted execution boundary,
    and these tests fail if an order call appears anywhere else -- including
    elsewhere inside ``qc_algorithm/`` itself.
  * ``gcfp/`` must never import ``qc_algorithm``, so the decision engine cannot
    reach execution even indirectly.

The value of the original directive was that trading behaviour was auditable at
a glance. That survives: one file, one function.
"""

from __future__ import annotations

import ast
import json
import pkgutil
from pathlib import Path

import gcfp

PACKAGE_ROOT = Path(gcfp.__file__).parent
REPO_ROOT = PACKAGE_ROOT.parent

# The one file allowed to move capital.
EXECUTION_BOUNDARY = REPO_ROOT / "qc_algorithm" / "execution.py"

# Anything that can reach a broker, an exchange, or an order router.
BANNED_IMPORTS = {
    "alpaca", "alpaca_trade_api", "ib_insync", "ibapi", "ccxt", "backtrader",
    "zipline", "vectorbt", "freqtrade", "robin_stocks", "tda", "tdameritrade",
    "schwab", "oandapyV20", "MetaTrader5", "krakenex", "binance", "coinbase",
    "interactivebrokers", "tradier", "polygon_trade",
    "webull", "fyers_api", "kiteconnect", "smartapi",
}

# QuantConnect is deliberately absent from BANNED_IMPORTS: it is this
# deployment's data source and execution venue, and banning the name outright
# would ban the read-only data adapter too. What must not happen is the
# decision engine reaching the QC *runtime* -- that is banned separately below,
# and the order API itself is governed structurally by the boundary tests.
QC_RUNTIME_IMPORTS = {"AlgorithmImports", "clr"}

BANNED_CALL_TOKENS = (
    "place_order", "submit_order", "create_order", "send_order", "buy_market",
    "sell_market", "execute_trade", "place_trade",
)


# Broker-API calls. QuantConnect's surface is included because that is the one
# this deployment can actually reach.
ORDER_CALLS = {
    "set_holdings", "SetHoldings",
    "liquidate", "Liquidate",
    "market_order", "MarketOrder",
    "limit_order", "LimitOrder",
    "stop_market_order", "StopMarketOrder",
    "market_on_close_order", "MarketOnCloseOrder",
    "order", "Order",
}


def _source_files():
    """Every source file in the decision engine."""
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _repo_source_files():
    """Every source file in the repository, tests excluded."""
    return sorted(
        p for p in REPO_ROOT.rglob("*.py")
        if "tests" not in p.parts and ".venv" not in p.parts and "build" not in p.parts
    )


def _called_attributes(path):
    """Names of every attribute/function actually invoked in a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name:
                out.add(name)
    return out


def test_no_trading_library_imported_anywhere():
    offenders = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # A relative import names a module inside this repository, not
                # a third-party package. Matching "from .quantconnect import X"
                # against the package list is a false positive.
                if node.level:
                    continue
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                if name in BANNED_IMPORTS:
                    offenders.append(f"{path.name}: {name}")
    assert not offenders, f"execution libraries imported: {offenders}"


def test_decision_engine_does_not_import_the_quantconnect_runtime():
    """The QC data adapter is duck-typed and must stay that way.

    If gcfp/ imported AlgorithmImports it would only run inside QC, and the
    engine would stop being testable and source-swappable -- which is the whole
    argument of the adapter layer.
    """
    offenders = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                if name in QC_RUNTIME_IMPORTS:
                    offenders.append(f"{path.name}: {name}")
    assert not offenders, f"decision engine imports the QC runtime: {offenders}"


def test_no_order_placing_call_names():
    offenders = []
    for path in _source_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and any(
                token in node.name for token in BANNED_CALL_TOKENS
            ):
                offenders.append(f"{path.name}:{node.name}")
            if isinstance(node, ast.Call):
                func = node.func
                attr = getattr(func, "attr", None) or getattr(func, "id", None)
                if attr and any(token in attr for token in BANNED_CALL_TOKENS):
                    offenders.append(f"{path.name}:{attr}")
    assert not offenders, f"order-placing calls present: {offenders}"


def test_banned_library_not_installed_in_environment():
    installed = {module.name for module in pkgutil.iter_modules()}
    present = installed & BANNED_IMPORTS
    assert not present, f"execution libraries in the dependency tree: {sorted(present)}"


# ---------------------------------------------------------------------------
# Execution boundary
# ---------------------------------------------------------------------------


def test_decision_engine_cannot_place_orders():
    """gcfp/ stays execution-free even though the deployment now trades."""
    offenders = []
    for path in _source_files():
        for name in _called_attributes(path) & ORDER_CALLS:
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{name}")
    assert not offenders, f"order calls inside the decision engine: {offenders}"


def test_execution_boundary_is_the_only_place_orders_are_placed():
    offenders = []
    for path in _repo_source_files():
        if path == EXECUTION_BOUNDARY:
            continue
        for name in _called_attributes(path) & ORDER_CALLS:
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{name}")
    assert not offenders, (
        "order calls outside qc_algorithm/execution.py -- the boundary is only a guarantee "
        f"while it is the sole location: {offenders}"
    )


def test_the_boundary_actually_contains_the_order_calls():
    """Guards against the previous test passing because nothing trades at all."""
    assert EXECUTION_BOUNDARY.exists(), "execution boundary file is missing"
    called = _called_attributes(EXECUTION_BOUNDARY)
    assert called & ORDER_CALLS, (
        "no order calls found in the execution boundary; either trading moved elsewhere "
        "or the ORDER_CALLS list has drifted from the broker API"
    )


def test_decision_engine_never_imports_the_execution_package():
    offenders = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(n.startswith("qc_algorithm") for n in names):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"decision engine imports the execution package: {offenders}"


def test_orders_are_off_until_explicitly_enabled():
    """A misconfigured deploy must not start trading on its own."""
    main = (REPO_ROOT / "qc_algorithm" / "main.py").read_text(encoding="utf-8")
    assert 'get_parameter("live_enabled")' in main, "live_enabled must be operator-set"
    assert '== "1"' in main, "live_enabled must default to off"

    config = json.loads((REPO_ROOT / "qc_algorithm" / "config.json").read_text(encoding="utf-8"))
    assert config["parameters"]["live_enabled"] == "0", "shipped config must default to dry run"


def test_dry_run_submits_nothing():
    from qc_algorithm.execution import ExecutionPlan, OrderIntent, apply_orders

    class ExplodingAlgorithm:
        def set_holdings(self, *a, **k):
            raise AssertionError("dry run must not submit orders")

        def liquidate(self, *a, **k):
            raise AssertionError("dry run must not liquidate")

    plan = ExecutionPlan(intents=[OrderIntent("AAA", "OPEN", 2.5, "test")])
    log = apply_orders(ExplodingAlgorithm(), plan, live_enabled=False)
    assert log and all("DRY RUN" in line for line in log)
