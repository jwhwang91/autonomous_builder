from __future__ import annotations

from autonomous_builder.claude.driver import FakeClaudeDriver
from autonomous_builder.claude.session import ClaudeSession
from autonomous_builder.config import BootstrapStep, ClaudeConfig
from autonomous_builder.execution.watchdog import Watchdog

from tests.conftest import GRAPHIFY_SUCCESS, result_block


def _cfg():
    return ClaudeConfig(poll_interval_seconds=0.001, ready_patterns=[r">\s*$", "READY"])


def _session(driver, adv_clock):
    return ClaudeSession(driver, _cfg(), clock=adv_clock, sleep=lambda s: None)


def test_open_detects_ready(adv_clock):
    d = FakeClaudeDriver(initial_output="Welcome to Claude Code\n> ")
    s = _session(d, adv_clock)
    assert s.open(startup_timeout=5)
    assert d.started


def test_open_fails_if_process_dies(adv_clock):
    d = FakeClaudeDriver(initial_output="", start_alive=True, eof_after_empty_reads=2)
    s = _session(d, adv_clock)
    d.set_alive(False)
    assert not s.open(startup_timeout=5)


def test_bootstrap_sends_steps(adv_clock):
    d = FakeClaudeDriver(initial_output="> ")
    s = _session(d, adv_clock)
    s.open(startup_timeout=5)
    report = s.bootstrap([BootstrapStep(send="/effort ultracode", settle_seconds=0.0)])
    assert report.ok
    assert "/effort ultracode" in d.sent_lines


def test_packet_monitor_reaches_sentinel(adv_clock):
    block = result_block("RT-D", "abc1234def0", "RT-E")
    d = FakeClaudeDriver(initial_output="> ", responders=[("REQUIRED|EXECUTE", block)])
    s = _session(d, adv_clock)
    s.open(startup_timeout=5)
    wd = Watchdog(600, 3600, clock=adv_clock)
    mr = s.send_packet_prompt("please EXECUTE and emit the REQUIRED block", wd)
    assert mr.reason == "sentinel"
    assert "AUTONOMOUS_BUILDER_RESULT" in s.transcript


def test_graphify_success_detected(adv_clock):
    d = FakeClaudeDriver(initial_output="> ", responders=[("/graphify", GRAPHIFY_SUCCESS)])
    s = _session(d, adv_clock)
    s.open(startup_timeout=5)
    wd = Watchdog(600, 3600, clock=adv_clock)
    mr = s.run_graphify("/graphify . --update", [r"Graph complete"], [r"ERROR: Graph is empty"], wd)
    assert mr.reason == "matched"


def test_graphify_failure_detected(adv_clock):
    d = FakeClaudeDriver(initial_output="> ",
                         responders=[("/graphify", "Traceback...\nERROR: Graph is empty\n")])
    s = _session(d, adv_clock)
    s.open(startup_timeout=5)
    wd = Watchdog(600, 3600, clock=adv_clock)
    mr = s.run_graphify("/graphify . --update", [r"Graph complete"], [r"ERROR: Graph is empty"], wd)
    assert mr.reason == "failure"


def test_idle_timeout(adv_clock):
    # driver stays alive but never emits meaningful output -> idle timeout
    d = FakeClaudeDriver(initial_output="> ")
    s = _session(d, adv_clock)
    s.open(startup_timeout=5)
    wd = Watchdog(idle_timeout_seconds=0.05, hard_timeout_seconds=100, clock=adv_clock)
    mr = s.send_packet_prompt("do work", wd)
    assert mr.reason == "idle_timeout"


def test_terminate_closes(adv_clock):
    d = FakeClaudeDriver(initial_output="> ")
    s = _session(d, adv_clock)
    s.open(startup_timeout=5)
    s.terminate(force=True)
    assert d.terminated
