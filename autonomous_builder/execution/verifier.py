"""Independent verification of a Claude packet result against repository truth.

Never rely on Claude's textual output. After a session ends, the builder takes
its own git snapshot and checks the claim against it. Repository facts override
Claude prose:

* Claude says COMPLETE but the working tree is dirty          -> failure
* Claude says commit abc123 but HEAD differs                  -> failure
* commit required but HEAD did not change                     -> failure
* Claude claims a report path that does not exist             -> failure
* Claude says next=RT-E but the ledger says GR1-REPAIR-A      -> ledger wins, disagreement recorded
"""
from __future__ import annotations

from typing import Optional

from autonomous_builder.config import ProjectProfile
from autonomous_builder.models import (
    ClaudeResult,
    GitState,
    PacketStatus,
    TestOutcome,
    VerificationReport,
    WorkingTree,
    normalize_packet_id,
)
from autonomous_builder.repository.ledger import LedgerParse


def _commit_matches(claimed: Optional[str], head: Optional[str]) -> bool:
    if not claimed or not head:
        return False
    a, b = claimed.lower(), head.lower()
    return a == b or a.startswith(b) or b.startswith(a)


class ResultVerifier:
    def verify(
        self,
        *,
        result: ClaudeResult,
        profile: ProjectProfile,
        git_before: GitState,
        git_after: GitState,
        ledger_after: Optional[LedgerParse] = None,
        report_exists: Optional[bool] = None,
        ledger_exists: bool = True,
    ) -> VerificationReport:
        report = VerificationReport()
        exec_cfg = profile.execution
        proj = profile.project

        # --- repository still valid (always blocking) ---------------------
        report.add("target repo exists", git_after.exists, git_after.root)
        report.add("is a git repository", git_after.is_repo, git_after.error or "")
        if not git_after.is_repo:
            return report  # nothing else is meaningful

        # branch consistency
        if proj.expected_branch:
            ok = git_after.branch == proj.expected_branch
            report.add("branch matches expected", ok,
                       f"expected {proj.expected_branch}, got {git_after.branch}")
        if git_before.branch and git_after.branch:
            report.add("branch unchanged during packet",
                       git_before.branch == git_after.branch,
                       f"{git_before.branch} -> {git_after.branch}")

        # origin consistency (only when configured & not a placeholder)
        configured_origin = proj.git_repo_url
        if configured_origin and "CONFIGURE_ME" not in str(configured_origin):
            ok = self._origin_ok(git_after.origin_url, configured_origin)
            report.add("origin matches configured url", ok,
                       f"configured {configured_origin}, got {git_after.origin_url}")

        # ledger still exists
        report.add("execution ledger exists", ledger_exists,
                   str(profile.resolve(profile.plan.execution_ledger_path)))

        # --- status-specific verification ---------------------------------
        if result.status == PacketStatus.COMPLETE:
            self._verify_complete(result, profile, git_before, git_after, report_exists, report)
        elif result.status == PacketStatus.BLOCKED:
            report.add("status is BLOCKED (run will stop)", True,
                       "; ".join(result.blockers) or "no blockers listed", blocking=False)
        elif result.status == PacketStatus.FAILED:
            report.add("status is FAILED", False,
                       "; ".join(result.blockers) or "Claude reported FAILED")
        else:
            report.add("result block valid", False,
                       f"unusable status/parse: {result.parse_errors}")

        # --- next-packet agreement (non-blocking; authority wins) ---------
        if ledger_after and ledger_after.next_authoritative_packet and result.next_authoritative_packet:
            claimed = normalize_packet_id(result.next_authoritative_packet)
            truth = normalize_packet_id(ledger_after.next_authoritative_packet)
            if claimed != truth:
                report.disagreements.append(
                    f"Claude says next={claimed} but ledger says next={truth} — ledger wins"
                )

        # --- unexpected push (non-blocking observation) -------------------
        if not exec_cfg.push and git_after.head != git_before.head:
            # a fresh local commit that is not ahead of origin is suspicious
            suspicious = git_after.origin_url is not None and git_after.ahead == 0 and git_after.behind == 0
            report.add("no unexpected push", not suspicious,
                       "local commit appears already on origin (possible push)"
                       if suspicious else "no push detected",
                       blocking=False)

        return report

    # -- helpers ------------------------------------------------------------
    def _verify_complete(
        self,
        result: ClaudeResult,
        profile: ProjectProfile,
        git_before: GitState,
        git_after: GitState,
        report_exists: Optional[bool],
        report: VerificationReport,
    ) -> None:
        exec_cfg = profile.execution

        # working tree must be clean
        report.add(
            "working tree clean after COMPLETE",
            git_after.working_tree == WorkingTree.CLEAN,
            f"{len(git_after.changed_files)} changed, {len(git_after.untracked_files)} untracked",
        )

        # commit required -> HEAD must have advanced
        if exec_cfg.require_commit_after_packet:
            advanced = bool(git_after.head) and git_after.head != git_before.head
            report.add("commit created (HEAD advanced)", advanced,
                       f"{git_before.short_head} -> {git_after.short_head}")
            if not result.commit:
                report.add("commit hash reported", False,
                           "COMPLETE with a required commit but COMMIT: NONE")

        # A claimed commit must match reality, regardless of whether a commit was
        # *required* — "Claude says commit abc123 but HEAD differs -> failure" is
        # unconditional (module contract). This also covers the require_commit=False
        # config, where the branch above is skipped.
        if result.commit and git_after.head:
            report.add("claimed commit matches HEAD",
                       _commit_matches(result.commit, git_after.head),
                       f"claimed {result.commit[:12]}, HEAD {git_after.short_head}")

        # report file must exist
        if result.report:
            exists = report_exists
            if exists is None:
                exists = self._report_path_exists(profile, result.report)
            report.add("completion report exists", bool(exists), result.report)
        else:
            report.add("completion report path reported", False,
                       "COMPLETE but REPORT: NONE", blocking=False)

        # tests must pass (blocking iff configured to stop on failed tests)
        if exec_cfg.stop_on_failed_tests:
            report.add("tests reported PASS", result.tests == TestOutcome.PASS,
                       f"TESTS: {result.tests.value}")
        else:
            report.add("tests reported", result.tests != TestOutcome.UNKNOWN,
                       f"TESTS: {result.tests.value}", blocking=False)

        # Claude's own WORKING_TREE claim should agree with reality
        if result.working_tree != WorkingTree.UNKNOWN:
            report.add("Claude tree claim matches reality",
                       result.working_tree == git_after.working_tree,
                       f"claimed {result.working_tree.value}, actual {git_after.working_tree.value}",
                       blocking=False)

    @staticmethod
    def _report_path_exists(profile: ProjectProfile, report_path: str) -> bool:
        from pathlib import Path
        p = Path(report_path).expanduser()
        if p.is_absolute() and p.exists():
            return True
        root = profile.resolve(profile.project.root_dir)
        if root and (root / report_path).exists():
            return True
        reports = profile.resolve(profile.plan.reports_dir)
        if reports and (reports / Path(report_path).name).exists():
            return True
        return False

    @staticmethod
    def _origin_ok(actual: Optional[str], configured: str) -> bool:
        if not actual:
            return False

        def norm(u: str) -> str:
            u = u.strip().lower()
            u = u.removesuffix(".git")
            u = u.replace("git@github.com:", "github.com/")
            u = u.replace("https://", "").replace("http://", "").replace("ssh://", "")
            return u.rstrip("/")

        return norm(actual) == norm(configured)
