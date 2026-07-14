"""Dynamic packet-prompt construction from template files.

Long prompt strings live in ``autonomous_builder/templates/*.md`` (never inline
in Python). The builder fills ``{{PLACEHOLDER}}`` tokens with per-session
context: project paths, current git branch/HEAD, the discovered packet, a compact
previous-session handoff, recent report paths, the starting prompt (first session
only) and the configured safety rules.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from autonomous_builder.config import ProjectProfile
from autonomous_builder.models import DiscoveryResult, GitState, Handoff

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


def render(template: str, context: dict) -> str:
    """Replace ``{{KEY}}`` placeholders; unfilled placeholders become empty."""
    def sub(m: re.Match) -> str:
        return str(context.get(m.group(1), ""))
    return _PLACEHOLDER_RE.sub(sub, template)


def _bullets(items: list[str], empty: str = "  (none)") -> str:
    items = [i for i in items if i and str(i).strip()]
    if not items:
        return empty
    return "\n".join(f"- {i}" for i in items)


class PromptBuilder:
    def __init__(self, templates_dir: Optional[str | Path] = None):
        self.templates_dir = Path(templates_dir) if templates_dir else _TEMPLATES_DIR

    def _load(self, name: str) -> str:
        return (self.templates_dir / name).read_text(encoding="utf-8")

    # -- handoff ------------------------------------------------------------
    def compact_handoff(self, handoff: Optional[Handoff]) -> str:
        if handoff is None:
            return (
                "This is the FIRST session of this run. There is no previous "
                "handoff. Establish the current authoritative packet purely from "
                "repository truth (ledger, reports, git, plan)."
            )
        drift = ", ".join(handoff.plan_drift) if handoff.plan_drift else "none"
        blockers = ", ".join(handoff.blockers) if handoff.blockers else "none"
        risks = ", ".join(handoff.unresolved_risks) if handoff.unresolved_risks else "none"
        tests = ", ".join(f"{k}={v}" for k, v in handoff.tests.items()) or "unknown"
        return (
            "PREVIOUS SESSION HANDOFF\n\n"
            f"Completed packet: {handoff.completed_packet or 'UNKNOWN'}\n"
            f"Status: {handoff.status}\n"
            f"Commit: {handoff.commit or 'NONE'}\n"
            f"Tests: {tests}\n"
            f"Report: {handoff.report or 'NONE'}\n"
            f"Working tree: {handoff.working_tree}\n"
            f"Graphify update: {handoff.graphify_update}\n"
            f"Next packet proposed: {handoff.next_authoritative_packet or 'NONE'}\n"
            f"Plan drift: {drift}\n"
            f"Unresolved blockers: {blockers}\n"
            f"Unresolved risks: {risks}"
        )

    # -- safety / push ------------------------------------------------------
    def safety_rules(self, profile: ProjectProfile) -> str:
        rules = [
            "Execute EXACTLY ONE packet this session; do not start the next.",
            "Never run destructive git commands (reset --hard, clean -fd, force "
            "push, branch rewrite). Do not stash the user's changes.",
            "Commit only after all blocking acceptance criteria pass; leave the "
            "working tree clean.",
            "The orchestrator will independently verify the commit, tests, report, "
            "ledger update, and working-tree cleanliness. Repository truth wins.",
        ]
        if profile.execution.require_clean_tree_before_packet:
            rules.insert(1, "If the working tree is already dirty when you start, "
                            "STOP and report it as a blocker — do not proceed.")
        if not profile.claude.dangerously_skip_permissions:
            rules.append("Normal interactive permissions apply "
                         "(--dangerously-skip-permissions is NOT in use).")
        return _bullets(rules)

    def push_policy(self, profile: ProjectProfile) -> str:
        if profile.execution.push:
            return ("PUSH POLICY: pushing is permitted ONLY if the authoritative "
                    "packet explicitly requires it; otherwise do not push.")
        return "PUSH POLICY: pushing is DISABLED. Do not push under any circumstances."

    # -- packet prompt ------------------------------------------------------
    def build_packet_prompt(
        self,
        *,
        profile: ProjectProfile,
        git_state: GitState,
        discovery: DiscoveryResult,
        handoff: Optional[Handoff],
        starting_prompt: Optional[str],
        recent_report_paths: list[str],
        is_first_session: bool,
        result_file: Optional[str] = None,
    ) -> str:
        template = self._load("packet_prompt.md")
        disagreements = ""
        if discovery.disagreements:
            disagreements = "Recorded disagreements:\n" + _bullets(discovery.disagreements, "")
        starting_section = ""
        if is_first_session and starting_prompt:
            starting_section = (
                "\n=== STARTING PROMPT (first session only) ===\n"
                + starting_prompt.strip() + "\n"
            )
        context = {
            "PROJECT_NAME": profile.project.name,
            "PROJECT_ROOT": str(profile.resolve(profile.project.root_dir) or profile.project.root_dir),
            "PLAN_PATH": str(profile.resolve(profile.plan.path) or profile.plan.path),
            "LEDGER_PATH": str(profile.resolve(profile.plan.execution_ledger_path) or profile.plan.execution_ledger_path),
            "REPORTS_DIR": str(profile.resolve(profile.plan.reports_dir) or profile.plan.reports_dir),
            "CURRENT_BRANCH": git_state.branch or "UNKNOWN",
            "CURRENT_HEAD": git_state.head or "UNKNOWN",
            "DISCOVERED_PACKET": discovery.next_packet or "UNKNOWN",
            "DISCOVERY_SOURCE": discovery.authority_source or "unknown",
            "DISCOVERY_DISAGREEMENTS": disagreements,
            "HANDOFF_BLOCK": self.compact_handoff(handoff),
            "STARTING_PROMPT_SECTION": starting_section,
            "RECENT_REPORTS": _bullets(recent_report_paths, "  (none found)"),
            "TEST_COMMANDS": _bullets(profile.execution.test_commands),
            "GRAPHIFY_COMMAND": profile.graphify.command,
            "SAFETY_RULES": self.safety_rules(profile),
            "PUSH_POLICY": self.push_policy(profile),
            "RESULT_FILE": result_file or "",
        }
        return render(template, context)

    def build_repair_prompt(
        self,
        *,
        profile: ProjectProfile,
        git_state: GitState,
        handoff: Optional[Handoff],
        repair_reason: str,
        result_file: Optional[str] = None,
    ) -> str:
        template = self._load("repair_prompt.md")
        context = {
            "PROJECT_NAME": profile.project.name,
            "PROJECT_ROOT": str(profile.resolve(profile.project.root_dir) or profile.project.root_dir),
            "PLAN_PATH": str(profile.resolve(profile.plan.path) or profile.plan.path),
            "LEDGER_PATH": str(profile.resolve(profile.plan.execution_ledger_path) or profile.plan.execution_ledger_path),
            "REPORTS_DIR": str(profile.resolve(profile.plan.reports_dir) or profile.plan.reports_dir),
            "CURRENT_BRANCH": git_state.branch or "UNKNOWN",
            "CURRENT_HEAD": git_state.head or "UNKNOWN",
            "REPAIR_REASON": repair_reason,
            "HANDOFF_BLOCK": self.compact_handoff(handoff),
            "TEST_COMMANDS": _bullets(profile.execution.test_commands),
            "SAFETY_RULES": self.safety_rules(profile),
            "PUSH_POLICY": self.push_policy(profile),
            "RESULT_FILE": result_file or "",
        }
        return render(template, context)

    def build_final_test_deck_prompt(
        self, *, profile: ProjectProfile, git_state: GitState
    ) -> str:
        template = self._load("final_test_deck_prompt.md")
        dev_rule = (
            "Do NOT start a dev server (e.g. npm run dev)."
            if profile.finalization.do_not_start_dev_server
            else "You may start a dev server if the packet requires it."
        )
        context = {
            "PROJECT_NAME": profile.project.name,
            "PROJECT_ROOT": str(profile.resolve(profile.project.root_dir) or profile.project.root_dir),
            "PLAN_PATH": str(profile.resolve(profile.plan.path) or profile.plan.path),
            "TEST_ASSET_DIR": str(profile.resolve(profile.assets.test_asset_dir) or profile.assets.test_asset_dir or "UNSET"),
            "CURRENT_BRANCH": git_state.branch or "UNKNOWN",
            "CURRENT_HEAD": git_state.head or "UNKNOWN",
            "DEV_SERVER_RULE": dev_rule,
            "SAFETY_RULES": self.safety_rules(profile),
            "PUSH_POLICY": self.push_policy(profile),
        }
        return render(template, context)
