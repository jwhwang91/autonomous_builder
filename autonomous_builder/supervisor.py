"""Resilient auto-resume supervisor.

Wraps a :class:`~autonomous_builder.runner.Runner` for truly unattended runs that
survive network outages / laptop hiccups:

* On a **transient** stop (retries exhausted or a timeout — which the retry policy
  only reaches with a CLEAN target tree), it waits for API connectivity to return
  and then auto-``resume``s. This is what "self-heals when the internet comes
  back" means in practice.
* On any **unsafe** stop (dirty tree, blocked packet, failed tests, graphify gate,
  ambiguity, branch/origin mismatch, …) it stops and waits for a human — it never
  plows ahead over a real problem.
* It exits cleanly on plan completion, and is bounded by a max wall-clock and a
  max number of auto-resumes so it can never loop forever.

The supervisor is clock/sleep/connectivity injectable, so the whole loop is
unit-tested with a scripted fake runner and never touches the real network.
"""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from typing import Callable, Optional

from autonomous_builder.models import RunState, StopReason

# Stop reasons safe to auto-resume: the retry policy only reaches these with a
# clean working tree, and they are the shape a network outage takes (sessions
# fail to reach Claude -> retries exhaust -> MAX_RETRIES).
AUTO_RESUMABLE = {
    StopReason.MAX_RETRIES.value,
    StopReason.TIMEOUT.value,
}

_SUCCESS = {StopReason.PLAN_COMPLETE.value}


def default_connectivity_check(host: str = "api.anthropic.com", port: int = 443,
                               timeout: float = 5.0) -> bool:
    """True if the Anthropic API host is reachable (TCP connect, no API call)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass
class SupervisorResult:
    state: RunState
    auto_resumes: int
    reason: str          # completed | paused | unsafe_stop | user_stop | cap_hours | cap_resumes | error
    detail: str = ""


class Supervisor:
    def __init__(
        self,
        runner,
        *,
        start_resume: bool = False,
        max_hours: float = 12.0,
        max_auto_resumes: int = 50,
        reconnect_poll_seconds: float = 30.0,
        connectivity_check: Callable[[], bool] = default_connectivity_check,
        tree_clean_check: Optional[Callable[[], bool]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        logger: Optional[Callable[[str], None]] = None,
    ):
        self.runner = runner
        self.start_resume = start_resume
        self.max_hours = max_hours
        self.max_auto_resumes = max_auto_resumes
        self.reconnect_poll_seconds = reconnect_poll_seconds
        self.connectivity_check = connectivity_check
        self.tree_clean_check = tree_clean_check or self._default_tree_clean
        self._clock = clock
        self._sleep = sleep
        self.log = logger or (lambda m: None)

    # -- clean-tree gate (belt-and-suspenders before every auto-resume) -----
    def _default_tree_clean(self) -> bool:
        gs = self.runner.git.snapshot()
        if not gs.is_repo:
            return False
        return not self.runner._non_ignored_dirty(gs)

    # -- main loop ----------------------------------------------------------
    def run(self) -> SupervisorResult:
        start = self._clock()
        resumes = 0
        try:
            state = self.runner.run(resume=self.start_resume)
        except Exception as exc:  # pragma: no cover - defensive
            self.log(f"runner raised: {exc!r}; stopping for a human")
            st = self.runner.store.load_state() or RunState(run_id="error", project="unknown")
            return SupervisorResult(st, resumes, "error", str(exc))

        while True:
            reason = state.stop_reason
            if reason in _SUCCESS or state.plan_complete:
                self.log("plan complete — done")
                return SupervisorResult(state, resumes, "completed")
            if reason == StopReason.NONE.value:
                # benign pause (e.g. --max-packets) — not a failure
                self.log("run paused (no stop reason) — exiting")
                return SupervisorResult(state, resumes, "paused")
            if reason == StopReason.USER_STOP.value:
                self.log("user requested stop — exiting")
                return SupervisorResult(state, resumes, "user_stop")
            if reason not in AUTO_RESUMABLE:
                self.log(f"unsafe/terminal stop ({reason}) — waiting for a human")
                return SupervisorResult(state, resumes, "unsafe_stop", reason)

            # transient stop: only auto-resume when it is SAFE to do so
            if not self.tree_clean_check():
                self.log("transient stop but the target tree is dirty — not auto-resuming")
                return SupervisorResult(state, resumes, "unsafe_stop", "dirty_tree_on_resume")
            if resumes >= self.max_auto_resumes:
                self.log(f"reached max auto-resumes ({self.max_auto_resumes})")
                return SupervisorResult(state, resumes, "cap_resumes")
            if self._hours_elapsed(start) >= self.max_hours:
                self.log(f"reached max wall-clock ({self.max_hours}h)")
                return SupervisorResult(state, resumes, "cap_hours")

            # wait for the network to come back, then resume
            wait_status = self._wait_for_connectivity(start)
            if wait_status == "stopped":
                self.runner.store.clear_stop()
                return SupervisorResult(state, resumes, "user_stop")
            if wait_status == "timeout":
                return SupervisorResult(state, resumes, "cap_hours", "connectivity not restored in budget")

            resumes += 1
            self.log(f"auto-resume #{resumes} (previous stop: {reason})")
            try:
                state = self.runner.run(resume=True)
            except Exception as exc:  # pragma: no cover - defensive
                self.log(f"runner raised on resume: {exc!r}")
                return SupervisorResult(state, resumes, "error", str(exc))

    # -- helpers ------------------------------------------------------------
    def _hours_elapsed(self, start: float) -> float:
        return (self._clock() - start) / 3600.0

    def _wait_for_connectivity(self, start: float) -> str:
        """Block until connectivity returns. Returns 'ok' | 'stopped' | 'timeout'."""
        waited = False
        while not self.connectivity_check():
            waited = True
            if self.runner.store.stop_requested():
                return "stopped"
            if self._hours_elapsed(start) >= self.max_hours:
                self.log("connectivity not restored within max hours")
                return "timeout"
            self.log("offline — waiting for connectivity to return...")
            self._sleep(self.reconnect_poll_seconds)
        if waited:
            self.log("connectivity restored")
        return "ok"
