"""Full-loop integration tests using a Claude-simulating FakeClaudeDriver.

These exercise acceptance criteria #5–#10: packet discovery -> fresh session ->
packet completion -> commit verification -> graphify gate -> handoff -> next
packet, plus packet evolution, resume, unsafe-dirty stop, and dashboard/handoff
artefacts — all without a live Claude process.
"""
from __future__ import annotations

import json

from autonomous_builder.claude.driver import FakeClaudeDriver
from autonomous_builder.claude.session import ClaudeSession
from autonomous_builder.models import RunStatus, StopReason
from autonomous_builder.repository.ledger import LedgerParser
from autonomous_builder.runner import Runner

from tests.conftest import GRAPHIFY_SUCCESS, build_profile, result_block


def make_factory(target, profile, adv_clock, sequence, *, commit=True,
                 graphify_output=GRAPHIFY_SUCCESS, plan_complete_on_last=True,
                 force_status="COMPLETE"):
    """Build a session_factory whose fake driver simulates each packet."""
    ledger = target.ledger_path

    def on_prompt(_text: str) -> str:
        current = LedgerParser().parse_file(ledger).next_authoritative_packet
        nxt = sequence.get(current, None)
        is_last = nxt is None
        if commit:
            head = target.simulate_packet(
                current, nxt, plan_complete=(is_last and plan_complete_on_last)
            )
            commit_hash = target.head()
            tree = "CLEAN"
        else:
            # simulate work WITHOUT committing -> dirty tree
            (target.root / "src.txt").write_text(f"uncommitted {current}\n", encoding="utf-8")
            commit_hash = "NONE"
            tree = "DIRTY"
        return result_block(
            current, commit_hash, nxt, tree=tree, status=force_status,
            plan_complete=("YES" if (is_last and plan_complete_on_last) else "NO"),
        )

    def factory(raw_log_path: str) -> ClaudeSession:
        driver = FakeClaudeDriver(
            initial_output="Welcome to Claude Code\n> ",
            responders=[
                ("REQUIRED RESULT BLOCK", on_prompt),
                (r"/graphify", graphify_output),
            ],
        )
        return ClaudeSession(driver, profile.claude, raw_log_path=raw_log_path,
                             clock=adv_clock, sleep=lambda s: None)

    return factory


def make_runner(target, profile, adv_clock, sequence, **kw):
    factory = make_factory(target, profile, adv_clock, sequence, **kw)
    return Runner(profile, session_factory=factory, clock=adv_clock, sleep=lambda s: None)


def test_full_run_to_plan_complete(target, tmp_path, adv_clock):
    profile = build_profile(target, tmp_path / "rt")
    runner = make_runner(target, profile, adv_clock, {"RT-D": "RT-E", "RT-E": None})
    state = runner.run()

    assert state.status == RunStatus.COMPLETE.value
    assert state.stop_reason == StopReason.PLAN_COMPLETE.value
    assert state.completed_packets == ["RT-D", "RT-E"]
    assert set(state.commits.keys()) == {"RT-D", "RT-E"}
    assert state.plan_complete
    # graphify gate recorded a success for the last packet
    assert state.last_graphify_update and state.last_graphify_update["success"]


def test_handoffs_and_dashboard_written(target, tmp_path, adv_clock):
    profile = build_profile(target, tmp_path / "rt")
    runner = make_runner(target, profile, adv_clock, {"RT-D": "RT-E", "RT-E": None})
    runner.run()
    store = runner.store
    # handoffs
    assert (store.handoffs_dir / "RT-D.json").exists()
    assert (store.handoffs_dir / "RT-D.md").exists()
    ho = json.loads((store.handoffs_dir / "RT-D.json").read_text())
    assert ho["completed_packet"] == "RT-D"
    assert ho["next_authoritative_packet"] == "RT-E"
    assert ho["graphify_update"] == "OK"
    # dashboard
    assert (store.dashboard_dir / "dashboard.md").exists()
    dj = json.loads((store.dashboard_dir / "dashboard.json").read_text())
    assert dj["run_status"] in ("COMPLETE", "RUNNING")
    assert "RT-D" in dj["completed_packets"]
    # logs + result json
    assert (store.logs_dir / "builder.log").exists()
    assert list(store.results_dir.glob("*.json"))
    assert list(store.sessions_dir.glob("*.log"))


def test_packet_evolution_no_hardcoded_sequence(target, tmp_path, adv_clock):
    # RT-D evolves to a repair packet, then to RT-E, then complete
    profile = build_profile(target, tmp_path / "rt")
    seq = {"RT-D": "GR1-REPAIR-A", "GR1-REPAIR-A": "RT-E", "RT-E": None}
    runner = make_runner(target, profile, adv_clock, seq)
    state = runner.run()
    assert state.completed_packets == ["RT-D", "GR1-REPAIR-A", "RT-E"]
    assert state.status == RunStatus.COMPLETE.value


def test_unsafe_dirty_tree_stops(target, tmp_path, adv_clock):
    profile = build_profile(target, tmp_path / "rt", max_retries=2)
    # commit=False -> the packet leaves the tree dirty and never commits
    runner = make_runner(target, profile, adv_clock, {"RT-D": "RT-E"}, commit=False)
    state = runner.run()
    assert state.status == RunStatus.STOPPED.value
    assert state.stop_reason == StopReason.DIRTY_TREE.value
    assert "RT-D" not in state.completed_packets
    # a recovery report was written
    assert list(runner.store.failures_dir.glob("*.md"))


