# Autonomous Builder

An external orchestration tool that autonomously drives **fresh Claude Code
sessions** through a long implementation plan, executing exactly **one
authoritative "packet" per session** and verifying **repository truth** rather
than trusting Claude's prose.

Its first target is the **DeckFlip Runtime Master Plan**, but nothing about the
target is hardcoded — packet ids, sequence, and evolution all come from the
target repository at run time.

```
Autonomous Builder repo  ──launches & supervises──►  Claude Code  ──edits──►  Target repo (DeckFlip)
        │                                                                          ├─ source changes
        └─ runtime_data/ (state, logs, handoffs, dashboard)                        ├─ tests
           (never written into the target repo)                                    ├─ reports + ledger
                                                                                    └─ commits
```

---

## What it does, per packet

1. Reads the plan, the Execution Ledger, the latest reports, and the previous
   session's handoff.
2. Inspects the target git repository (read-only).
3. **Discovers the current authoritative packet** from repository truth.
4. Launches a **fresh** interactive Claude Code process (never `--continue` /
   `--resume`).
5. Configures it (model via `--model`, effort via `/effort`, optionally graphify
   for structural understanding).
6. Sends a **dynamically generated packet prompt** and monitors output with an
   idle + hard watchdog.
7. Waits for the machine-parseable `AUTONOMOUS_BUILDER_RESULT` block.
8. **Verifies the claim against git/ledger/reports** — repository truth wins.
9. Runs the **Graphify update gate** (`/graphify . --update`) in the same session;
   stops if it fails.
10. Terminates the process completely, writes a structured **handoff**, updates
    the **dashboard**, and starts a completely fresh session for the next packet.
11. Continues until repository truth says the plan is complete (not just one
    packet), then optionally runs a final test-deck phase.

---

## Safety model

The builder **only orchestrates and verifies**. Claude makes every target-repo
change. The builder never:

- runs a destructive git command (`reset --hard`, `clean -fd`, force push, branch
  rewrite),
- stashes or discards the user's changes,
- commits on Claude's behalf,
- pushes (disabled by default — see below),
- passes `--dangerously-skip-permissions` (off by default),
- continues after a **blocked packet**, **failed test**, **dirty-tree
  verification failure**, **ambiguous discovery**, or a **failed graphify gate**.

When something is unsafe or ambiguous, it **stops and writes a recovery report**
rather than guessing.

### Repository-truth authority

The next authoritative packet is decided by this hierarchy (highest first):

1. Live target git state
2. Execution Ledger (`NEXT AUTHORITATIVE PACKET` markers, dated `UPDATE` blocks,
   status table, supersession section)
3. Latest completion / audit / repair reports
4. Long implementation plan
5. Previous Claude session result *(secondary evidence only)*

Claude's textual claims never override repository facts. Disagreements are
**recorded**, not silently resolved. Examples:

| Claude says | Repository says | Builder does |
|---|---|---|
| `COMPLETE` | working tree dirty | verification failure → stop |
| commit `abc123` | HEAD is `def456` | verification failure → stop |
| next `RT-E` | ledger says `GR1-REPAIR-A` | ledger wins, disagreement recorded |
| plan complete | a runtime packet remains | do not stop |

### Fresh-session architecture

Every packet runs in a brand-new Claude process. A finished session is
terminated completely (process tree reaped via psutil) so nothing lingers. This
keeps context clean and makes the run resumable from repository truth at any
point.

### Packet evolution

Packet ids are opaque, evolvable strings — `R0`, `RT-A`, `RT-D1`, `GR1`,
`GR1-REPAIR-A`, `IP6`, `IP9′`. The sequence is **never hardcoded**; a packet may
evolve `RT-D → RT-D1 → GR1-REPAIR-A → RT-E`, and the builder follows the ledger.

---

## Installation (macOS)

