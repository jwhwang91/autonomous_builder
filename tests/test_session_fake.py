from __future__ import annotations

import re

from autonomous_builder.claude.driver import FakeClaudeDriver
from autonomous_builder.claude.parser import END_SENTINEL
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
    # command text is typed via send_text, then submitted with a carriage return
    assert "/effort ultracode" in d.sent_texts
    assert "\r" in d.sent_texts


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


# The echoed prompt template — invalid STATUS (pipe form) + placeholder PACKET.
ECHO_TEMPLATE = (
    "echoed prompt back:\n"
    "AUTONOMOUS_BUILDER_RESULT\n"
    "STATUS: COMPLETE|BLOCKED|FAILED\n"
    "PACKET: <the packet id you executed>\n"
    "TESTS: PASS|FAIL|PARTIAL\n"
    "END_AUTONOMOUS_BUILDER_RESULT\n"
)


def test_echo_template_block_ignored(adv_clock):
    # regression: the echoed prompt template contains the sentinels but is an
    # INVALID block — it must NOT complete the packet monitor.
    d = FakeClaudeDriver(initial_output=ECHO_TEMPLATE, eof_after_empty_reads=4)
    s = ClaudeSession(d, _cfg(), clock=adv_clock, sleep=lambda x: None)
    wd = Watchdog(600, 3600, clock=adv_clock)
    mr = s._monitor([re.escape(END_SENTINEL)], watchdog=wd, require_valid_result=True, label="packet")
    assert mr.reason == "eof"  # ignored the echo; the driver ran out instead


def test_real_result_block_completes_after_echo(adv_clock):
    # the echoed template is present first, then Claude emits a REAL block later —
    # only the real block completes the monitor.
    block = result_block("RT-F", "abc1234def0", "RT-G")
    d = FakeClaudeDriver(initial_output=ECHO_TEMPLATE)
    for _ in range(5):
        d.queue_output("…implementing RT-F…\n")
    d.queue_output("final:\n" + block)
    s = ClaudeSession(d, _cfg(), clock=adv_clock, sleep=lambda x: None)
    wd = Watchdog(600, 3600, clock=adv_clock)
    mr = s._monitor([re.escape(END_SENTINEL)], watchdog=wd, require_valid_result=True, label="packet")
    assert mr.reason == "sentinel"


def test_completes_via_result_file(adv_clock, tmp_path):
    # Claude wrote the result block to a FILE — the builder completes on that,
    # even though the TUI stream never carries a valid block.
    rf = tmp_path / "RT-G.block"
    rf.write_text(result_block("RT-G", "abc1234def0", "RT-H"), encoding="utf-8")
    d = FakeClaudeDriver(initial_output="…implementing…", eof_after_empty_reads=1000)
    s = ClaudeSession(d, _cfg(), clock=adv_clock, sleep=lambda x: None)
    wd = Watchdog(600, 3600, clock=adv_clock)
    mr = s._monitor([re.escape(END_SENTINEL)], watchdog=wd, require_valid_result=True,
                    result_file=str(rf), label="packet")
    assert mr.reason == "result_file"


def test_partial_result_file_ignored(adv_clock, tmp_path):
    # a half-written result file (no END sentinel) must NOT complete
    rf = tmp_path / "x.block"
    rf.write_text("AUTONOMOUS_BUILDER_RESULT\nSTATUS: COMPLETE\nPACKET: RT-G\n", encoding="utf-8")
    d = FakeClaudeDriver(initial_output="…", eof_after_empty_reads=3)
    s = ClaudeSession(d, _cfg(), clock=adv_clock, sleep=lambda x: None)
    wd = Watchdog(600, 3600, clock=adv_clock)
    mr = s._monitor([re.escape(END_SENTINEL)], watchdog=wd, require_valid_result=True,
                    result_file=str(rf), label="packet")
    assert mr.reason == "eof"  # incomplete file ignored


def test_terminate_closes(adv_clock):
    d = FakeClaudeDriver(initial_output="> ")
    s = _session(d, adv_clock)
    s.open(startup_timeout=5)
    s.terminate(force=True)
    assert d.terminated
