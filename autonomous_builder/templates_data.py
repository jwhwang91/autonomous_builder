"""Inline scaffolding text for ``autonomous-builder init``.

A generic example profile with explicit placeholders. Real profiles live under
``projects/<slug>/config.yaml``; see ``projects/deckflip-runtime`` for a fully
worked example.
"""
from __future__ import annotations

EXAMPLE_CONFIG_YAML = """\
# Autonomous Builder project profile.
# Replace <CONFIGURE_ME> placeholders. Do NOT invent the git repo url.

project:
  name: My Project
  root_dir: /absolute/path/to/target/repo
  git_repo_url: <CONFIGURE_ME>          # e.g. https://github.com/you/target.git
  expected_branch: null                 # null = do not enforce a branch

plan:
  path: /absolute/path/to/target/repo/docs/IMPLEMENTATION_PLAN.md
  execution_ledger_path: /absolute/path/to/target/repo/docs/reports/EXECUTION_LEDGER.md
  reports_dir: /absolute/path/to/target/repo/docs/reports
  starting_prompt_path: ./starting_prompt.md

assets:
  test_asset_dir: null

claude:
  executable: /opt/homebrew/bin/claude
  model: opus
  effort: ultracode
  fresh_session_per_packet: true
  max_retries_per_packet: 2
  idle_timeout_minutes: 45
  hard_timeout_minutes: 180
  permission_mode: default              # normal interactive Claude behaviour
  dangerously_skip_permissions: false   # keep false (safe default)
  # bootstrap_steps default to a single '/effort <effort>' step; model is passed
  # as a reliable CLI flag. Override here to customise the session bootstrap.
  # bootstrap_steps:
  #   - send: "/effort ultracode"
  #     description: set effort
  #   - send: "/graphify . --update"
  #     description: graphify understanding at bootstrap
  #     settle_seconds: 3.0

graphify:
  update_after_commit: true
  command: /graphify . --update
  run_at_bootstrap: false
  stop_on_failure: true
  timeout_minutes: 30

execution:
  require_clean_tree_before_packet: true
  require_commit_after_packet: true
  stop_on_failed_tests: true
  stop_on_blocker: true
  stop_when_plan_complete: true
  push: false
  test_commands:
    - npm run typecheck
    - npm run test
    - npm run build

finalization:
  create_test_deck: false
  require_runtime_audit_pass: true
  do_not_start_dev_server: true

state:
  data_dir: runtime_data
"""

EXAMPLE_STARTING_PROMPT = """\
# Starting prompt (first session only)

This text is injected into the FIRST packet prompt only. Use it to give the very
first session any one-time orientation it needs — for example:

- where the authoritative plan and ledger live
- any repository conventions the plan assumes
- a reminder that repository truth (git, ledger, reports) is authoritative and
  that exactly ONE packet is to be executed per session

Keep it short. The per-packet prompt already covers the standard workflow,
safety rules, and the required result block.
"""