```bash
cd autonomous-builder
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Requirements: Python 3.11+, an installed `claude` CLI (default
`/opt/homebrew/bin/claude`), and git. Runtime deps: `pexpect`, `PyYAML`, `rich`,
`psutil` (tests add `pytest`, `pytest-mock`).

Verify the CLI:

```bash
autonomous-builder --version
python -m autonomous_builder --version   # equivalent
```

---

## Configuring a project

Profiles live at `projects/<slug>/config.yaml`. A fully worked example ships at
`projects/deckflip-runtime/`. Scaffold a new one with:

```bash
autonomous-builder init --project my-project
$EDITOR projects/my-project/config.yaml
```

Key fields (see the example for all of them):

```yaml
project:
  name: DeckFlip Runtime
  root_dir: /Users/you/github_projects/DeckFlip
  git_repo_url: https://github.com/you/DeckFlip.git   # or null to skip origin check
  expected_branch: null                                # null = do not enforce
plan:
  path: .../docs/RUNTIME_MASTER_PLAN.md
  execution_ledger_path: .../docs/reports/..._EXECUTION_LEDGER.md
  reports_dir: .../docs/reports
  starting_prompt_path: ./projects/deckflip-runtime/starting_prompt.md
claude:
  executable: /opt/homebrew/bin/claude
  model: opus
  effort: ultracode
  idle_timeout_minutes: 45
  hard_timeout_minutes: 180
  max_retries_per_packet: 2
graphify:
  update_after_commit: true
  command: /graphify . --update
execution:
  require_clean_tree_before_packet: true
  require_commit_after_packet: true
  push: false
```

> **The git repo URL is never invented.** In the shipped DeckFlip profile it was
> read from the target's `git remote get-url origin` (and is documented as such).
> Set it to `null` to skip origin verification.

---

## Usage

```bash
autonomous-builder validate --project deckflip-runtime    # check paths & config
autonomous-builder doctor   --project deckflip-runtime    # + dry-run packet discovery (no Claude)
autonomous-builder run      --project deckflip-runtime    # start an autonomous run
autonomous-builder run      --project deckflip-runtime --max-packets 1   # one packet then pause
autonomous-builder resume   --project deckflip-runtime    # resume from repository truth
autonomous-builder status   --project deckflip-runtime    # current run status
autonomous-builder stop     --project deckflip-runtime    # graceful stop at next packet boundary
autonomous-builder final-test-deck --project deckflip-runtime   # optional finalization (disabled by default)
autonomous-builder projects                               # list profiles
```

`python -m autonomous_builder <cmd>` works identically.

### Reading the dashboard

Live, morning-readable status is written to:

```
runtime_data/dashboard/dashboard.md      # human-readable
runtime_data/dashboard/dashboard.json    # machine-readable
```

It shows run status, branch/HEAD, elapsed time, current + completed packets,
commit hashes, retries, failures, blockers, plan drift, unresolved risks, the
last graphify update, the next authoritative packet, working-tree status, the
last session log path, disagreements, and the stop reason.

### Stopping safely

`autonomous-builder stop` requests a **graceful** halt: the run finishes what it
is safely able to and stops before starting the next packet. `stop --force` also
attempts to kill the live Claude process — this can leave the target tree dirty,
so prefer the graceful form.

### Resuming & failure recovery

State persists after every transition to `runtime_data/state/<slug>.json`. On
`resume`, the builder loads state, **reconciles it against repository truth**
(never blindly trusting stale local state), and continues from the ledger's
current authoritative packet.

When a run stops unsafely it writes a recovery report under
`runtime_data/failures/` describing the packet, the verification failures, the
parse errors, and concrete recovery steps. Typical stop reasons and what to do:

| Stop reason | Meaning | Recovery |
|---|---|---|
| `DIRTY_TREE_BEFORE_PACKET` | target tree dirty before a packet | inspect & resolve manually, then `resume` |
| `STOP_AT_GRAPHIFY_GATE` | `/graphify . --update` did not confirm success | run graphify manually, then `resume` |
| `AMBIGUOUS_PACKET_DISCOVERY` | conflicting authoritative markers | fix the ledger's NEXT marker, then `resume` |
| `PACKET_BLOCKED` | Claude reported BLOCKED | address the blocker, then `resume` |
| `FAILED_TESTS` | a blocking test failed | fix, then `resume` |
| `MAX_RETRIES_EXCEEDED` | transient failures exhausted retries (clean tree) | inspect logs, then `resume` |

### Logs

```
runtime_data/
  logs/builder.log                          high-level runner log
  logs/sessions/<ts>_<packet>_attempt-N.log raw Claude session stream (ANSI preserved)
  results/<ts>_<packet>_attempt-N.json      parsed result block
  handoffs/<packet>.json + <packet>.md      machine + human handoff
  state/<slug>.json                         resumable run state
  dashboard/dashboard.{md,json}             live dashboard
  failures/<ts>_<name>.md                   timeout / recovery / ambiguity reports
