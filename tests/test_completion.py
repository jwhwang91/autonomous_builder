from __future__ import annotations

from autonomous_builder.execution.completion import PlanCompletion
from autonomous_builder.repository.ledger import LedgerParse
from autonomous_builder.repository.reports import ReportInfo


def test_next_packet_means_not_complete():
    v = PlanCompletion().evaluate(ledger=LedgerParse(), next_packet="RT-D")
    assert not v.complete
    assert "RT-D" in v.reason


def test_no_evidence_not_complete():
    v = PlanCompletion(require_audit_pass=False).evaluate(ledger=LedgerParse(), next_packet=None)
    assert not v.complete


def test_markers_plus_audit_complete():
    ledger = LedgerParse(plan_complete=True, plan_complete_evidence=["all done"])
    audit = ReportInfo(path="/a", name="a.md", mtime=1, kind="audit", verdict="PASS")
    v = PlanCompletion(require_audit_pass=True).evaluate(ledger=ledger, next_packet=None, audits=[audit])
    assert v.complete
    assert v.audit_passed is True


def test_markers_without_audit_not_complete():
    ledger = LedgerParse(plan_complete=True, plan_complete_evidence=["all done"])
    v = PlanCompletion(require_audit_pass=True).evaluate(ledger=ledger, next_packet=None, audits=[])
    assert not v.complete


def test_failing_audit_blocks_completion():
    ledger = LedgerParse(plan_complete=True, plan_complete_evidence=["all done"])
    audit = ReportInfo(path="/a", name="a.md", mtime=1, kind="audit", verdict="FAIL")
    v = PlanCompletion(require_audit_pass=True).evaluate(ledger=ledger, next_packet=None, audits=[audit])
    assert not v.complete
    assert v.audit_passed is False


def test_markers_complete_when_audit_not_required():
    ledger = LedgerParse(plan_complete=True, plan_complete_evidence=["all done"])
    v = PlanCompletion(require_audit_pass=False).evaluate(ledger=ledger, next_packet=None)
    assert v.complete


def test_passing_audit_alone_without_markers_not_complete():
    # regression: a passing per-packet acceptance report must NOT declare the
    # whole plan complete without explicit plan-completion markers.
    audit = ReportInfo(path="/a", name="ip3-acceptance.md", mtime=1, kind="acceptance", verdict="PASS")
    v = PlanCompletion(require_audit_pass=True).evaluate(
        ledger=LedgerParse(), plan=LedgerParse(), next_packet=None, audits=[audit]
    )
    assert not v.complete
    assert "marker" in v.reason
