"""A hard stop for long phases, so a job always gets to write its report.

Deadlines checked between months were not enough: one month could stall
for hours inside a download — a feed throttling, a cache going stale at
midnight — and the job was killed by its runner with nothing reported,
three runs in a row. A timer interrupts whatever is running instead, and
the caller turns the interruption into a stated, partial result.
"""

from __future__ import annotations

import signal
import time
from contextlib import contextmanager
from typing import Iterator


#: Interval at which the stop is re-raised once the deadline has passed.
REPEAT_SECONDS = 5.0


class TimeBudgetExceeded(BaseException):
    """Raised inside the running phase when its time is up.

    A ``BaseException``, like ``KeyboardInterrupt``: the data layer retries
    on ``except Exception``, and when this was an ``Exception`` a retry loop
    caught it, the one-shot timer was spent, and the phase ran on until the
    runner killed the job — exactly what the timer exists to prevent.
    """


@contextmanager
def time_limit(deadline: float | None) -> Iterator[None]:
    """Raise :class:`TimeBudgetExceeded` in the block at ``deadline``.

    ``deadline`` is a ``time.monotonic()`` value; ``None`` means no limit.
    Uses SIGALRM, so it applies on the main thread of a POSIX process —
    the CLI on a GitHub runner — and is a no-op elsewhere.
    """
    if deadline is None or not hasattr(signal, "SIGALRM"):
        yield
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeBudgetExceeded()

    def _expire(signum, frame):  # pragma: no cover - timing dependent
        raise TimeBudgetExceeded()

    previous = signal.signal(signal.SIGALRM, _expire)
    # Keeps firing every few seconds past the deadline, so even a bare
    # ``except:`` somewhere below cannot absorb the stop for good.
    signal.setitimer(signal.ITIMER_REAL, remaining, REPEAT_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


__all__ = ["TimeBudgetExceeded", "time_limit"]
