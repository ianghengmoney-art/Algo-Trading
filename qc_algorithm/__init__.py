"""QuantConnect execution boundary.

This package is the **only** place in the repository where order-placing calls
are permitted. `gcfp/` remains a pure decision engine that cannot execute, and
`tests/test_prime_directives.py` enforces both halves of that split: no order
capability inside `gcfp/`, and no order calls anywhere outside
`qc_algorithm/execution.py`.

The original GCFP v3 spec was alert-only by design. Running on QuantConnect
paper trading is a deliberate, operator-chosen departure from that: the
algorithm places simulated orders without a human in the loop. Keeping the
boundary explicit is what makes the departure auditable rather than diffuse --
one file to read to know everything this system can do to a portfolio.

No real capital is at risk in paper mode. Section 12 still requires two to
three months of paper trading before that conversation changes.
"""

__all__ = ["execution", "state"]