def test_graphify_gate_failure_stops(target, tmp_path, adv_clock):
    profile = build_profile(target, tmp_path / "rt")
    runner = make_runner(
        target, profile, adv_clock, {"RT-D": "RT-E"},
        graphify_output="Traceback (most recent call last):\nERROR: Graph is empty\n",
    )
    state = runner.run()
    assert state.stop_reason == StopReason.STOP_AT_GRAPHIFY_GATE.value
    # RT-D was committed & verified, but the run halted at the graphify gate
    assert "RT-D" in state.completed_packets or state.current_packet == "RT-D"
    assert any("graphify_gate" in p.name for p in runner.store.failures_dir.glob("*.md"))


def test_graphify_gate_is_durable_across_resume(target, tmp_path, adv_clock):
    profile = build_profile(target, tmp_path / "rt")
    # first run: graphify fails -> STOP_AT_GRAPHIFY_GATE, pending recorded
    r1 = make_runner(target, profile, adv_clock, {"RT-D": "RT-E", "RT-E": None},
                     graphify_output="ERROR: Graph is empty\n")
    s1 = r1.run()
    assert s1.stop_reason == StopReason.STOP_AT_GRAPHIFY_GATE.value
    assert s1.graphify_pending is not None
    assert s1.graphify_pending["packet"] == "RT-D"

    # resume: graphify now succeeds -> pending resolved, run continues to complete
    r2 = make_runner(target, profile, adv_clock, {"RT-D": "RT-E", "RT-E": None})
    s2 = r2.run(resume=True)
    assert s2.graphify_pending is None
    assert "RT-E" in s2.completed_packets
    assert s2.status == RunStatus.COMPLETE.value


def test_preflight_ignores_graphify_out_dirty(target, tmp_path, adv_clock):
    from autonomous_builder.models import GitState, StopReason as SR
    profile = build_profile(target, tmp_path / "rt")
    runner = make_runner(target, profile, adv_clock, {})
    # tree dirty ONLY in graphify-out/ -> preflight must NOT stop
    gs = GitState(root=str(target.root), exists=True, is_repo=True, branch="main",
                  head="abc123", dirty=True, untracked_files=["graphify-out/graph.json"])
    assert runner._preflight(gs) is None
    # a real source change DOES stop
    gs2 = GitState(root=str(target.root), exists=True, is_repo=True, branch="main",
                   head="abc123", dirty=True, changed_files=["src/app.ts"])
    stop = runner._preflight(gs2)
    assert stop is not None and stop[0] == SR.DIRTY_TREE


def test_dirty_tree_preflight_stops_before_session(target, tmp_path, adv_clock):
    target.make_dirty()  # dirty BEFORE any packet
    profile = build_profile(target, tmp_path / "rt")
    runner = make_runner(target, profile, adv_clock, {"RT-D": "RT-E"})
    state = runner.run()
    assert state.stop_reason == StopReason.DIRTY_TREE.value
    assert state.completed_packets == []


def test_resume_from_repository_truth(target, tmp_path, adv_clock):
    profile = build_profile(target, tmp_path / "rt")
    # first run: only one packet
    r1 = make_runner(target, profile, adv_clock, {"RT-D": "RT-E", "RT-E": None})
    r1.max_packets = 1
    s1 = r1.run()
    assert s1.completed_packets == ["RT-D"]
    assert LedgerParser().parse_file(target.ledger_path).next_authoritative_packet == "RT-E"

    # resume: continues from repository truth (ledger now says RT-E)
    r2 = make_runner(target, profile, adv_clock, {"RT-D": "RT-E", "RT-E": None})
    s2 = r2.run(resume=True)
    assert "RT-E" in s2.completed_packets
    assert s2.status == RunStatus.COMPLETE.value


def test_blocked_status_stops_even_with_commit(target, tmp_path, adv_clock):
    # Claude commits + writes a report but reports BLOCKED -> must STOP, never be
    # rescued into "success" by repository truth.
    profile = build_profile(target, tmp_path / "rt", max_retries=0)
    runner = make_runner(target, profile, adv_clock, {"RT-D": "RT-E"}, force_status="BLOCKED")
    state = runner.run()
    assert state.stop_reason == StopReason.BLOCKED.value
    assert "RT-D" not in state.completed_packets


def test_ambiguous_ledger_stops(target, tmp_path, adv_clock):
    # write two equal-authority conflicting markers
    target.ledger_path.write_text(
        "# Ledger\nNEXT AUTHORITATIVE PACKET: RT-D — one\n"
        "NEXT AUTHORITATIVE PACKET: RT-E — two\n", encoding="utf-8"
    )
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=str(target.root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "ambiguous ledger"], cwd=str(target.root), check=True, capture_output=True)
    profile = build_profile(target, tmp_path / "rt")
    runner = make_runner(target, profile, adv_clock, {})
    state = runner.run()
    assert state.stop_reason == StopReason.AMBIGUOUS_DISCOVERY.value
    assert any("ambiguity" in p.name for p in runner.store.failures_dir.glob("*.md"))
