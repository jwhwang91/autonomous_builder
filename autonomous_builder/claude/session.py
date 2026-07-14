"""Claude Code interactive session lifecycle.

One :class:`ClaudeSession` drives exactly one fresh Claude process through:

    open (spawn + wait for readiness)
      -> bootstrap (/model, /effort, optional graphify-at-bootstrap)
      -> send packet prompt + monitor until the result sentinel / timeout / EOF
      -> (runner verifies commit) -> run graphify update gate
      -> terminate completely

Everything is expressed against the :class:`ClaudeDriver` abstraction, so the
whole lifecycle is exercisable with :class:`FakeClaudeDriver` and never needs a
live process in unit tests. Timeouts are enforced by an injected
:class:`Watchdog`; ANSI is stripped for the parsed transcript while the raw
stream is preserved to a log file.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from autonomous_builder.claude.driver import ClaudeDriver
from autonomous_builder.claude.parser import END_SENTINEL, ResultParser, strip_ansi
from autonomous_builder.config import BootstrapStep, ClaudeConfig
from autonomous_builder.models import is_packet_id
from autonomous_builder.execution.watchdog import Watchdog, WatchdogVerdict

# bracketed-paste wrappers let us insert a multi-line prompt without each newline
# submitting the input prematurely.
_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"


class MonitorReason(str):
    pass


@dataclass
class MonitorResult:
    reason: str  # sentinel | matched | failure | eof | idle_timeout | hard_timeout
    output: str = ""
    matched_pattern: Optional[str] = None


@dataclass
class BootstrapStepResult:
    send: str
    confirmed: bool
    required: bool
    note: str = ""


@dataclass
class BootstrapReport:
    ok: bool = True
    steps: list[BootstrapStepResult] = field(default_factory=list)


class ClaudeSession:
    def __init__(
        self,
        driver: ClaudeDriver,
        config: ClaudeConfig,
        *,
        raw_log_path: Optional[str | Path] = None,
        logger: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        prompt_delivery: str = "paste",  # paste | lines
    ):
        self.driver = driver
        self.config = config
        self.logger = logger or (lambda msg: None)
        self._clock = clock
        self._sleep = sleep
        self.prompt_delivery = prompt_delivery
        self.raw_log_path = Path(raw_log_path) if raw_log_path else None
        self._raw_fh = None
        self.transcript = ""          # ANSI-stripped, full session
        self.ready = False
        self._ready_res = [re.compile(p) for p in config.ready_patterns]
        self._interactive = [(re.compile(p, re.IGNORECASE), r) for p, r in config.interactive_responses.items()]
        self._responded: set[str] = set()
        # optional live-progress heartbeat: heartbeat(label, elapsed, idle_for, nbytes)
        self.heartbeat: Optional[Callable[[str, float, float, int], None]] = None
        self.heartbeat_seconds: float = 15.0
        self._parser = ResultParser()  # to content-validate result blocks

    # -- lifecycle ----------------------------------------------------------
    def open(self, startup_timeout: float = 90.0) -> bool:
        if self.raw_log_path:
            self.raw_log_path.parent.mkdir(parents=True, exist_ok=True)
            self._raw_fh = self.raw_log_path.open("a", encoding="utf-8", errors="replace")
        self.logger(f"spawning claude: {self.config.executable}")
        self.driver.start()
        self.ready = self._wait_ready(startup_timeout)
        return self.ready

    def _wait_ready(self, timeout: float) -> bool:
        deadline = self._clock() + timeout
        window = ""
        while self._clock() < deadline:
            chunk = self.driver.read_available(self.config.poll_interval_seconds)
            if chunk:
                self._absorb(chunk)
                window = (window + strip_ansi(chunk))[-4000:]
                if any(r.search(window) for r in self._ready_res):
                    self.logger("session ready")
                    return True
            if not self.driver.is_alive():
                self.logger("claude exited before readiness")
                return False
        # No explicit ready pattern seen, but the process is alive and produced
        # output — treat as ready (Claude Code UI strings drift between versions).
        alive = self.driver.is_alive()
        self.logger("readiness pattern not confirmed; proceeding" if alive else "not ready")
        return alive

    # -- bootstrap ----------------------------------------------------------
    def bootstrap(self, steps: list[BootstrapStep]) -> BootstrapReport:
        report = BootstrapReport()
        for step in steps:
            self.logger(f"bootstrap: {step.send}  ({step.description})")
            self._deliver_line(step.send, step.press_enter)
            confirmed = True
            note = ""
            if step.expect:
                mr = self._monitor(
                    until_patterns=[step.expect],
                    idle_seconds=max(step.settle_seconds * 4, 10.0),
                    hard_seconds=max(step.settle_seconds * 8, 30.0),
                    label=f"bootstrap:{step.send}",
                )
                confirmed = mr.reason in ("sentinel", "matched")
                note = mr.reason
                if not confirmed and step.required:
                    report.ok = False
            else:
                # no confirmation pattern: settle briefly and drain output
                self._drain(step.settle_seconds)
            report.steps.append(BootstrapStepResult(step.send, confirmed, step.required, note))
            if step.required and not confirmed:
                self.logger(f"bootstrap step FAILED (required): {step.send}")
                break
        return report

    # -- packet execution ---------------------------------------------------
    def send_packet_prompt(self, prompt: str, watchdog: Watchdog) -> MonitorResult:
        self.logger("delivering packet prompt")
        self._deliver_prompt(prompt)
        self.logger("prompt submitted; waiting for Claude's result block")
        # Completion is gated on a VALID result block (see _monitor): Claude Code
        # echoes the pasted prompt, and the prompt template contains the sentinels,
        # but that echoed block is the invalid template (STATUS: COMPLETE|BLOCKED|
        # FAILED, PACKET: <placeholder>) and is ignored — we wait for Claude's real,
        # parseable block.
        return self._monitor(
            until_patterns=[re.escape(END_SENTINEL)],
            watchdog=watchdog,
            require_valid_result=True,
            label="packet",
        )

    def run_graphify(self, command: str, success_patterns: list[str],
                     failure_patterns: list[str], watchdog: Watchdog) -> MonitorResult:
        self.logger(f"running graphify gate: {command}")
        self._deliver_line(command, True)
        return self._monitor(
            until_patterns=success_patterns,
            failure_patterns=failure_patterns,
            watchdog=watchdog,
            label="graphify",
        )

    def terminate(self, force: bool = False) -> None:
        self.logger("terminating claude session")
        try:
            self.driver.terminate(force=force)
        finally:
            self._close_log()

    def close(self) -> None:
        try:
            self.driver.close()
        finally:
            self._close_log()

    # -- monitor loop -------------------------------------------------------
    def _monitor(
        self,
        until_patterns: list[str],
        *,
        failure_patterns: Optional[list[str]] = None,
        watchdog: Optional[Watchdog] = None,
        idle_seconds: Optional[float] = None,
        hard_seconds: Optional[float] = None,
        require_valid_result: bool = False,
        label: str = "",
    ) -> MonitorResult:
        wd = watchdog or Watchdog(
            idle_timeout_seconds=idle_seconds or (self.config.idle_timeout_minutes * 60),
            hard_timeout_seconds=hard_seconds or (self.config.hard_timeout_minutes * 60),
            clock=self._clock,
        )
        until = [re.compile(p) for p in until_patterns]
        failure = [re.compile(p, re.IGNORECASE) for p in (failure_patterns or [])]
        collected = ""
        window = ""
        # keep enough overlap that a pattern can never straddle the truncation
        # boundary, even when a single read returns a full 64k buffer.
        keep = 16000
        last_hb = self._clock()
        while True:
            # live heartbeat so a long monitor is visibly alive, not "frozen"
            if self.heartbeat is not None:
                now = self._clock()
                if (now - last_hb) >= self.heartbeat_seconds:
                    last_hb = now
                    try:
                        self.heartbeat(label, wd.elapsed, wd.idle_for, len(collected))
                    except Exception:  # pragma: no cover - never let display break the run
                        pass
            chunk = self.driver.read_available(self.config.poll_interval_seconds)
            if chunk:
                stripped = self._absorb(chunk)
                collected += stripped
                # Search the FULL combined text (old tail + new chunk) BEFORE
                # truncating, so a sentinel inside a large single read is not lost.
                search_space = window + stripped
                window = search_space[-keep:]
                if self._is_meaningful(stripped):
                    wd.note_activity()
                # handle any known interactive prompt
                self._maybe_respond(search_space)
                # failure first (decisive)
                for fp in failure:
                    if fp.search(search_space):
                        return MonitorResult("failure", collected, fp.pattern)
                for up in until:
                    if not up.search(search_space):
                        continue
                    if up.pattern == re.escape(END_SENTINEL):
                        # Only complete on a VALID result block. Claude Code echoes
                        # the pasted prompt, whose template contains the sentinels,
                        # but that echoed block is invalid (template STATUS/PACKET
                        # placeholders) — ignore it and keep waiting for Claude's
                        # real, parseable block.
                        if require_valid_result and not self._is_real_result(search_space):
                            continue
                        reason = "sentinel"
                    else:
                        reason = "matched"
                    # drain a little trailing output for completeness
                    collected += self._drain(0.5)
                    return MonitorResult(reason, collected, up.pattern)
            if not self.driver.is_alive():
                collected += self._drain(0.3)
                return MonitorResult("eof", collected)
            verdict = wd.check()
            if verdict == WatchdogVerdict.IDLE_TIMEOUT:
                self.logger(f"[{label}] idle timeout after {wd.idle_for:.0f}s")
                return MonitorResult("idle_timeout", collected)
            if verdict == WatchdogVerdict.HARD_TIMEOUT:
                self.logger(f"[{label}] hard timeout after {wd.elapsed:.0f}s")
                return MonitorResult("hard_timeout", collected)

    def _is_real_result(self, text: str) -> bool:
        """True only for a genuine result block — not the echoed prompt template.

        The echoed template has ``STATUS: COMPLETE|BLOCKED|FAILED`` (invalid) and
        ``PACKET: <placeholder>`` (not a real id); Claude's real block parses
        cleanly with a real packet id.
        """
        r = self._parser.parse(text)
        return r.is_valid and bool(r.packet) and is_packet_id(r.packet)

    # -- helpers ------------------------------------------------------------
    def _submit(self) -> None:
        """Press Enter in the Claude Code TUI.

        Enter is a CARRIAGE RETURN (\\r) in the raw-mode TUI — pexpect.sendline's
        line feed (\\n) only inserts a newline in the input box and does NOT
        submit, which left the pasted prompt sitting un-sent.
        """
        self.driver.send_text("\r")

    def _deliver_prompt(self, prompt: str) -> None:
        if self.prompt_delivery == "paste":
            self.driver.send_text(_PASTE_START + prompt + _PASTE_END)
            self._sleep(1.0)  # let a large multi-line paste fully settle
        else:  # lines mode: send as raw text
            self.driver.send_text(prompt)
            self._sleep(0.6)
        self._submit()

    def _deliver_line(self, text: str, press_enter: bool) -> None:
        self.driver.send_text(text)
        if press_enter:
            self._sleep(0.3)
            self._submit()

    def _maybe_respond(self, window: str) -> None:
        for pattern, response in self._interactive:
            m = pattern.search(window)
            if not m:
                continue
            # Dedup on the matched text (stable across the sliding window), not a
            # window offset (which shifts as the window scrolls and would cause
            # repeated responses to the same on-screen prompt).
            key = f"{pattern.pattern}:{m.group(0).strip()[:80]}"
            if key in self._responded:
                continue
            self._responded.add(key)
            self.logger(f"interactive prompt matched {pattern.pattern!r}; sending {response!r}")
            if response in ("\r", "\n", "enter", "ENTER"):
                self._submit()
            elif len(response) == 1:
                self.driver.send_text(response)
            else:
                self.driver.send_text(response)
                self._submit()

    def _drain(self, seconds: float, max_empty: int = 3) -> str:
        """Read whatever is immediately available for up to *seconds*.

        Terminates as soon as the stream goes quiet (``max_empty`` consecutive
        empty reads) so it is bounded even under a stubbed / non-advancing clock.
        """
        end = self._clock() + seconds
        out = ""
        empties = 0
        while self._clock() < end and empties < max_empty:
            chunk = self.driver.read_available(min(0.25, self.config.poll_interval_seconds))
            if chunk:
                out += self._absorb(chunk)
                empties = 0
            else:
                empties += 1
                if not self.driver.is_alive():
                    break
        return out

    # Cap the in-memory transcript on marathon sessions. The result block is
    # always at the END, so keeping the tail is sufficient for parsing; the full
    # raw stream is still preserved to the raw log file.
    _TRANSCRIPT_CAP = 4_000_000

    def _absorb(self, chunk: str) -> str:
        """Record raw chunk to the log and return the ANSI-stripped text."""
        if self._raw_fh:
            try:
                self._raw_fh.write(chunk)
                self._raw_fh.flush()
            except Exception:  # pragma: no cover - defensive
                pass
        stripped = strip_ansi(chunk)
        self.transcript += stripped
        if len(self.transcript) > self._TRANSCRIPT_CAP:
            self.transcript = self.transcript[-self._TRANSCRIPT_CAP:]
        return stripped

    @staticmethod
    def _is_meaningful(text: str) -> bool:
        # non-whitespace content that isn't just a spinner/dot line
        s = text.strip()
        if not s:
            return False
        return bool(re.search(r"[A-Za-z0-9]", s))

    def _close_log(self) -> None:
        if self._raw_fh:
            try:
                self._raw_fh.close()
            except Exception:  # pragma: no cover
                pass
            self._raw_fh = None