```

Parsed copies are ANSI-stripped; the raw session log preserves the stream.

---

## Testing

```bash
pytest -q          # 110 tests, no live Claude needed
```

Unit tests never require a live Claude session — they use a scriptable
`FakeClaudeDriver`. The full runner loop (discovery → fresh session → commit
verification → graphify gate → handoff → next packet, plus packet evolution,
resume, unsafe-dirty stop, and ambiguity stop) is exercised end-to-end against a
**real temporary git repo** with a fake driver that simulates Claude's commits.
Ledger, report, and discovery parsers are additionally validated against the
real DeckFlip ledger/reports via `autonomous-builder doctor`.

The live pexpect driver (`PexpectClaudeDriver`) is fully implemented, not
stubbed; the `ClaudeDriver` abstraction is the seam for both.

---

## Known limitations & assumptions

- **pexpect / TTY behaviour.** Driving an interactive TUI over a pseudo-terminal
  is inherently fragile. Prompts are delivered via bracketed paste
  (`\e[200~ … \e[201~`) to avoid premature submission of multi-line text; set
  `prompt_delivery` if your Claude version needs a different mode.
- **Claude Code version drift.** UI strings and menus change between versions.
  The driver therefore uses **configurable** bootstrap steps and regex-based
  prompt detection, passes the model as a CLI flag (`--model`) rather than
  driving the `/model` menu, and treats readiness leniently (proceeds if the
  process is alive and producing output even when no exact ready string matches).
- **Autonomous permissions.** By default the builder uses normal interactive
  Claude behaviour and does **not** auto-approve edits or pass
  `--dangerously-skip-permissions`. For a truly unattended overnight run you must
  either configure `claude.interactive_responses` (regex → key) for the prompts
  your setup produces, or opt into a permission mode — deliberately a conscious
  choice, not a silent default.
- **Graphify assumptions.** The graphify gate looks for success/failure signals
  in `/graphify . --update` output (e.g. `Graph complete`, `ERROR: Graph is
  empty`). These patterns are configurable under `graphify.success_patterns` /
  `failure_patterns`. If no positive success signal appears, the gate treats the
  update as **failed** (safe default) and stops. A failed gate is **durable** —
  it is re-attempted on the next run/resume before any new packet.
  `/graphify . --update` writes `graphify-out/` into the target working tree; the
  pre-packet clean-tree check ignores those paths (`execution.ignore_dirty_paths`,
  default `["graphify-out/"]`) so they don't stop the loop. Consider gitignoring
  `graphify-out/` in the target repo as well.
- **Verdict extraction** from reports is heuristic (read only from verdict-
  declaration lines); it errs toward "unknown" rather than a false PASS.
- **Why push is disabled by default.** Pushing is outward-facing and hard to
  reverse. The builder's job is local, verifiable progress; publishing is a
  separate, deliberate decision left to you (`execution.push: true` to enable).

---

## Project layout

```
autonomous_builder/
  cli.py  config.py  models.py  runner.py  templates_data.py
  claude/    driver.py  session.py  parser.py  prompts.py
  repository/ git.py  ledger.py  reports.py  graphify.py
  execution/ discovery.py  verifier.py  retry.py  watchdog.py  completion.py
  state/     store.py  dashboard.py
  templates/ packet_prompt.md  repair_prompt.md  final_test_deck_prompt.md
projects/deckflip-runtime/  config.yaml  starting_prompt.md
runtime_data/   (generated at run time)
tests/
```
