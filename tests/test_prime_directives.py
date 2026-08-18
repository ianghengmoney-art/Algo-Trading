"""Prime Directive 1: the system must be incapable of placing an order."""

from __future__ import annotations

import ast
import pkgutil
from pathlib import Path

import gcfp

PACKAGE_ROOT = Path(gcfp.__file__).parent

# Anything that can reach a broker, an exchange, or an order router.
BANNED_IMPORTS = {
    "alpaca", "alpaca_trade_api", "ib_insync", "ibapi", "ccxt", "backtrader",
    "zipline", "vectorbt", "freqtrade", "robin_stocks", "tda", "tdameritrade",
    "schwab", "oandapyV20", "MetaTrader5", "krakenex", "binance", "coinbase",
    "interactivebrokers", "quantconnect", "lean", "tradier", "polygon_trade",
    "webull", "fyers_api", "kiteconnect", "smartapi",
}

BANNED_CALL_TOKENS = (
    "place_order", "submit_order", "create_order", "send_order", "buy_market",
    "sell_market", "execute_trade", "place_trade",
)


def _source_files():
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def test_no_trading_library_imported_anywhere():
    offenders = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                if name in BANNED_IMPORTS:
                    offenders.append(f"{path.name}: {name}")
    assert not offenders, f"execution libraries imported: {offenders}"


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
