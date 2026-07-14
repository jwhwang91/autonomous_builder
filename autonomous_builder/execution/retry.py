"""State-aware retry policy.

Retries are only ever taken when it is SAFE — the target working tree is clean
and no partial packet work is at risk. Anything that could sit on top of a
half-implemented packet, a real test failure, a blocked packet, or a git-identity
mismatch stops the run and produces a recovery report instead of retrying.
"""
from __future__ import annotations

from dataclasses import dataclass

from autonomous_builder.models import PacketStatus, RetryDecision, StopReason


@dataclass
class RetryContext:
    attempt: int              # 1-based number of the attempt that just ran
    max_retries: int
    status: PacketStatus
    session_reason: str       # sentinel | eof | idle_timeout | hard_timeout | bootstrap_failed
    tree_clean: bool
    result_valid: bool
    verification_ok: bool
    tests_failed: bool = False
    blocked: bool = False
    branch_changed: bool = False
    origin_mismatch: bool = False
    graphify_failed: bool = False
    destructive_requested: bool = False


# session end reasons that are transient and therefore safely retryable on a
# clean tree.
_TRANSIENT = {"eof", "idle_timeout", "hard_timeout", "bootstrap_failed", "failure"}


class RetryPolicy:
    def decide(self, ctx: RetryContext) -> RetryDecision:
        # --- unsafe conditions: never retry -------------------------------
        if ctx.blocked or ctx.status == PacketStatus.BLOCKED:
            return RetryDecision(False, False, "packet is BLOCKED; stopping", StopReason.BLOCKED)
        if ctx.graphify_failed:
            return RetryDecision(False, False, "graphify update failed after commit",
                                 StopReason.STOP_AT_GRAPHIFY_GATE)
        if ctx.branch_changed:
            return RetryDecision(False, False, "git branch changed unexpectedly",
                                 StopReason.BRANCH_MISMATCH)
        if ctx.origin_mismatch:
            return RetryDecision(False, False, "git origin mismatch", StopReason.ORIGIN_MISMATCH)
        if ctx.destructive_requested:
            return RetryDecision(False, False, "a destructive migration was requested",
                                 StopReason.VERIFICATION_FAILED)
        if ctx.tests_failed:
            return RetryDecision(False, False, "a blocking test failed", StopReason.FAILED_TESTS)
        if not ctx.tree_clean:
            return RetryDecision(
                False, False,
                "working tree is dirty with uncommitted packet work; not safe to "
                "retry — inspect and recover manually",
                StopReason.DIRTY_TREE,
            )

        # --- at this point the tree is clean; classify the failure --------
        transient = ctx.session_reason in _TRANSIENT
        malformed = not ctx.result_valid
        recoverable = transient or malformed or not ctx.verification_ok

        if not recoverable:
            # nothing to retry (shouldn't normally reach here)
            return RetryDecision(False, True, "no recoverable failure detected",
                                 StopReason.VERIFICATION_FAILED)

        # --- retries exhausted? -------------------------------------------
        if ctx.attempt > ctx.max_retries:
            return RetryDecision(
                False, True,
                f"max retries exceeded ({ctx.max_retries}) with a clean tree",
                StopReason.MAX_RETRIES,
            )

        reason = "transient session failure" if transient else (
            "malformed result block" if malformed else "verification failed"
        )
        return RetryDecision(
            True, True,
            f"safe retry ({reason}) — working tree is clean, attempt "
            f"{ctx.attempt + 1}/{ctx.max_retries + 1}",
            StopReason.NONE,
        )
