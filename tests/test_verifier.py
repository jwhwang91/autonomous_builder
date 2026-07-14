from __future__ import annotations

from autonomous_builder.execution.verifier import ResultVerifier
from autonomous_builder.models import (
    ClaudeResult,
    GitState,
    PacketStatus,
    TestOutcome,
    WorkingTree,
)


def _git(head, dirty=False, branch="main", origin=None):
    return GitState(root="/t", exists=True, is_repo=True, branch=branch, head=head,
                    dirty=dirty, origin_url=origin,
                    changed_files=(["a.ts"] if dirty else []))


def _complete(commit="bbb222", tests="PASS", tree="CLEAN", report="docs/reports/x.md"):
    return ClaudeResult(found_block=True, status=PacketStatus.COMPLETE, packet="RT-D",
                        commit=commit, tests=TestOutcome(tests),
                        working_tree=WorkingTree(tree), report=report)


def test_complete_dirty_tree_fails(profile):
    v = ResultVerifier().verify(
        result=_complete(), profile=profile,
        git_before=_git("aaa111"), git_after=_git("bbb222", dirty=True),
        report_exists=True,
    )
    assert not v.ok
    assert any("working tree clean" in c.name for c in v.failures)


def test_complete_clean_commit_match_ok(profile):
    v = ResultVerifier().verify(
        result=_complete(commit="bbb222"), profile=profile,
        git_before=_git("aaa111"), git_after=_git("bbb222"),
        report_exists=True,
    )
    assert v.ok


def test_commit_mismatch_fails(profile):
    v = ResultVerifier().verify(
        result=_complete(commit="deadbeef"), profile=profile,
        git_before=_git("aaa111"), git_after=_git("bbb222"),
        report_exists=True,
    )
    assert not v.ok
    assert any("claimed commit matches HEAD" in c.name for c in v.failures)


def test_no_commit_when_required_fails(profile):
    v = ResultVerifier().verify(
        result=_complete(commit="aaa111"), profile=profile,
        git_before=_git("aaa111"), git_after=_git("aaa111"),  # HEAD unchanged
        report_exists=True,
    )
    assert not v.ok
    assert any("HEAD advanced" in c.name for c in v.failures)


def test_missing_report_fails(profile):
    v = ResultVerifier().verify(
        result=_complete(), profile=profile,
        git_before=_git("aaa111"), git_after=_git("bbb222"),
        report_exists=False,
    )
    assert not v.ok
    assert any("completion report exists" in c.name for c in v.failures)


def test_failed_tests_fail(profile):
    v = ResultVerifier().verify(
        result=_complete(tests="FAIL"), profile=profile,
        git_before=_git("aaa111"), git_after=_git("bbb222"),
        report_exists=True,
    )
    assert not v.ok
    assert any("tests reported PASS" in c.name for c in v.failures)


def test_commit_mismatch_fails_even_when_commit_not_required(profile):
    # regression: a claimed commit that doesn't match HEAD must fail even when
    # require_commit_after_packet is False (the check must be unconditional).
    profile.execution.require_commit_after_packet = False
    v = ResultVerifier().verify(
        result=_complete(commit="deadbeef"), profile=profile,
        git_before=_git("aaa111"), git_after=_git("bbb222"),
        report_exists=True,
    )
    assert not v.ok
    assert any("claimed commit matches HEAD" in c.name for c in v.failures)


def test_branch_mismatch_when_expected(profile):
    profile.project.expected_branch = "release"
    v = ResultVerifier().verify(
        result=_complete(), profile=profile,
        git_before=_git("aaa111", branch="release"),
        git_after=_git("bbb222", branch="main"),
        report_exists=True,
    )
    assert not v.ok
    assert any("branch matches expected" in c.name for c in v.failures)


def test_not_a_repo_short_circuits(profile):
    ga = GitState(root="/t", exists=True, is_repo=False, error="not a repo")
    v = ResultVerifier().verify(
        result=_complete(), profile=profile,
        git_before=_git("aaa111"), git_after=ga, report_exists=True,
    )
    assert not v.ok
