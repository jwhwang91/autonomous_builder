from __future__ import annotations

import pytest

from autonomous_builder.execution.watchdog import Watchdog, WatchdogVerdict


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_ok_when_active():
    c = Clock()
    wd = Watchdog(idle_timeout_seconds=10, hard_timeout_seconds=100, clock=c)
    c.t = 5
    wd.note_activity()
    c.t = 9
    assert wd.check() == WatchdogVerdict.OK


def test_idle_timeout():
    c = Clock()
    wd = Watchdog(idle_timeout_seconds=10, hard_timeout_seconds=1000, clock=c)
    c.t = 11
    assert wd.check() == WatchdogVerdict.IDLE_TIMEOUT


def test_activity_resets_idle():
    c = Clock()
    wd = Watchdog(idle_timeout_seconds=10, hard_timeout_seconds=1000, clock=c)
    c.t = 9
    wd.note_activity()
    c.t = 18  # 9 since activity
    assert wd.check() == WatchdogVerdict.OK
    c.t = 20  # 11 since activity
    assert wd.check() == WatchdogVerdict.IDLE_TIMEOUT


def test_hard_timeout_dominates():
    c = Clock()
    wd = Watchdog(idle_timeout_seconds=10, hard_timeout_seconds=50, clock=c)
    c.t = 5
    wd.note_activity()
    c.t = 51
    assert wd.check() == WatchdogVerdict.HARD_TIMEOUT


def test_invalid_timeouts():
    with pytest.raises(ValueError):
        Watchdog(0, 10)
