"""Authoritative packet discovery.

Authority hierarchy (highest first):

    1. Current target repository state (git)
    2. Execution Ledger
    3. Latest completion / audit / repair reports
    4. Long implementation plan
    5. Previous Claude session result   (secondary evidence only)

Repository truth is authoritative; Claude's textual claims never override it.
The discovery never hardcodes a packet sequence, supports evolved/repair/split
ids, and — critically — refuses to guess: on ambiguity it stops with a clear
diagnostic so the caller can emit an ambiguity report.
"""
from __future__ import annotations

from typing import Optional

from autonomous_builder.models import (
    ClaudeResult,
    DiscoveryOutcome,
    DiscoveryResult,
    GitState,
    normalize_packet_id,
)
from autonomous_builder.repository.ledger import LedgerParse
from autonomous_builder.execution.completion import CompletionVerdict, PlanCompletion


class PacketDiscovery:
    def __init__(self, require_audit_pass: bool = True):
        self.completion = PlanCompletion(require_audit_pass=require_audit_pass)

    def discover(
        self,
        *,
        ledger: LedgerParse,
        plan: Optional[LedgerParse] = None,
        report_hints: Optional[list[tuple[str, str]]] = None,   # (packet, report_path), latest-first
        report_completions: Optional[list[str]] = None,          # packets with a completion report
        audits: Optional[list] = None,                           # ReportInfo audits/acceptance
        previous_result: Optional[ClaudeResult] = None,
        git_state: Optional[GitState] = None,
        git_recent_packets: Optional[list[str]] = None,
    ) -> DiscoveryResult:
        report_hints = report_hints or []
        report_completions = [normalize_packet_id(p) for p in (report_completions or [])]
        git_recent_packets = [normalize_packet_id(p) for p in (git_recent_packets or [])]

        # Completed set (union of all evidence).
        completed = self._merge_unique(
            ledger.completed_packets, report_completions, git_recent_packets
        )
        superseded = list(ledger.superseded.keys())

        # --- ambiguity from the ledger itself is decisive -----------------
        if ledger.ambiguous:
            return DiscoveryResult(
                outcome=DiscoveryOutcome.AMBIGUOUS,
                completed_packets=completed,
                superseded_packets=superseded,
                ambiguity_reason=ledger.ambiguity_reason,
                evidence=[f"ledger ambiguous: {ledger.ambiguity_reason}"],
            )

        # --- pick the next packet down the authority chain ----------------
        chosen: Optional[str] = None
        source: Optional[str] = None
        evidence: list[str] = []
        disagreements: list[str] = []

        if ledger.next_authoritative_packet:
            chosen = ledger.next_authoritative_packet
            source = ledger.next_source or "ledger"
            evidence.append(f"ledger next packet: {chosen} ({source})")
        else:
            # ledger silent -> reports (level 3) beat plan (level 4). The ledger's
            # equal-authority ambiguity check does NOT protect this level (the
            # ledger is silent precisely here), so if the reports themselves
            # disagree we must stop rather than guess by file order.
            distinct = {normalize_packet_id(p) for p, _ in report_hints if p}
            if len(distinct) > 1:
                return DiscoveryResult(
                    outcome=DiscoveryOutcome.AMBIGUOUS,
                    completed_packets=completed,
                    superseded_packets=superseded,
                    ambiguity_reason=(
                        "ledger is silent and the recent reports disagree on the next "
                        "packet: " + ", ".join(sorted(distinct))
                    ),
                    evidence=["ledger silent; conflicting report next-hints"],
                )
            report_pick = self._first_hint(report_hints)
            if report_pick:
                chosen, path = report_pick
                source = f"reports:{path}"
                evidence.append(f"reports next hint: {chosen} ({path})")
            elif plan is not None and plan.ambiguous:
                return DiscoveryResult(
                    outcome=DiscoveryOutcome.AMBIGUOUS,
                    completed_packets=completed,
                    superseded_packets=superseded,
                    ambiguity_reason=f"plan ambiguous: {plan.ambiguity_reason}",
                    evidence=["ledger silent; plan ambiguous"],
                )
            elif plan is not None and plan.next_authoritative_packet:
                chosen = plan.next_authoritative_packet
                source = plan.next_source or "plan"
                evidence.append(f"plan next packet: {chosen} ({source})")

        # --- record cross-source disagreements (authority already applied) -
        self._collect_disagreements(
            chosen, ledger, report_hints, plan, previous_result, report_completions, disagreements
        )

        # --- no next packet anywhere -> maybe plan complete ---------------
        if not chosen:
            verdict = self.completion.evaluate(
                ledger=ledger, plan=plan, next_packet=None, audits=audits
            )
            if verdict.complete:
                return DiscoveryResult(
                    outcome=DiscoveryOutcome.PLAN_COMPLETE,
                    completed_packets=completed,
                    superseded_packets=superseded,
                    plan_complete_evidence=verdict.evidence,
                    evidence=evidence + [verdict.reason],
                )
            # nothing to do and not clearly complete -> stop, do not guess
            return DiscoveryResult(
                outcome=DiscoveryOutcome.NO_DATA,
                completed_packets=completed,
                superseded_packets=superseded,
                ambiguity_reason=verdict.reason,
                evidence=evidence + verdict.missing,
            )

        # --- guard: the chosen packet must not be superseded --------------
        if normalize_packet_id(chosen) in {normalize_packet_id(s) for s in superseded}:
            by = ledger.superseded.get(normalize_packet_id(chosen), "?")
            return DiscoveryResult(
                outcome=DiscoveryOutcome.AMBIGUOUS,
                next_packet=chosen,
                completed_packets=completed,
                superseded_packets=superseded,
                ambiguity_reason=(
                    f"the discovered next packet {chosen} is marked SUPERSEDED "
                    f"(by {by}); refusing to run a superseded packet"
                ),
                evidence=evidence,
                disagreements=disagreements,
            )

        # --- observation: chosen packet already appears complete ----------
        if normalize_packet_id(chosen) in {normalize_packet_id(c) for c in completed}:
            disagreements.append(
                f"the discovered next packet {chosen} also appears COMPLETE in "
                "repository evidence; the next pointer may be stale — Claude must "
                "re-verify before executing"
            )

        return DiscoveryResult(
            outcome=DiscoveryOutcome.NEXT_PACKET,
            next_packet=normalize_packet_id(chosen),
            authority_source=source,
            completed_packets=completed,
            superseded_packets=superseded,
            evidence=evidence,
            disagreements=disagreements,
        )

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _merge_unique(*lists) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for lst in lists:
            for item in lst:
                n = normalize_packet_id(item)
                if n and n not in seen:
                    seen.add(n)
                    out.append(n)
        return out

    @staticmethod
    def _first_hint(hints: list[tuple[str, str]]) -> Optional[tuple[str, str]]:
        """Return the strongest (latest-first) report hint, or None.

        If the two most recent hints disagree we still take the first (latest);
        the disagreement is recorded separately. Genuine ambiguity from equal
        sources is caught at the ledger level (the higher authority).
        """
        for pid, path in hints:
            if pid:
                return (normalize_packet_id(pid), path)
        return None

    @staticmethod
    def _collect_disagreements(
        chosen: Optional[str],
        ledger: LedgerParse,
        report_hints: list[tuple[str, str]],
        plan: Optional[LedgerParse],
        previous_result: Optional[ClaudeResult],
        report_completions: list[str],
        out: list[str],
    ) -> None:
        if not chosen:
            return
        cn = normalize_packet_id(chosen)
        # reports disagree
        for pid, path in report_hints:
            if pid and normalize_packet_id(pid) != cn:
                out.append(f"reports suggest next={normalize_packet_id(pid)} ({path}) "
                           f"but authority selects {cn}")
                break
        # plan disagrees
        if plan is not None and plan.next_authoritative_packet:
            pn = normalize_packet_id(plan.next_authoritative_packet)
            if pn != cn and ledger.next_authoritative_packet:
                out.append(f"plan suggests next={pn} but ledger authority selects {cn}")
        # previous Claude result disagrees (secondary; ledger wins)
        if previous_result and previous_result.next_authoritative_packet:
            rn = normalize_packet_id(previous_result.next_authoritative_packet)
            if rn != cn:
                out.append(f"previous Claude session proposed next={rn} but authority "
                           f"selects {cn} (Claude output is secondary)")
        # a completion report already exists for the chosen packet
        if cn in {normalize_packet_id(c) for c in report_completions}:
            out.append(f"a completion report already exists for {cn} — it may already "
                       "be done; Claude must re-verify before executing")
