from __future__ import annotations

from autonomous_builder.execution.discovery import PacketDiscovery
from autonomous_builder.models import ClaudeResult, DiscoveryOutcome, PacketStatus
from autonomous_builder.repository.ledger import LedgerParse
from autonomous_builder.repository.reports import ReportInfo


def disc(**kw):
    return PacketDiscovery(require_audit_pass=kw.pop("require_audit", True))


def test_ledger_wins_over_claude_output():
    ledger = LedgerParse(next_authoritative_packet="RT-D", next_source="ledger:explicit_colon@line1")
    prev = ClaudeResult(found_block=True, status=PacketStatus.COMPLETE, packet="RT-C",
                        next_authoritative_packet="RT-E")
    r = disc().discover(ledger=ledger, previous_result=prev)
    assert r.outcome == DiscoveryOutcome.NEXT_PACKET
    assert r.next_packet == "RT-D"
    assert any("previous Claude session proposed next=RT-E" in d for d in r.disagreements)


def test_report_repair_beats_stale_plan():
    # ledger silent -> reports (level 3) beat plan (level 4)
    ledger = LedgerParse()
    plan = LedgerParse(next_authoritative_packet="RT-E", next_source="plan@line5")
    r = disc().discover(ledger=ledger, plan=plan, report_hints=[("GR1-REPAIR-A", "repair.md")])
    assert r.next_packet == "GR1-REPAIR-A"
    assert r.authority_source and "reports" in r.authority_source


def test_plan_falls_back_when_reports_silent():
    ledger = LedgerParse()
    plan = LedgerParse(next_authoritative_packet="RT-J", next_source="plan@line9")
    r = disc().discover(ledger=ledger, plan=plan)
    assert r.next_packet == "RT-J"


def test_plan_complete():
    ledger = LedgerParse(plan_complete=True, plan_complete_evidence=["ledger: all packets complete"])
    audit = ReportInfo(path="/a.md", name="a.md", mtime=1.0, kind="audit", verdict="PASS")
    r = disc().discover(ledger=ledger, audits=[audit])
    assert r.outcome == DiscoveryOutcome.PLAN_COMPLETE


def test_plan_complete_requires_audit_when_configured():
    ledger = LedgerParse(plan_complete=True, plan_complete_evidence=["all done"])
    r = disc(require_audit=True).discover(ledger=ledger, audits=[])
    assert r.outcome == DiscoveryOutcome.NO_DATA  # conservative: no audit -> not complete


def test_ambiguity_stop():
    ledger = LedgerParse(ambiguous=True, ambiguity_reason="RT-D vs RT-E equal markers")
    r = disc().discover(ledger=ledger)
    assert r.outcome == DiscoveryOutcome.AMBIGUOUS
    assert "RT-D" in r.ambiguity_reason


def test_refuses_superseded_next():
    ledger = LedgerParse(next_authoritative_packet="IP5", next_source="ledger",
                         superseded={"IP5": "RT-A"})
    r = disc().discover(ledger=ledger)
    assert r.outcome == DiscoveryOutcome.AMBIGUOUS
    assert "SUPERSEDED" in r.ambiguity_reason


def test_conflicting_report_hints_when_ledger_silent_stops():
    # regression: ledger silent + reports disagree -> ambiguous, do not guess
    r = disc().discover(ledger=LedgerParse(),
                        report_hints=[("RT-D", "a.md"), ("RT-E", "b.md")])
    assert r.outcome == DiscoveryOutcome.AMBIGUOUS
    assert "disagree" in (r.ambiguity_reason or "")


def test_flags_already_complete_next():
    ledger = LedgerParse(next_authoritative_packet="RT-D", next_source="ledger",
                         completed_packets=["RT-D"])
    r = disc().discover(ledger=ledger, report_completions=["RT-D"])
    assert r.outcome == DiscoveryOutcome.NEXT_PACKET
    assert any("already" in d.lower() and "RT-D" in d for d in r.disagreements)
