"""Session watchdog: idle timeout and hard (total) timeout.

The watchdog is clock-injectable so tests can drive it deterministically without
sleeping. ``note_activity`` is called by the session monitor loop whenever
*meaningful* output arrives (whitespace / pure ANSI spinner noise does not
count), and ``check`` is polled to decide whether to intervene.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Callable


class WatchdogVerdict(str, Enum):
    OK = "OK"
    IDLE_TIMEOUT = "IDLE_TIMEOUT"
    HARD_TIMEOUT = "HARD_TIMEOUT"


class Watchdog:
    def __init__(
        self,
        idle_timeout_seconds: float,
        hard_timeout_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        if idle_timeout_seconds <= 0 or hard_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        self.idle_timeout = idle_timeout_seconds
        self.hard_timeout = hard_timeout_seconds
        self._clock = clock
        self._start = clock()
        self._last_activity = self._start

    def note_activity(self) -> None:
        self._last_activity = self._clock()

    def reset(self) -> None:
        now = self._clock()
        self._start = now
        self._last_activity = now

    @property
    def elapsed(self) -> float:
        return self._clock() - self._start

    @property
    def idle_for(self) -> float:
        return self._clock() - self._last_activity

    def check(self) -> WatchdogVerdict:
        if self.elapsed >= self.hard_timeout:
            return WatchdogVerdict.HARD_TIMEOUT
        if self.idle_for >= self.idle_timeout:
            return WatchdogVerdict.IDLE_TIMEOUT
        return WatchdogVerdict.OK
