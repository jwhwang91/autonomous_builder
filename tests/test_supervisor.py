from __future__ import annotations

import itertools

from autonomous_builder.models import RunState, RunStatus, StopReason
from autonomous_builder.supervisor import Supervisor

from tests.conftest import build_profile
from tests.test_runner_fake import make_runner


# ---------------------------------------------------------------------------
# scripted fake runner for the supervisor decision logic
# ---------------------------------------------------------------------------
class FakeStore:
    def __init__(self):
        self._stop = False
        self.logs = []
        self.state = None

    def stop_requested(self):
        return self._stop

    def clear_stop(self):
        self._stop = False

    def load_state(self):
        return self.state

    def log(self, m):
        self.logs.append(m)


class FakeRunner:
    def __init__(self, states):
        self.states = list(states)
        self.i = 0
        self.calls = []
        self.store = FakeStore()

    def run(self, resume=False):
        self.calls.append(resume)
        st = self.states[min(self.i, len(self.states) - 1)]
        self.i += 1
        return st


def _state(reason, plan_complete=False):
    s = RunState(run_id="r", project="p")
    s.stop_reason = reason.value if hasattr(reason, "value") else reason
    s.plan_complete = plan_complete
    s.status = RunStatus.COMPLETE.value if plan_complete else RunStatus.STOPPED.value
    return s


def _sup(runner, **kw):
    kw.setdefault("connectivity_check", lambda: True)
    kw.setdefault("tree_clean_check", lambda: True)
    kw.setdefault("clock", itertools.count(0, 0.01).__next__)
    kw.setdefault("sleep", lambda s: None)
    return Supervisor(runner, **kw)


# ---------------------------------------------------------------------------
def test_completes_immediately():
    r = FakeRunner([_state(StopReason.PLAN_COMPLETE, plan_complete=True)])
    res = _sup(r).run()
    assert res.reason == "completed" and res.auto_resumes == 0
    assert r.calls == [False]


def test_auto_resume_transient_then_complete():
    r = FakeRunner([_state(StopReason.MAX_RETRIES),
                    _state(StopReason.PLAN_COMPLETE, plan_complete=True)])
    res = _sup(r).run()
    assert res.reason == "completed" and res.auto_resumes == 1
    assert r.calls == [False, True]


def test_unsafe_dirty_tree_no_resume():
    r = FakeRunner([_state(StopReason.DIRTY_TREE)])
    res = _sup(r).run()
    assert res.reason == "unsafe_stop" and res.auto_resumes == 0
    assert r.calls == [False]


def test_blocked_no_resume():
    r = FakeRunner([_state(StopReason.BLOCKED)])
    assert _sup(r).run().reason == "unsafe_stop"


def test_graphify_gate_no_resume():
    r = FakeRunner([_state(StopReason.STOP_AT_GRAPHIFY_GATE)])
    assert _sup(r).run().reason == "unsafe_stop"


def test_ambiguous_no_resume():
    r = FakeRunner([_state(StopReason.AMBIGUOUS_DISCOVERY)])
    assert _sup(r).run().reason == "unsafe_stop"


def test_transient_but_dirty_tree_blocks_resume():
    r = FakeRunner([_state(StopReason.MAX_RETRIES)])
    res = _sup(r, tree_clean_check=lambda: False).run()
    assert res.reason == "unsafe_stop" and res.detail == "dirty_tree_on_resume"
    assert res.auto_resumes == 0


def test_cap_max_auto_resumes():
    r = FakeRunner([_state(StopReason.MAX_RETRIES)] * 10)
    res = _sup(r, max_auto_resumes=2).run()
    assert res.reason == "cap_resumes" and res.auto_resumes == 2
    assert r.calls == [False, True, True]


def test_connectivity_wait_then_resume():
    r = FakeRunner([_state(StopReason.MAX_RETRIES),
                    _state(StopReason.PLAN_COMPLETE, plan_complete=True)])
    # offline twice, then online
    conn = iter([False, False, True, True, True])
    slept = []
    res = _sup(r, connectivity_check=lambda: next(conn),
               sleep=lambda s: slept.append(s), reconnect_poll_seconds=5).run()
    assert res.reason == "completed"
    assert len(slept) >= 2  # waited while offline


def test_user_stop():
    r = FakeRunner([_state(StopReason.USER_STOP)])
    assert _sup(r).run().reason == "user_stop"


def test_paused_none_is_benign():
    r = FakeRunner([_state(StopReason.NONE)])
    assert _sup(r).run().reason == "paused"


def test_stop_requested_during_wait():
    r = FakeRunner([_state(StopReason.MAX_RETRIES),
                    _state(StopReason.PLAN_COMPLETE, plan_complete=True)])
    r.store._stop = True  # a stop was requested
    # offline so it enters the wait loop, where it should notice the stop request
    res = _sup(r, connectivity_check=lambda: False).run()
    assert res.reason == "user_stop"


class Clock:
    def __init__(self, seq):
        self.seq = list(seq)
        self.i = 0

    def __call__(self):
        v = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return v


def test_cap_max_hours():
    r = FakeRunner([_state(StopReason.MAX_RETRIES)] * 10)
    # start=0, first elapsed check 0h, after one resume the clock jumps past 12h
    res = _sup(r, clock=Clock([0, 0, 100000, 100000])).run()
    assert res.reason == "cap_hours"


# ---------------------------------------------------------------------------
# end-to-end through the real Runner + fake Claude driver (happy path)
# ---------------------------------------------------------------------------
def test_supervisor_over_real_runner_completes(target, tmp_path, adv_clock):
    profile = build_profile(target, tmp_path / "rt")
    runner = make_runner(target, profile, adv_clock, {"RT-D": "RT-E", "RT-E": None})
    sup = Supervisor(runner, connectivity_check=lambda: True,
                     clock=adv_clock, sleep=lambda s: None)
    res = sup.run()
    assert res.reason == "completed" and res.auto_resumes == 0
    assert res.state.completed_packets == ["RT-D", "RT-E"]
