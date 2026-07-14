"""Plan-completion detection.

The builder must distinguish PACKET COMPLETE from ENTIRE PLAN COMPLETE. One
packet reporting success is never sufficient to stop. Plan completion is
concluded only when repository truth agrees there is no remaining authoritative
packet AND (when configured) a required final audit/acceptance gate has passed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from autonomous_builder.repository.ledger import LedgerParse
from autonomous_builder.repository.reports import ReportInfo


@dataclass
class CompletionVerdict:
    complete: bool
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    audit_passed: Optional[bool] = None
    missing: list[str] = field(default_factory=list)


class PlanCompletion:
    def __init__(self, require_audit_pass: bool = True):
        self.require_audit_pass = require_audit_pass

    def evaluate(
        self,
        *,
        ledger: LedgerParse,
        plan: Optional[LedgerParse] = None,
        next_packet: Optional[str],
        audits: Optional[list[ReportInfo]] = None,
    ) -> CompletionVerdict:
        evidence: list[str] = []
        # A concrete next authoritative packet means we are NOT done, full stop.
        if next_packet:
            return CompletionVerdict(
                complete=False,
                reason=f"authoritative next packet remains: {next_packet}",
            )

        # No next packet anywhere: require explicit plan-completion MARKERS. A
        # passing per-packet audit/acceptance report is NOT, on its own, evidence
        # that the whole plan is done — "one packet reporting success is never
        # sufficient to stop". Markers are the precondition; the audit is an extra
        # gate layered on top of them.
        markers: list[str] = []
        if ledger.plan_complete:
            markers.extend(ledger.plan_complete_evidence or ["ledger indicates plan complete"])
        if plan is not None and plan.plan_complete:
            markers.extend(plan.plan_complete_evidence or ["plan indicates completion"])
        evidence.extend(markers)

        # audit / acceptance gate
        audit_passed: Optional[bool] = None
        missing: list[str] = []
        if self.require_audit_pass:
            audits = audits or []
            passing = [a for a in audits if a.verdict == "PASS"]
            failing = [a for a in audits if a.verdict == "FAIL"]
            if failing:
                audit_passed = False
                missing.append(f"audit/acceptance report failing: {failing[0].name}")
            elif passing:
                audit_passed = True
            else:
                audit_passed = None
                missing.append("no passing final audit/acceptance report found")

        # Decide.
        if not markers:
            return CompletionVerdict(
                complete=False,
                reason="no next packet found, but no explicit plan-completion markers "
                       "in the ledger/plan — do not assume completion off a passing "
                       "per-packet report",
                missing=["plan-completion markers"] + missing,
            )
        if self.require_audit_pass and audit_passed is not True:
            return CompletionVerdict(
                complete=False,
                reason="completion markers present but required audit gate not confirmed PASS",
                evidence=evidence,
                audit_passed=audit_passed,
                missing=missing,
            )
        if audit_passed is True:
            evidence.append("required audit/acceptance gate PASS")
        return CompletionVerdict(
            complete=True,
            reason="no remaining authoritative packet and completion criteria satisfied",
            evidence=evidence,
            audit_passed=audit_passed,
        )
