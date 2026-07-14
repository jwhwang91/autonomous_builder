"""The orchestration loop.

Drives fresh Claude sessions through the evolving authoritative packet sequence:

    preflight git  ->  discover authoritative packet  ->  fresh Claude session
    (bootstrap -> packet prompt -> monitor)  ->  parse result  ->  verify against
    repository truth  ->  graphify update gate  ->  handoff  ->  next fresh session

Repository truth is authoritative throughout. The runner never edits the target
repository and never runs a destructive git command. It stops (with a recovery
report) rather than guessing whenever discovery is ambiguous, a packet is
blocked, tests fail, the tree is dirty, or the graphify gate fails.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from autonomous_builder.claude.driver import DriverConfig, PexpectClaudeDriver
from autonomous_builder.claude.parser import ResultParser
from autonomous_builder.claude.prompts import PromptBuilder
from autonomous_builder.claude.session import ClaudeSession
from autonomous_builder.config import BootstrapStep, ProjectProfile
from autonomous_builder.execution.discovery import PacketDiscovery
from autonomous_builder.execution.retry import RetryContext, RetryPolicy
from autonomous_builder.execution.verifier import ResultVerifier
from autonomous_builder.execution.watchdog import Watchdog
from autonomous_builder.models import (
    ClaudeResult,
    DiscoveryOutcome,
    GitState,
    Handoff,
    PacketStatus,
    RunState,
    RunStatus,
    StopReason,
    TestOutcome,
    WorkingTree,
    find_packet_ids,
    normalize_packet_id,
    utcnow_iso,
    PacketHistoryEntry,
)
from autonomous_builder.repository.git import GitMonitor
from autonomous_builder.repository.graphify import GraphifyGate, GraphifyResult
from autonomous_builder.repository.ledger import LedgerParse, LedgerParser
from autonomous_builder.repository.reports import ReportScanner
from autonomous_builder.state.dashboard import DashboardExtras, DashboardWriter
from autonomous_builder.state.store import StateStore


def default_bootstrap_steps(profile: ProjectProfile) -> list[BootstrapStep]:
    # Effort and model are set via CLI flags (--effort/--model) at spawn, NOT via
    # the /effort or /model slash menus (those are interactive pickers that tangle
    # with prompt delivery). So the default bootstrap is empty unless the profile
    # explicitly configures steps.
    steps = list(profile.claude.bootstrap_steps)
    if profile.graphify.run_at_bootstrap:
        steps.append(BootstrapStep(
            send=profile.graphify.command,
            description="graphify structural understanding (bootstrap)",
            settle_seconds=3.0,
        ))
    return steps


def default_session_factory(profile: ProjectProfile) -> Callable[[str], ClaudeSession]:
    """Build a fresh Pexpect-backed ClaudeSession per packet."""
    def factory(raw_log_path: str) -> ClaudeSession:
        args: list[str] = []
        if profile.claude.model:
            args += ["--model", profile.claude.model]
        if profile.claude.effort:
            args += ["--effort", profile.claude.effort]  # reliable; not the /effort menu
        if profile.claude.dangerously_skip_permissions:
            args.append("--dangerously-skip-permissions")
        args += list(profile.claude.extra_args)
        root = str(profile.resolve(profile.project.root_dir) or profile.project.root_dir)
        driver = PexpectClaudeDriver(DriverConfig(
            command=profile.claude.executable,
            args=args,
            cwd=root,
            env=dict(os.environ),
        ))
        return ClaudeSession(driver, profile.claude, raw_log_path=raw_log_path)
    return factory


@dataclass
class StepResult:
    stop: bool = False
    stop_reason: StopReason = StopReason.NONE
    stop_detail: str = ""
    success: bool = False
    handoff: Optional[Handoff] = None


class Runner:
    def __init__(
        self,
        profile: ProjectProfile,
        *,
        session_factory: Optional[Callable[[str], ClaudeSession]] = None,
        git_monitor: Optional[GitMonitor] = None,
        store: Optional[StateStore] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_packets: Optional[int] = None,
        progress: Optional[Callable[[str], None]] = None,
        notify: Optional[Callable[[str, str], None]] = None,
    ):
        self.profile = profile
        self.progress = progress or (lambda m: None)  # live console progress
        self.notify = notify or (lambda title, msg: None)  # native desktop notification
        self.store = store or StateStore(profile)
        root = str(profile.resolve(profile.project.root_dir) or profile.project.root_dir)
        self.git = git_monitor or GitMonitor(root)
        self.session_factory = session_factory or default_session_factory(profile)
        self.discovery = PacketDiscovery(
            require_audit_pass=profile.finalization.require_runtime_audit_pass
        )
        self.verifier = ResultVerifier()
        self.retry = RetryPolicy()
        self.prompts = PromptBuilder()
        self.graphify = GraphifyGate(profile.graphify)
        self.dashboard = DashboardWriter(self.store.dashboard_dir)
        self.parser = ResultParser()
        self._clock = clock
        self._sleep = sleep
        self.max_packets = max_packets
        self._extras = DashboardExtras()

    # -- live progress (console + builder.log) ------------------------------
    def _say(self, msg: str) -> None:
        self.store.log(msg)
        self.progress(msg)

    def _session_logger(self, msg: str) -> None:
        self.store.log(f"session: {msg}")
        self.progress(f"   · {msg}")

    def _heartbeat(self, label: str, elapsed: float, idle_for: float, nbytes: int) -> None:
        m, s = divmod(int(elapsed), 60)
        self.progress(
            f"   ⏳ {label}: {m}m{s:02d}s elapsed · last Claude output "
            f"{int(idle_for)}s ago · {nbytes / 1024:.1f} KB captured"
        )

    # ======================================================================
    # entry points
    # ======================================================================
    def run(self, *, resume: bool = False) -> RunState:
        state = self._load_or_new(resume)
        state.status = RunStatus.RUNNING.value
        state.stop_reason = StopReason.NONE.value
        state.stop_detail = ""
        self._save(state)
        self.store.log(f"=== run start (resume={resume}) run_id={state.run_id} ===")

        self.store.clear_stop()
        packets_done = 0
        try:
            while True:
                if self.store.stop_requested():
                    self.store.clear_stop()
                    self._stop(state, self.git.snapshot(), StopReason.USER_STOP,
                               "stop requested by operator (graceful, at packet boundary)")
                    break
                if self.max_packets is not None and packets_done >= self.max_packets:
                    self.store.log(f"reached max_packets={self.max_packets}; pausing")
                    break

                git_state = self.git.snapshot()
                self._refresh_dashboard(state, git_state)

                # a durable graphify gate: re-attempt any pending update before
                # advancing to a new packet (makes STOP_AT_GRAPHIFY_GATE survive resume)
                pend_stop = self._resolve_pending_graphify(state, git_state)
                if pend_stop:
                    self._stop(state, git_state, *pend_stop)
                    break

                stop = self._preflight(git_state)
                if stop:
                    self._stop(state, git_state, *stop)
                    break

                ledger, plan, reports, discovery = self._discover(git_state)
                self._extras.next_packet_source = discovery.authority_source
                self._extras.disagreements = list(discovery.disagreements)

                if discovery.outcome in (DiscoveryOutcome.AMBIGUOUS, DiscoveryOutcome.NO_DATA):
                    detail = discovery.ambiguity_reason or "no discovery data"
                    self._write_ambiguity_report(discovery, git_state)
                    reason = (StopReason.AMBIGUOUS_DISCOVERY
                              if discovery.outcome == DiscoveryOutcome.AMBIGUOUS
                              else StopReason.NO_DATA)
                    self._stop(state, git_state, reason, detail)
                    break

                if discovery.outcome == DiscoveryOutcome.PLAN_COMPLETE:
                    state.plan_complete = True
                    self._stop(state, git_state, StopReason.PLAN_COMPLETE,
                               "; ".join(discovery.plan_complete_evidence) or "plan complete")
                    self.store.log("PLAN COMPLETE — repository truth indicates no remaining packet")
                    break

                packet = discovery.next_packet
                state.next_packet = packet
                is_first = not state.first_session_done
                self._say(f"authoritative packet: {packet} (source {discovery.authority_source})")

                step = self._execute_packet(state, packet, discovery, git_state, is_first)
                packets_done += 1
                state.first_session_done = True

                if step.stop:
                    self._stop(state, self.git.snapshot(), step.stop_reason, step.stop_detail)
                    break

                if not step.success:
                    # execute_packet exhausted retries without a clean stop reason
                    self._stop(state, self.git.snapshot(), StopReason.VERIFICATION_FAILED,
                               "packet did not complete and no safe retry remained")
                    break

                self._save(state)
        finally:
            self._save(state)
            self._refresh_dashboard(state, self.git.snapshot())
            self.store.log(f"=== run end status={state.status} stop={state.stop_reason} ===")
        return state

    def resume(self) -> RunState:
        return self.run(resume=True)

    # ======================================================================
    # preflight & discovery
    # ======================================================================
    def _preflight(self, git_state: GitState) -> Optional[tuple[StopReason, str]]:
        proj = self.profile.project
        if not git_state.exists:
            return (StopReason.WRONG_REPO, f"target root does not exist: {git_state.root}")
        if not git_state.is_repo:
            return (StopReason.NOT_A_REPO, f"target root is not a git repository: {git_state.root}")
        if proj.git_repo_url and "CONFIGURE_ME" not in str(proj.git_repo_url):
            if not self._origin_ok(git_state.origin_url, proj.git_repo_url):
                return (StopReason.ORIGIN_MISMATCH,
                        f"origin {git_state.origin_url} != configured {proj.git_repo_url}")
        if proj.expected_branch and git_state.branch != proj.expected_branch:
            return (StopReason.BRANCH_MISMATCH,
                    f"branch {git_state.branch} != expected {proj.expected_branch}")
        if self.profile.execution.require_clean_tree_before_packet:
            offending = self._non_ignored_dirty(git_state)
            if offending:
                preview = ", ".join(offending[:8])
                return (StopReason.DIRTY_TREE,
                        f"working tree is dirty before packet ({preview}); refusing to proceed. "
                        "Inspect and resolve manually — the builder will not stash or discard changes.")
        return None

    def _non_ignored_dirty(self, git_state: GitState) -> list[str]:
        """Changed/untracked files EXCLUDING configured ignore paths (graphify-out/)."""
        ignore = self.profile.execution.ignore_dirty_paths
        files = list(git_state.changed_files) + list(git_state.untracked_files)
        return [f for f in files if not any(f == p or f.startswith(p.rstrip("/") + "/") or f.startswith(p)
                                            for p in ignore)]

    def _discover(self, git_state: GitState):
        ledger = LedgerParser().parse_file(self.profile.resolve(self.profile.plan.execution_ledger_path))
        plan_path = self.profile.resolve(self.profile.plan.path)
        plan = LedgerParser().parse_file(plan_path) if plan_path and plan_path.exists() else LedgerParse()
        reports = ReportScanner(self.profile.resolve(self.profile.plan.reports_dir))
        completions = [i.header_packets[0] for i in reports.scan() if i.header_packets]
        git_packets = find_packet_ids(self.git.last_commit_message() or "")
        discovery = self.discovery.discover(
            ledger=ledger,
            plan=plan,
            report_hints=reports.next_packet_hints(4),
            report_completions=completions,
            audits=reports.audits(),
            git_state=git_state,
            git_recent_packets=git_packets,
        )
        return ledger, plan, reports, discovery

    # ======================================================================
    # one packet (with state-aware retries)
    # ======================================================================
    def _execute_packet(self, state: RunState, packet: str, discovery,
                        git_before: GitState, is_first: bool) -> StepResult:
        attempt = 1
        max_retries = self.profile.claude.max_retries_per_packet
        while True:
            state.current_packet = packet
            state.attempt = attempt
            state.status = RunStatus.RUNNING.value
            self._save(state)
            self._say(f"--- packet {packet} attempt {attempt} — launching a fresh Claude session ---")

            log_path = self.store.session_log_path(packet, attempt)
            state.last_session_log = str(log_path)
            session = self.session_factory(str(log_path))
            session.logger = self._session_logger      # stream steps to the console
            session.heartbeat = self._heartbeat         # live "still working" ticks

            # Claude writes its result block here; the builder polls + reads it
            # directly (clean, prompt completion). Clear any stale file first.
            result_file = self.store.result_block_path(packet, attempt)
            try:
                result_file.unlink()
            except FileNotFoundError:
                pass

            # The session (a live Claude process) is ALWAYS closed on the way out
            # of this attempt — including on any exception path — so a finished
            # session never lingers in the background.
            try:
                monitor_reason, result, git_after, bootstrap_ok = self._run_session(
                    session, state, packet, discovery, git_before, is_first, attempt, result_file
                )
                self.progress(f"   · session ended ({monitor_reason}); verifying against repository truth…")

                self.store.write_result_json(packet, attempt, result)
                history = PacketHistoryEntry(
                    packet=packet, attempt=attempt, status=result.status.value,
                    commit=git_after.head, started_at=utcnow_iso(), ended_at=utcnow_iso(),
                    session_log=str(log_path), note=f"session={monitor_reason}",
                )

                # --- assess outcome vs repository truth --------------------
                verification = self.verifier.verify(
                    result=result, profile=self.profile,
                    git_before=git_before, git_after=git_after,
                    ledger_after=self._reparse_ledger(),
                    ledger_exists=self._ledger_exists(),
                )
                self._extras.disagreements = list(dict.fromkeys(
                    self._extras.disagreements + verification.disagreements
                ))
                # Repository truth can rescue a garbled/COMPLETE-but-unverified
                # block, but must NEVER override an explicit BLOCKED/FAILED status
                # — those are strong signals from Claude that we must stop.
                explicit_bad = result.is_valid and result.status in (
                    PacketStatus.BLOCKED, PacketStatus.FAILED
                )
                repo_success = (not explicit_bad) and self._repo_truth_success(
                    packet, git_before, git_after
                )
                success = (
                    result.status == PacketStatus.COMPLETE and result.is_valid and verification.ok
                ) or repo_success

                if success:
                    history.verification_ok = True
                    graphify_result = self._maybe_graphify(session, packet, git_before, git_after)
                    if graphify_result is not None and not graphify_result.success:
                        self._record_graphify(state, graphify_result)
                        # persist a durable "graphify pending" marker so a resume
                        # re-attempts the update before advancing to a new packet.
                        state.graphify_pending = {"packet": packet, "commit": git_after.head}
                        self._write_graphify_failure(graphify_result)
                        history.stop_reason = StopReason.STOP_AT_GRAPHIFY_GATE.value
                        state.packet_history.append(history)
                        self._save(state)
                        return StepResult(stop=True, stop_reason=StopReason.STOP_AT_GRAPHIFY_GATE,
                                          stop_detail=graphify_result.error or "graphify update failed")
                    if graphify_result is not None:
                        self._record_graphify(state, graphify_result)

                    handoff = self._build_handoff(packet, result, git_after, graphify_result, attempt)
                    self.store.write_handoff(handoff)
                    self._commit_success_to_state(state, packet, git_after, handoff)
                    history.commit = git_after.head
                    state.packet_history.append(history)
                    self._say(f"✅ packet {packet} COMPLETE — commit {git_after.short_head}")
                    self.notify(f"✅ {packet} COMPLETE",
                                f"commit {git_after.short_head} · next {handoff.next_authoritative_packet or '—'}")
                    return StepResult(success=True, handoff=handoff)

                # --- failure: decide retry vs stop -------------------------
                decision = self._decide_retry(
                    result, verification, git_before, git_after,
                    monitor_reason, bootstrap_ok, attempt, max_retries,
                )
                history.stop_reason = (decision.stop_reason.value
                                       if not decision.should_retry else StopReason.NONE.value)
                state.packet_history.append(history)
                self._save(state)

                if decision.should_retry:
                    self.store.log(f"retrying packet {packet}: {decision.reason}")
                    attempt += 1
                    continue

                self.store.log(f"stopping on packet {packet}: {decision.reason}")
                self._write_recovery_report(packet, attempt, decision, result, verification, git_after)
                return StepResult(stop=True, stop_reason=decision.stop_reason, stop_detail=decision.reason)
            finally:
                session.close()

    def _decide_retry(self, result, verification, git_before, git_after,
                      monitor_reason, bootstrap_ok, attempt, max_retries):
        branch_changed = bool(git_before.branch and git_after.branch
                              and git_before.branch != git_after.branch)
        ctx = RetryContext(
            attempt=attempt,
            max_retries=max_retries,
            status=result.status,
            session_reason=(monitor_reason if bootstrap_ok else "bootstrap_failed"),
            tree_clean=(git_after.working_tree == WorkingTree.CLEAN),
            result_valid=result.is_valid,
            verification_ok=verification.ok,
            tests_failed=self._tests_failed(result, verification),
            blocked=(result.status == PacketStatus.BLOCKED),
            branch_changed=branch_changed,
            origin_mismatch=False,
            graphify_failed=False,
        )
        return self.retry.decide(ctx)

    def _run_session(self, session: ClaudeSession, state, packet, discovery,
                     git_before, is_first, attempt, result_file=None):
        """Open, bootstrap and run one Claude session; return (reason, result, git_after, bootstrap_ok)."""
        try:
            ready = session.open()
        except Exception as exc:  # pragma: no cover - spawn failure
            self.store.log(f"session spawn failed: {exc}", logging.ERROR)
            return ("bootstrap_failed", ClaudeResult(), self.git.snapshot(), False)
        if not ready:
            return ("bootstrap_failed", ClaudeResult(), self.git.snapshot(), False)

        boot = session.bootstrap(default_bootstrap_steps(self.profile))
        if not boot.ok:
            self.store.log("bootstrap failed (a required step was not confirmed)", logging.WARNING)
            return ("bootstrap_failed", ClaudeResult(), self.git.snapshot(), False)

        prompt = self._build_prompt(packet, discovery, git_before, is_first, attempt, result_file)
        watchdog = Watchdog(
            idle_timeout_seconds=self.profile.claude.idle_timeout_minutes * 60,
            hard_timeout_seconds=self.profile.claude.hard_timeout_minutes * 60,
            clock=self._clock,
        )
        state.claude_pid = session.driver.pid
        self._save(state)
        monitor = session.send_packet_prompt(
            prompt, watchdog, result_file=str(result_file) if result_file else None
        )
        result = self._parse_result(result_file, session.transcript)
        git_after = self.git.snapshot()
        return (monitor.reason, result, git_after, True)

    def _parse_result(self, result_file, transcript: str) -> ClaudeResult:
        """Prefer the result FILE Claude wrote (clean) over the garbled TUI transcript."""
        if result_file is not None:
            try:
                p = Path(result_file)
                if p.exists():
                    r = self.parser.parse(p.read_text(encoding="utf-8", errors="replace"))
                    if r.is_valid:
                        return r
            except OSError:  # pragma: no cover - defensive
                pass
        return self.parser.parse(transcript)

    def _build_prompt(self, packet, discovery, git_before, is_first, attempt, result_file=None) -> str:
        handoff = self.store.latest_handoff() if not is_first else None
        rf = str(result_file) if result_file else None
        if attempt > 1:
            return self.prompts.build_repair_prompt(
                profile=self.profile, git_state=git_before, handoff=handoff,
                repair_reason=(f"A previous attempt on {packet} did not cleanly verify "
                               "against repository truth. Verify current state carefully."),
                result_file=rf,
            )
        starting_prompt = None
        if is_first and self.profile.plan.starting_prompt_path:
            sp = self.profile.resolve(self.profile.plan.starting_prompt_path)
            if sp and sp.exists():
                starting_prompt = sp.read_text(encoding="utf-8")
        reports = ReportScanner(self.profile.resolve(self.profile.plan.reports_dir))
        return self.prompts.build_packet_prompt(
            profile=self.profile, git_state=git_before, discovery=discovery,
            handoff=handoff, starting_prompt=starting_prompt,
            recent_report_paths=reports.latest_paths(self.profile.execution.max_reports_in_prompt),
            is_first_session=is_first, result_file=rf,
        )

    # ======================================================================
    # graphify gate
    # ======================================================================
    def _maybe_graphify(self, session: ClaudeSession, packet, git_before,
                        git_after) -> Optional[GraphifyResult]:
        if not self.profile.graphify.update_after_commit:
            return None
        # The gate is BUILDER-enforced: run graphify whenever a real commit was
        # made (HEAD advanced), regardless of Claude's self-reported
        # GRAPHIFY_UPDATE_REQUIRED flag — Claude must not be able to skip it.
        commit_made = bool(git_after.head) and git_after.head != git_before.head
        if not commit_made:
            return None
        watchdog = Watchdog(
            idle_timeout_seconds=self.profile.graphify.timeout_minutes * 60,
            hard_timeout_seconds=self.profile.graphify.timeout_minutes * 60,
            clock=self._clock,
        )
        self._say(f"running graphify gate for {packet} @ {git_after.short_head} …")
        monitor = session.run_graphify(
            self.graphify.command,
            self.profile.graphify.success_patterns,
            self.profile.graphify.failure_patterns,
            watchdog,
        )
        error = None
        if monitor.reason in ("idle_timeout", "hard_timeout", "eof"):
            error = f"graphify monitor ended: {monitor.reason}"
        return self.graphify.classify(
            monitor.output, packet=packet, commit=git_after.head, error=error
        )

    def _record_graphify(self, state: RunState, gr: GraphifyResult) -> None:
        state.last_graphify_update = {
            "packet": gr.packet, "commit": gr.commit, "success": gr.success,
            "timestamp": gr.timestamp, "error": gr.error,
        }
        if gr.success:
            state.graphify_pending = None

    def _resolve_pending_graphify(self, state: RunState, git_state: GitState):
        """Re-attempt a previously-failed graphify update before any new packet.

        Returns a (StopReason, detail) tuple if the gate still fails, else None.
        """
        pend = state.graphify_pending
        if not pend:
            return None
        commit = pend.get("commit")
        packet = pend.get("packet")
        # If HEAD has moved on, the operator resolved it (or ran graphify manually);
        # clear the pending marker and proceed.
        if not git_state.is_repo or git_state.head != commit:
            self.store.log(f"clearing stale graphify-pending for {packet}@{commit} "
                           f"(HEAD is now {git_state.short_head})")
            state.graphify_pending = None
            self._save(state)
            return None
        self.store.log(f"resolving pending graphify for {packet}@{git_state.short_head}")
        gr = self._run_standalone_graphify(packet, commit)
        self._record_graphify(state, gr)
        self._save(state)
        if gr.success:
            return None
        self._write_graphify_failure(gr)
        return (StopReason.STOP_AT_GRAPHIFY_GATE, gr.error or "graphify update still failing")

    def _run_standalone_graphify(self, packet, commit) -> GraphifyResult:
        """Open a fresh session solely to run the graphify update for HEAD."""
        log_path = self.store.session_log_path(f"{packet}-graphify", 0)
        session = self.session_factory(str(log_path))
        try:
            if not session.open():
                return GraphifyResult(success=False, packet=packet, commit=commit,
                                      error="session failed to open for graphify retry")
            boot = session.bootstrap(default_bootstrap_steps(self.profile))
            if not boot.ok:
                return GraphifyResult(success=False, packet=packet, commit=commit,
                                      error="bootstrap failed for graphify retry")
            watchdog = Watchdog(
                idle_timeout_seconds=self.profile.graphify.timeout_minutes * 60,
                hard_timeout_seconds=self.profile.graphify.timeout_minutes * 60,
                clock=self._clock,
            )
            monitor = session.run_graphify(
                self.graphify.command,
                self.profile.graphify.success_patterns,
                self.profile.graphify.failure_patterns,
                watchdog,
            )
            error = None
            if monitor.reason in ("idle_timeout", "hard_timeout", "eof"):
                error = f"graphify monitor ended: {monitor.reason}"
            return self.graphify.classify(monitor.output, packet=packet, commit=commit, error=error)
        finally:
            session.close()

    # ======================================================================
    # helpers
    # ======================================================================
    def _repo_truth_success(self, packet, git_before, git_after) -> bool:
        """Accept a packet via repository truth even if the prose block was garbled."""
        if not git_after.is_repo:
            return False
        if git_after.working_tree != WorkingTree.CLEAN:
            return False
        if self.profile.execution.require_commit_after_packet:
            if not git_after.head or git_after.head == git_before.head:
                return False
        ledger_after = self._reparse_ledger()
        norm = normalize_packet_id(packet)
        ledger_done = norm in {normalize_packet_id(p) for p in ledger_after.completed_packets}
        report = ReportScanner(self.profile.resolve(self.profile.plan.reports_dir)).report_for_packet(packet)
        report_ok = report is not None
        return ledger_done or report_ok

    def _reparse_ledger(self) -> LedgerParse:
        path = self.profile.resolve(self.profile.plan.execution_ledger_path)
        return LedgerParser().parse_file(path) if path and path.exists() else LedgerParse()

    def _ledger_exists(self) -> bool:
        path = self.profile.resolve(self.profile.plan.execution_ledger_path)
        return bool(path and path.exists())

    @staticmethod
    def _tests_failed(result: ClaudeResult, verification) -> bool:
        if result.tests == TestOutcome.FAIL:
            return True
        for c in verification.checks:
            if c.name == "tests reported PASS" and not c.ok:
                return True
        return False

    def _build_handoff(self, packet, result, git_after, graphify_result, attempt) -> Handoff:
        tests = {}
        if result.tests != TestOutcome.UNKNOWN:
            tests["suite"] = result.tests.value
        graphify_state = "SKIPPED"
        if graphify_result is not None:
            graphify_state = "OK" if graphify_result.success else "FAILED"
        # authoritative next packet from the (now-updated) ledger
        ledger_after = self._reparse_ledger()
        next_pkt = ledger_after.next_authoritative_packet or result.next_authoritative_packet
        return Handoff(
            completed_packet=result.packet or packet,
            status=result.status.value if result.is_valid else PacketStatus.COMPLETE.value,
            commit=git_after.head,
            next_authoritative_packet=next_pkt,
            tests=tests,
            report=result.report,
            plan_drift=result.plan_drift,
            blockers=result.blockers,
            unresolved_risks=result.unresolved_risks,
            changed_files=self.git.last_commit_files(),
            graphify_update=graphify_state,
            working_tree=git_after.working_tree.value,
            attempt=attempt,
        )

    def _commit_success_to_state(self, state: RunState, packet, git_after, handoff) -> None:
        norm = normalize_packet_id(packet)
        if norm not in state.completed_packets:
            state.completed_packets.append(norm)
        if git_after.head:
            state.commits[norm] = git_after.head
        state.latest_report = handoff.report
        state.next_packet = handoff.next_authoritative_packet
        state.current_packet = None
        state.attempt = 0
        self._extras.blockers = handoff.blockers
        self._extras.plan_drift = handoff.plan_drift
        self._extras.unresolved_risks = handoff.unresolved_risks
        self._save(state)

    # ======================================================================
    # state / dashboard / reports
    # ======================================================================
    def _load_or_new(self, resume: bool) -> RunState:
        state = self.store.load_state() if resume else None
        if state is None:
            state = RunState(run_id=self.store.new_run_id(), project=self.profile.slug or self.profile.project.name)
            self.store.log("no prior state; starting a new run")
        else:
            # reconcile against repository truth (never blindly trust stale state)
            git_state = self.git.snapshot()
            ledger = self._reparse_ledger()
            reports = ReportScanner(self.profile.resolve(self.profile.plan.reports_dir))
            completions = ledger.completed_packets + [
                i.header_packets[0] for i in reports.scan() if i.header_packets
            ]
            notes = self.store.reconcile(
                state, completed_from_truth=completions,
                git_head=git_state.head, git_branch=git_state.branch,
                discovered_next=ledger.next_authoritative_packet,
            )
            for n in notes:
                self.store.log(f"reconcile: {n}")
        return state

    def _save(self, state: RunState) -> None:
        self.store.save_state(state)

    def _refresh_dashboard(self, state: RunState, git_state: GitState) -> None:
        self.dashboard.write(state, profile=self.profile, git_state=git_state, extras=self._extras)

    def _stop(self, state: RunState, git_state: GitState, reason: StopReason, detail: str) -> None:
        state.stop_reason = reason.value
        state.stop_detail = detail
        if reason == StopReason.PLAN_COMPLETE:
            state.status = RunStatus.COMPLETE.value
        else:
            state.status = RunStatus.STOPPED.value
        self._save(state)
        self._refresh_dashboard(state, git_state)
        self.store.log(f"STOP: {reason.value} — {detail}", logging.WARNING)
        self.progress(f"🛑 STOP: {reason.value} — {detail}")
        if reason == StopReason.PLAN_COMPLETE:
            self.notify("🎉 Plan complete", detail[:120])
        else:
            self.notify(f"🛑 Run stopped: {reason.value}", detail[:120])

    def _write_ambiguity_report(self, discovery, git_state) -> None:
        content = (
            f"# Ambiguity / no-data stop\n\n"
            f"- **Outcome:** {discovery.outcome.value}\n"
            f"- **Reason:** {discovery.ambiguity_reason}\n"
            f"- **Branch/HEAD:** {git_state.branch} / {git_state.short_head}\n\n"
            f"## Evidence\n" + ("\n".join(f"- {e}" for e in discovery.evidence) or "- none") + "\n\n"
            f"## Completed (repository truth)\n"
            + ("\n".join(f"- {p}" for p in discovery.completed_packets) or "- none") + "\n\n"
            f"## Superseded\n"
            + ("\n".join(f"- {p}" for p in discovery.superseded_packets) or "- none") + "\n\n"
            "## Recovery\n"
            "- Inspect the Execution Ledger's NEXT AUTHORITATIVE PACKET marker and the "
            "latest reports; resolve the ambiguity, then re-run `resume`.\n"
        )
        path = self.store.write_failure_report(f"ambiguity_{discovery.outcome.value}", content)
        self.store.log(f"wrote ambiguity report: {path}")

    def _write_graphify_failure(self, gr: GraphifyResult) -> None:
        content = (
            f"# STOP_AT_GRAPHIFY_GATE\n\n"
            f"- **Packet:** {gr.packet}\n- **Commit:** {gr.commit}\n"
            f"- **Error:** {gr.error}\n- **Timestamp:** {gr.timestamp}\n\n"
            f"## Graphify output (tail)\n```\n{gr.output_tail}\n```\n\n"
            f"## Recovery instructions\n{gr.recovery_instructions()}\n"
        )
        self.store.write_failure_report(f"graphify_gate_{gr.packet}", content)

    def _write_recovery_report(self, packet, attempt, decision, result, verification, git_after) -> None:
        failures = "\n".join(f"- {c.name}: {c.detail}" for c in verification.failures) or "- none"
        content = (
            f"# Packet {packet} stopped (attempt {attempt})\n\n"
            f"- **Stop reason:** {decision.stop_reason.value}\n"
            f"- **Decision:** {decision.reason}\n"
            f"- **Claude status:** {result.status.value}\n"
            f"- **Result valid:** {result.is_valid}\n"
            f"- **Working tree:** {git_after.working_tree.value}\n"
            f"- **HEAD:** {git_after.short_head}\n\n"
            f"## Verification failures\n{failures}\n\n"
            f"## Parse errors\n" + ("\n".join(f"- {e}" for e in result.parse_errors) or "- none") + "\n\n"
            f"## Blockers reported\n" + ("\n".join(f"- {b}" for b in result.blockers) or "- none") + "\n\n"
            "## Recovery\n"
            "- Review the raw session log and the verification failures above.\n"
            "- If the tree is dirty, inspect the partial work manually (the builder "
            "will not stash or discard it).\n"
            "- Resolve the issue, then re-run `resume`.\n"
        )
        self.store.write_failure_report(f"recovery_{packet}_attempt-{attempt}", content)

    @staticmethod
    def _origin_ok(actual: Optional[str], configured: str) -> bool:
        if not actual:
            return False

        def norm(u: str) -> str:
            u = u.strip().lower().removesuffix(".git")
            u = u.replace("git@github.com:", "github.com/")
            for p in ("https://", "http://", "ssh://"):
                u = u.replace(p, "")
            return u.rstrip("/")

        return norm(actual) == norm(configured)

    # ======================================================================
    # finalization (optional test deck)
    # ======================================================================
    def run_final_test_deck(self) -> GraphifyResult | ClaudeResult | None:
        """Optional finalization: generate a test deck with the completed runtime."""
        if not self.profile.finalization.create_test_deck:
            self.store.log("final test deck disabled in config; nothing to do")
            return None
        git_state = self.git.snapshot()
        prompt = self.prompts.build_final_test_deck_prompt(profile=self.profile, git_state=git_state)
        log_path = self.store.final_deck_dir / "final_test_deck_session.log"
        session = self.session_factory(str(log_path))
        if not session.open():
            self.store.log("final test deck: session failed to open", logging.ERROR)
            return None
        session.bootstrap(default_bootstrap_steps(self.profile))
        watchdog = Watchdog(
            idle_timeout_seconds=self.profile.claude.idle_timeout_minutes * 60,
            hard_timeout_seconds=self.profile.claude.hard_timeout_minutes * 60,
            clock=self._clock,
        )
        session.send_packet_prompt(prompt, watchdog)
        result = self.parser.parse(session.transcript)
        session.terminate(force=True)
        import json
        from autonomous_builder.models import to_jsonable
        (self.store.final_deck_dir / "final_test_deck_result.json").write_text(
            json.dumps(to_jsonable(result), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self.store.log(f"final test deck result: {result.status.value}")
        return result
