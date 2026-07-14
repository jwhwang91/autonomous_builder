You are executing a REPAIR / recovery packet under external orchestration
(Autonomous Builder). A previous attempt on this packet did not cleanly verify.
Proceed carefully and conservatively.

=== PROJECT CONTEXT ===
Project:            {{PROJECT_NAME}}
Repository root:    {{PROJECT_ROOT}}
Implementation plan:{{PLAN_PATH}}
Execution ledger:   {{LEDGER_PATH}}
Reports directory:  {{REPORTS_DIR}}
Current git branch: {{CURRENT_BRANCH}}
Current git HEAD:   {{CURRENT_HEAD}}

=== WHY THIS IS A REPAIR ===
{{REPAIR_REASON}}

=== PREVIOUS SESSION HANDOFF ===
{{HANDOFF_BLOCK}}

=== WHAT TO DO ===
1.  Read the plan, the Execution Ledger, and the most recent relevant reports.
2.  Use Graphify for structural understanding before editing.
3.  Verify git state. If the working tree is dirty from a prior partial attempt,
    INSPECT the changes first — do not blindly discard them, and do not run any
    destructive git command. Report what you find.
4.  Determine the correct authoritative packet (it may now be a repair/split id).
5.  Complete the packet's blocking acceptance criteria.
6.  Run every packet-required test (baseline commands below plus any extras).
7.  Update the report(s) and ledger; commit only after blocking criteria pass;
    leave the working tree clean. Do NOT push. Do NOT run the next packet.
8.  Return the result block (identical format to a normal packet) as the LAST
    thing you output.

=== BASELINE TEST COMMANDS ===
{{TEST_COMMANDS}}

=== SAFETY RULES ===
{{SAFETY_RULES}}
{{PUSH_POLICY}}

=== REQUIRED RESULT BLOCK ===
AUTONOMOUS_BUILDER_RESULT
STATUS: COMPLETE|BLOCKED|FAILED
PACKET: <the packet id you executed>
COMMIT: <full git commit hash, or NONE>
NEXT_AUTHORITATIVE_PACKET: <the next authoritative packet id, or NONE>
TESTS: PASS|FAIL|PARTIAL
WORKING_TREE: CLEAN|DIRTY
PLAN_COMPLETE: YES|NO
REPORT: <path to the completion report, or NONE>
GRAPHIFY_UPDATE_REQUIRED: YES|NO
BLOCKERS:
- <one per line, or omit if none>
PLAN_DRIFT:
- <divergence from the plan, or omit if none>
UNRESOLVED_RISKS:
- <risks for the next session, or omit if none>
END_AUTONOMOUS_BUILDER_RESULT
