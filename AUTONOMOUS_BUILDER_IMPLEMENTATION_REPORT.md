# Autonomous Builder — Implementation Report

**Status:** complete, installed, tested. Builds the builder only — no DeckFlip
changes were made and no overnight run was started.

---

## 1. Architecture

A clean Python package driven by a single orchestration loop. Layers depend
downward only; `models.py` is the shared foundation (stdlib-only).

```
Runner (runner.py) — the loop
  ├─ config.py            YAML → ProjectProfile (+ validation)
  ├─ models.py            enums, dataclasses, packet-id primitives, JSON ser.
  ├─ repository/
  │    git.py             read-only GitMonitor (subprocess; refuses non-read-only git)
  │    ledger.py          LedgerParser (markers, dated UPDATEs, status table, supersession)
  │    reports.py         ReportScanner (kind, subject packet, verdict, next-hints)
  │    graphify.py        GraphifyGate (success/failure detection + recovery record)
  ├─ claude/
  │    driver.py          ClaudeDriver ABC → PexpectClaudeDriver (live) + FakeClaudeDriver (test)
  │    session.py         open→bootstrap→packet→graphify lifecycle; monitor loop
  │    parser.py          AUTONOMOUS_BUILDER_RESULT sentinel parser (tolerant)
  │    prompts.py         PromptBuilder (renders templates/*.md)
  ├─ execution/
  │    discovery.py       PacketDiscovery (authority hierarchy)
  │    verifier.py        ResultVerifier (repo truth vs prose)
  │    retry.py           RetryPolicy (state-aware safe/unsafe)
  │    watchdog.py        Watchdog (idle + hard, clock-injectable)
  │    completion.py      PlanCompletion (packet vs whole-plan)
  ├─ state/
  │    store.py           RunState persist/load/reconcile, handoffs, logs, failure reports
  │    dashboard.py       live dashboard.md + dashboard.json
  └─ templates/           packet_prompt.md, repair_prompt.md, final_test_deck_prompt.md
```

## 2. Files created

- Packaging: `pyproject.toml`, `requirements.txt`, `.gitignore`, `README.md`,
  this report.
- Package (24 modules): `autonomous_builder/{__init__,__main__,cli,config,models,runner,templates_data}.py`
  plus the `claude/`, `repository/`, `execution/`, `state/` subpackages and three
  `templates/*.md` prompt files.
- Profile: `projects/deckflip-runtime/{config.yaml,starting_prompt.md}`.
- Tests: `tests/` — 13 test modules + `conftest.py`, **98 tests**.
- `runtime_data/.gitkeep` (state/logs are git-ignored).

## 3. Dependencies

Minimal, per spec: `pexpect`, `PyYAML`, `rich`, `psutil`; dev `pytest`,
`pytest-mock`. Everything else is standard library. `psutil` is used only to reap
the Claude process tree on termination. No large framework.

## 4. Claude driver design

The whole system depends on the abstract `ClaudeDriver` (start / send_line /
send_text / send_control / read_available / is_alive / pid / terminate / close),
never on a live pexpect object.

- **`PexpectClaudeDriver`** — real, fully implemented. Spawns `claude` with a
  wide PTY, reads via `read_nonblocking` with our own timeout management,
  delivers multi-line prompts via bracketed paste, and on termination sends
  Ctrl-C, escalates through pexpect terminate, then reaps the whole process tree
  with psutil so nothing lingers.
- **`FakeClaudeDriver`** — deterministic, scriptable (`(pattern → output)`
  responders). Tests script "when the packet prompt is sent, emit the result
  block" and "when `/graphify` is sent, emit success/failure".

`ClaudeSession` layers the lifecycle on top: readiness detection, configurable
bootstrap steps (model via `--model` CLI flag; effort via `/effort`), the
monitor loop (idle/hard watchdog, interactive-prompt handling, ANSI stripping,
raw-log capture), and the graphify gate — all driveable by the fake driver.

## 5. Packet discovery & authority hierarchy

`PacketDiscovery` combines `LedgerParse`, report hints/completions, an optional
`plan` parse, the previous Claude result, and git evidence. Authority order:

1. live git state → 2. Execution Ledger → 3. reports → 4. plan → 5. Claude output.

