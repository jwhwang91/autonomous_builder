The implementation plan is COMPLETE and the required runtime audit has PASSED.
This is the OPTIONAL finalization phase: generate one Immersive test deck using
the completed Runtime, to validate it end-to-end on real assets.

=== PROJECT CONTEXT ===
Project:            {{PROJECT_NAME}}
Repository root:    {{PROJECT_ROOT}}
Implementation plan:{{PLAN_PATH}}
Test asset directory: {{TEST_ASSET_DIR}}
Current git branch: {{CURRENT_BRANCH}}
Current git HEAD:   {{CURRENT_HEAD}}

=== WHAT TO DO ===
1.  Use the completed Runtime to generate ONE Immersive test deck from the assets
    in the test asset directory above.
2.  Prefer CLI-only generation. {{DEV_SERVER_RULE}}
3.  Record the exact output paths of every generated artifact (deck files,
    thumbnails, logs) and the test results.
4.  Do NOT modify runtime source unless a blocking generation bug requires it; if
    it does, note it clearly as PLAN_DRIFT.
5.  Do NOT push.

=== SAFETY RULES ===
{{SAFETY_RULES}}
{{PUSH_POLICY}}

=== REQUIRED RESULT BLOCK ===
AUTONOMOUS_BUILDER_RESULT
STATUS: COMPLETE|BLOCKED|FAILED
PACKET: FINAL-TEST-DECK
COMMIT: <full git commit hash if you committed anything, or NONE>
NEXT_AUTHORITATIVE_PACKET: NONE
TESTS: PASS|FAIL|PARTIAL
WORKING_TREE: CLEAN|DIRTY
PLAN_COMPLETE: YES
REPORT: <path to a generated-deck report, or NONE>
GRAPHIFY_UPDATE_REQUIRED: NO
BLOCKERS:
- <one per line, or omit if none>
PLAN_DRIFT:
- <divergence, or omit if none>
UNRESOLVED_RISKS:
- <risks, or omit if none>
GENERATED_ARTIFACTS:
- <path to each generated deck artifact>
END_AUTONOMOUS_BUILDER_RESULT
