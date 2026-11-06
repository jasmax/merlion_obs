"""A clock the tracer reads from.

Real mode: wall time. Simulation mode: a virtual clock that tools advance by their
simulated latency, so a demo run produces realistic timestamps and durations
across weeks of traffic without actually sleeping.
"""
import datetime as dt

UTC = dt.timezone.utc


class Clock:
    def __init__(self):
        self._virtual = None

    def set(self, t: dt.datetime):
        self._virtual = t.astimezone(UTC)

    def now(self) -> dt.datetime:
        return self._virtual if self._virtual is not None else dt.datetime.now(UTC)

    def advance(self, ms: float):
        if self._virtual is not None:
            self._virtual += dt.timedelta(milliseconds=ms)

    def reset(self):
        self._virtual = None


CLOCK = Clock()