- The ledger's explicit `NEXT AUTHORITATIVE PACKET:` colon marker is the strongest
  single pointer; dated `UPDATE (…, latest)` blocks and inline prose are ranked by
  (kind, date, recency) — **document position never silently breaks a tie**, so
  two equal-authority markers naming different packets are flagged **ambiguous**.
- Supersession (section table + inline), splits (`IP3` → `IP3-A…F`), and repairs
  (`*-REPAIR-*`) are detected. A discovered packet that is itself superseded is
  refused (ambiguous). A discovered packet that already looks complete is executed
  but flagged for Claude to re-verify.
- On ambiguity or no-data the run **stops with a diagnostic** — it never guesses.
- Grounded against the real DeckFlip ledger: correctly yields **RT-D**, lists the
  real completed/superseded sets, and surfaces the genuine ledger-vs-reports
  disagreement (RT-D/RT-E reports exist on disk while the ledger's marker still
  says RT-D).

Packet ids are opaque, evolvable strings via a shape-based grammar
(`R0`, `RT-A`, `RT-D1`, `GR1-REPAIR-A`, `IP6`, `IP9′`) — the sequence is never
hardcoded.

## 6. Result verification (repository truth wins)

After each session the builder takes its **own** git snapshot and checks the
`AUTONOMOUS_BUILDER_RESULT` claim against it: repo exists & is a repo, branch
consistency (and vs `expected_branch`), origin match (when configured), ledger
exists; and for a COMPLETE claim: clean working tree, HEAD advanced, claimed
commit == HEAD, completion report exists, tests PASS, and a non-blocking
"no unexpected push" observation. Next-packet disagreements are recorded (ledger
wins). If the prose block is garbled but repository truth independently confirms
a clean commit + report/ledger completion, the packet is accepted on that truth.

## 7. Graphify gate

After a verified commit — and while the session is still alive — the builder
sends `/graphify . --update`, monitors for success/failure signals, and only then
closes the session. On failure it stops at `STOP_AT_GRAPHIFY_GATE`, records the
packet/commit/output tail/error, and writes recovery instructions. No positive
success signal ⇒ treated as failure (safe default).

## 8. Retry policy

State-aware. Retries only on a **clean tree** for transient failures (process
crash before edits, bootstrap failure, transient timeout, malformed block with a
clean tree). Never retries on: dirty tree, failed blocking test, blocked packet,
branch/origin change, graphify failure, or a destructive-migration request —
those stop with a recovery report. Default `max_retries_per_packet: 2`.

## 9. Watchdog

Two clock-injectable timeouts per session: **idle** (default 45 min of no
meaningful output) and **hard** (default 180 min total). Meaningful-output
detection ignores whitespace/spinner noise. On timeout the session is terminated
cleanly (force only if needed), the repo is inspected, and — crucially — a dirty
half-implemented packet is **not** blindly retried.

## 10. Resume strategy

