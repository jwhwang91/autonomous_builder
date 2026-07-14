# DeckFlip Runtime — first-session orientation

You are implementing the DeckFlip **Performance Runtime**, driven one packet at a
time by an external orchestrator. This orientation is injected only into the
first session of a run.

Authoritative sources (in priority order — repository truth wins over any prose,
including this note and any handoff):

1. The live target git repository state (branch, HEAD, working tree, history).
2. The Execution Ledger:
   `docs/reports/DECKFLIP_2_EXECUTION_LEDGER.md`
   — read its **NEXT AUTHORITATIVE PACKET** marker and the latest dated UPDATE.
3. The most recent completion / audit / repair reports in `docs/reports/`.
4. The Runtime Master Plan: `docs/RUNTIME_MASTER_PLAN.md`
   — it supersedes IP5 with packets RT-A…RT-J (RT-A executed as R0+R1). Packets
   evolve (e.g. RT-D → RT-D1 → GR1-REPAIR-A → RT-E); do not assume the sequence
   is fixed, and never run a superseded packet.

Ground rules for this and every session:

- Use Graphify (`graphify-out/`) for structural understanding **before** editing.
- Execute **exactly one** authoritative packet, then stop.
- Run every packet-required test — the baseline is `npm run typecheck`,
  `npm run test`, `npm run build`, `npm run build:editor`, plus any commands the
  packet itself requires.
- Update the required completion report(s) and the Execution Ledger (including the
  NEXT AUTHORITATIVE PACKET marker) before committing.
- Commit only after all blocking acceptance criteria pass; keep the working tree
  clean. Do **not** push. Do **not** start the next packet.
- End your response with the machine-parseable `AUTONOMOUS_BUILDER_RESULT` block.

The runtime must remain byte-identical with flags off, and the Direction fence
must hold. When in doubt, verify against repository truth and stop rather than
guess.
