"""Shared test fixtures.

Provides a real temporary git repository (so GitMonitor exercises real git), a
constructed ProjectProfile pointing at it, an advancing clock, and a helper to
build a Claude-simulating FakeClaudeDriver whose responder performs the git
commit + ledger update a real packet would, then emits the result block.
"""
from __future__ import annotations

import itertools
import subprocess
from pathlib import Path

import pytest

from autonomous_builder.config import (
    AssetsConfig,
    BootstrapStep,
    ClaudeConfig,
    ExecutionConfig,
    FinalizationConfig,
    GraphifyConfig,
    PlanConfig,
    ProjectConfig,
    ProjectProfile,
    StateConfig,
)


# ---------------------------------------------------------------------------
# advancing clock
# ---------------------------------------------------------------------------
@pytest.fixture
def adv_clock():
    counter = itertools.count(0.0, 0.01)
    return lambda: next(counter)


# ---------------------------------------------------------------------------
# temp git target repo
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True).stdout.strip()


class Target:
    """A temporary target git repo with plan/ledger/reports, plus commit helpers."""

    def __init__(self, root: Path, ledger_next: str = "RT-D"):
        self.root = root
        self.docs = root / "docs"
        self.reports = self.docs / "reports"
        self.reports.mkdir(parents=True, exist_ok=True)
        self.plan_path = self.docs / "PLAN.md"
        self.ledger_path = self.reports / "LEDGER.md"
        self.plan_path.write_text("# Plan\nPackets RT-A..RT-J.\n", encoding="utf-8")
        self.write_ledger_next(ledger_next)
        (root / "src.txt").write_text("v0\n", encoding="utf-8")
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "test@example.com")
        _git(root, "config", "user.name", "Test")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "initial")
        _git(root, "branch", "-M", "main")

    # -- ledger content ----------------------------------------------------
    def write_ledger_next(self, next_packet: str | None, *, completed: list[str] | None = None,
                          plan_complete: bool = False):
        completed = completed or []
        lines = ["# Execution Ledger", ""]
        for c in completed:
            lines.append(f"- **{c} is complete.**")
        if next_packet:
            lines.append(f"NEXT AUTHORITATIVE PACKET: {next_packet} — the next thing to do.")
        else:
            lines.append("NEXT AUTHORITATIVE PACKET: NONE")
        if plan_complete:
            lines.append("The entire plan is complete. All packets complete.")
        self.ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def head(self) -> str:
        return _git(self.root, "rev-parse", "HEAD")

    def branch(self) -> str:
        return _git(self.root, "rev-parse", "--abbrev-ref", "HEAD")

    def make_dirty(self):
        (self.root / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    # -- simulate a Claude packet: edit + report + ledger + commit ----------
    def simulate_packet(self, packet: str, next_packet: str | None, *,
                        write_report: bool = True, plan_complete: bool = False,
                        completed: list[str] | None = None) -> str:
        (self.root / "src.txt").write_text(f"work for {packet}\n", encoding="utf-8")
        report_rel = None
        if write_report:
            report = self.reports / f"phase-{packet}-report.md"
            report.write_text(f"# {packet} report\nVerdict: PASS\nBlockers: none\n", encoding="utf-8")
            report_rel = f"docs/reports/{report.name}"
        self.write_ledger_next(next_packet, completed=(completed or []) + [packet],
                               plan_complete=plan_complete)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", f"{packet}: done")
        return report_rel


@pytest.fixture
def target(tmp_path) -> Target:
    return Target(tmp_path / "target")


# ---------------------------------------------------------------------------
# profile pointing at the target
# ---------------------------------------------------------------------------
def build_profile(target: Target, data_dir: Path, **overrides) -> ProjectProfile:
    claude = ClaudeConfig(
        executable="/bin/echo",  # exists; never actually run in fake-driver tests
        model="opus", effort="ultracode",
        poll_interval_seconds=0.001,
        idle_timeout_minutes=overrides.get("idle_minutes", 45),
        hard_timeout_minutes=overrides.get("hard_minutes", 180),
        max_retries_per_packet=overrides.get("max_retries", 2),
        ready_patterns=[r">\s*$", "READY"],
        # the fake driver emits the result block immediately (an "instant" Claude),
        # so disable the real-run echo-drain / warm-up guard in tests
        echo_settle_seconds=0.0,
        min_packet_result_seconds=0.0,
        bootstrap_steps=[BootstrapStep(send="/effort ultracode", settle_seconds=0.0)],
    )
    profile = ProjectProfile(
        project=ProjectConfig(name="Test Project", root_dir=str(target.root),
                              git_repo_url=overrides.get("git_url"),
                              expected_branch=overrides.get("expected_branch")),
        plan=PlanConfig(path=str(target.plan_path),
                        execution_ledger_path=str(target.ledger_path),
                        reports_dir=str(target.reports),
                        starting_prompt_path=overrides.get("starting_prompt_path")),
        assets=AssetsConfig(test_asset_dir=overrides.get("test_asset_dir")),
        claude=claude,
        graphify=GraphifyConfig(update_after_commit=overrides.get("graphify", True)),
        execution=ExecutionConfig(push=False,
                                  require_clean_tree_before_packet=overrides.get("require_clean", True)),
        finalization=FinalizationConfig(require_runtime_audit_pass=False),
        state=StateConfig(data_dir=str(data_dir)),
        source_path=str(target.root / "config.yaml"),
        slug="test-project",
        repo_root=str(data_dir.parent),
    )
    return profile


@pytest.fixture
def profile(target, tmp_path) -> ProjectProfile:
    return build_profile(target, tmp_path / "runtime_data")


# ---------------------------------------------------------------------------
# result block helper
# ---------------------------------------------------------------------------
def result_block(packet: str, commit: str, next_packet: str | None, *,
                 status="COMPLETE", tests="PASS", tree="CLEAN",
                 plan_complete="NO", report="docs/reports/x.md",
                 graphify="YES") -> str:
    return (
        "Here is my result.\n"
        "AUTONOMOUS_BUILDER_RESULT\n"
        f"STATUS: {status}\n"
        f"PACKET: {packet}\n"
        f"COMMIT: {commit}\n"
        f"NEXT_AUTHORITATIVE_PACKET: {next_packet or 'NONE'}\n"
        f"TESTS: {tests}\n"
        f"WORKING_TREE: {tree}\n"
        f"PLAN_COMPLETE: {plan_complete}\n"
        f"REPORT: {report}\n"
        f"GRAPHIFY_UPDATE_REQUIRED: {graphify}\n"
        "BLOCKERS:\n"
        "PLAN_DRIFT:\n"
        "END_AUTONOMOUS_BUILDER_RESULT\n"
    )


GRAPHIFY_SUCCESS = "Graph: 1200 nodes, 3400 edges, 12 communities\nGraph complete. Outputs in graphify-out/\n"