State persists after every transition. `resume` loads it, then **reconciles
against repository truth** (refreshes completed packets from ledger/reports/git,
trusts the ledger's current next packet over any stale stored value) and
continues. Stale local state is never blindly trusted.

## 11. Dashboard

`runtime_data/dashboard/dashboard.{md,json}`, rewritten after every transition,
with run status, target + root, branch/HEAD/working-tree, elapsed, current +
completed packets, commit hashes, retries, failures, blockers, plan drift,
unresolved risks, disagreements, last graphify update, next authoritative packet,
last session log, and stop reason.

## 11a. Adversarial review & hardening

After the first green suite, the code was put through a multi-agent adversarial
review (5 reviewers by subsystem → per-finding skeptic verification). It
confirmed **12 real defects**, all now fixed with regression tests:

- Ledger: negated status ("X is *not* complete" was read as COMPLETE); prose
  "do not run <other>" mis-attributing supersession to the preceding packet.
- Completion: a single passing per-packet audit could declare the whole plan
  complete without any plan-completion markers.
- Discovery: silent-ledger + disagreeing report hints now stop (ambiguous)
  instead of guessing by file order.
- Verifier: a mismatched claimed commit is now rejected even when
  `require_commit_after_packet: false`.
- Driver: the process tree is captured *before* killing the parent (reparented
  orphans no longer survive termination).
- Runner: the session is closed on every exception path (try/finally); the
  graphify gate is builder-enforced (not skippable via Claude's prose flag) and
  **durable across resume** (re-attempted before any new packet); `graphify-out/`
  output no longer trips the next packet's clean-tree preflight
  (`execution.ignore_dirty_paths`).
- Session: the sentinel is searched over the full combined read before the match
  window is truncated; interactive-prompt dedup keys on matched text, not offset.
- Parser: 64-hex SHA-256 commit object names are accepted.
- Config: numeric fields raise a clear `ConfigError` on non-numeric values;
  `graphify.timeout_minutes` is validated.

## 12. Tests

**110 tests, all passing, ~4s, no live Claude required.** Coverage matches the
spec's matrix: config (valid/missing/bad-path/invalid-timeout), ledger (standard/
split/repair/superseded/no-next/ambiguous/unicode), parser (valid/extra-prose/
missing-field/malformed-status/unicode/last-block-wins/ANSI), discovery (ledger>
Claude, reports>stale-plan, plan-complete, ambiguity, superseded-refusal), git
(clean/dirty/not-a-repo/missing/commit-changed/origin/read-only-guard), retry
(safe transient vs unsafe dirty stop, blocked, failed tests, graphify, max), state
(save/load, resume reconciliation, handoff, stop lifecycle), prompts (starting
prompt once, handoff included, safe path rendering, sentinel present), watchdog,
verifier, completion, the session lifecycle via the fake driver, and a full
runner integration suite (run-to-plan-complete, handoffs+dashboard artefacts,
**packet evolution** RT-D→GR1-REPAIR-A→RT-E, unsafe-dirty stop, graphify-gate
failure, dirty-tree preflight, **resume from repository truth**, ambiguity stop)
against a **real temporary git repo** with a Claude-simulating fake driver.

## 13. Validation result

`autonomous-builder validate --project deckflip-runtime` → **VALIDATION PASSED**,
all checks green (project root + git repo, plan, ledger, reports dir, starting
prompt, test asset dir, claude executable). `doctor` dry-runs discovery against
the real repo and reports **next = RT-D** via the ledger's explicit marker, with
the real disagreements surfaced.

## 14. How to run

```bash
cd autonomous-builder
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

autonomous-builder validate --project deckflip-runtime
autonomous-builder doctor   --project deckflip-runtime      # dry-run discovery, no Claude
autonomous-builder run      --project deckflip-runtime --max-packets 1   # one packet, observe
autonomous-builder run      --project deckflip-runtime      # full overnight run
autonomous-builder status   --project deckflip-runtime      # or read runtime_data/dashboard/dashboard.md
autonomous-builder resume   --project deckflip-runtime      # after any stop
autonomous-builder stop     --project deckflip-runtime      # graceful stop
```

## 15. Known limitations

- Interactive-TUI control over a PTY is inherently fragile; multi-line prompts use
  bracketed paste and readiness/bootstrap are configurable to absorb Claude Code
  UI drift. The model is set via `--model` (reliable) rather than the `/model`
  menu; effort via `/effort`.
- Unattended overnight runs need a permission strategy: by default the builder
  uses normal interactive Claude and does **not** auto-approve edits or pass
  `--dangerously-skip-permissions`. Configure `claude.interactive_responses` for
  your setup's prompts, or opt into a permission mode — a conscious choice.
- Graphify success/failure and report verdicts are detected heuristically (both
  configurable / conservative — they err toward "stop" or "unknown", never a
  false success).
- Live end-to-end behaviour against a real Claude session is exercised through the
  `ClaudeDriver` seam; the pexpect driver is implemented, not stubbed, but the
  automated suite deliberately uses the fake driver.

## 16. Next steps for the user

1. `source .venv/bin/activate` (or recreate the venv as above).
2. `autonomous-builder doctor --project deckflip-runtime` — confirm it reports the
   expected authoritative packet and review any disagreements.
3. `autonomous-builder run --project deckflip-runtime --max-packets 1` — watch one
   packet end-to-end, then inspect `runtime_data/dashboard/dashboard.md` and the
   handoff.
4. If the first packet looks right, run without `--max-packets` for the full plan;
   check the dashboard in the morning. Use `resume` after any safe stop.
5. Decide on a permission strategy before a truly unattended run (see §15).
