from __future__ import annotations

from autonomous_builder.models import Handoff, PacketHistoryEntry, RunState
from autonomous_builder.state.store import StateStore


def test_save_and_load_roundtrip(profile):
    store = StateStore(profile)
    state = RunState(run_id="run-x", project="test-project")
    state.completed_packets = ["RT-A", "RT-B"]
    state.commits = {"RT-A": "abc123", "RT-B": "def456"}
    state.next_packet = "RT-C"
    state.packet_history.append(PacketHistoryEntry(packet="RT-B", attempt=1, status="COMPLETE", commit="def456"))
    store.save_state(state)

    loaded = store.load_state()
    assert loaded is not None
    assert loaded.run_id == "run-x"
    assert loaded.completed_packets == ["RT-A", "RT-B"]
    assert loaded.commits["RT-A"] == "abc123"
    assert loaded.next_packet == "RT-C"
    assert loaded.packet_history[0].packet == "RT-B"


def test_load_missing_returns_none(profile):
    assert StateStore(profile).load_state() is None


def test_reconcile_prefers_repo_truth(profile):
    store = StateStore(profile)
    state = RunState(run_id="r", project="p", completed_packets=["RT-A"], next_packet="RT-STALE")
    notes = store.reconcile(
        state, completed_from_truth=["RT-A", "RT-B", "RT-C"],
        git_head="deadbeef", git_branch="main", discovered_next="RT-D",
    )
    assert "RT-B" in state.completed_packets and "RT-C" in state.completed_packets
    assert state.next_packet == "RT-D"  # repo truth wins over stale stored value
    assert any("differs from repository truth" in n for n in notes)


def test_handoff_written_json_and_md(profile):
    store = StateStore(profile)
    h = Handoff(completed_packet="RT-D", status="COMPLETE", commit="abc1234",
                next_authoritative_packet="RT-E", tests={"suite": "PASS"},
                report="docs/reports/x.md", working_tree="CLEAN", graphify_update="OK")
    json_path, md_path = store.write_handoff(h)
    assert json_path.exists() and md_path.exists()
    md = md_path.read_text(encoding="utf-8")
    assert "RT-D" in md and "RT-E" in md and "COMPLETE" in md


def test_stop_request_lifecycle(profile):
    store = StateStore(profile)
    assert not store.stop_requested()
    store.request_stop("please stop")
    assert store.stop_requested()
    store.clear_stop()
    assert not store.stop_requested()


def test_latest_handoff(profile):
    store = StateStore(profile)
    store.write_handoff(Handoff(completed_packet="RT-D", next_authoritative_packet="RT-E"))
    latest = store.latest_handoff()
    assert latest is not None and latest.completed_packet == "RT-D"
