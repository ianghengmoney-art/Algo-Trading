"""§13 validation protocol — the backtest.

This package simulates what the operator *would have been told* at each point
in history, and what would have happened had they acted on it.  It is not an
execution path: nothing here connects to a broker, and the simulated fills
exist only to attribute outcomes to signals.  Prime Directive 1 is unaffected,
and ``tests/test_prime_directives.py`` still parses this package for trading
libraries like any other.

The design constraint that shapes everything here is §13.8: **point-in-time
data is mandatory**.  Every evaluation runs with the adapter's ``as_of`` set to
the rebalance date, so a 2015 decision sees only what had been filed by 2015 —
restatements included or excluded on their real dates.  Getting this wrong does
not produce an error; it produces a better-looking result, which is why it is
enforced structurally rather than left to discipline.
"""

from .portfolio import BacktestBook, BacktestPosition, ClosedPosition, SimulatedFill

__all__ = ["BacktestBook", "BacktestPosition", "ClosedPosition", "SimulatedFill"]
