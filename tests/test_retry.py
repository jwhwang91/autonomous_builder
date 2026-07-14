from __future__ import annotations

from autonomous_builder.execution.retry import RetryContext, RetryPolicy
from autonomous_builder.models import PacketStatus, StopReason


def ctx(**kw):
    base = dict(attempt=1, max_retries=2, status=PacketStatus.UNKNOWN,
                session_reason="idle_timeout", tree_clean=True,
                result_valid=False, verification_ok=False)
    base.update(kw)
    return RetryContext(**base)


def test_safe_transient_retry_on_clean_tree():
    d = RetryPolicy().decide(ctx(session_reason="idle_timeout", tree_clean=True))
    assert d.should_retry and d.safe


def test_malformed_block_clean_tree_retries():
    d = RetryPolicy().decide(ctx(session_reason="sentinel", result_valid=False,
                                 verification_ok=False, tree_clean=True))
    assert d.should_retry


def test_unsafe_dirty_tree_stops():
    d = RetryPolicy().decide(ctx(tree_clean=False, session_reason="eof"))
    assert not d.should_retry
    assert d.stop_reason == StopReason.DIRTY_TREE


def test_blocked_stops():
    d = RetryPolicy().decide(ctx(status=PacketStatus.BLOCKED, blocked=True, tree_clean=True))
    assert not d.should_retry
    assert d.stop_reason == StopReason.BLOCKED


def test_failed_tests_stops():
    d = RetryPolicy().decide(ctx(tests_failed=True, tree_clean=True))
    assert not d.should_retry
    assert d.stop_reason == StopReason.FAILED_TESTS


def test_graphify_failure_stops():
    d = RetryPolicy().decide(ctx(graphify_failed=True, tree_clean=True))
    assert not d.should_retry
    assert d.stop_reason == StopReason.STOP_AT_GRAPHIFY_GATE


def test_branch_change_stops():
    d = RetryPolicy().decide(ctx(branch_changed=True, tree_clean=True))
    assert d.stop_reason == StopReason.BRANCH_MISMATCH


def test_max_retries_exceeded():
    d = RetryPolicy().decide(ctx(attempt=3, max_retries=2, tree_clean=True,
                                 session_reason="idle_timeout"))
    assert not d.should_retry
    assert d.stop_reason == StopReason.MAX_RETRIES
