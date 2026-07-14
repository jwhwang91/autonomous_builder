"""Claude Code process driver abstraction.

The rest of the system depends on the abstract :class:`ClaudeDriver`, never on a
live pexpect object directly. Two concrete drivers exist:

* :class:`PexpectClaudeDriver` — the real driver that spawns and supervises an
  interactive ``claude`` process via pexpect (fully implemented, not stubbed).
* :class:`FakeClaudeDriver` — a deterministic, scriptable driver for unit tests
  so the suite never needs a live Claude session.

The driver interface is intentionally thin (start / send / read / lifecycle);
the higher-level expect/monitor loop and watchdog live in ``session.py`` so they
can be exercised with the fake driver.
"""
from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional, Union


@dataclass
class DriverConfig:
    command: str
    args: list[str] = field(default_factory=list)
    cwd: Optional[str] = None
    env: Optional[dict] = None
    dimensions: tuple[int, int] = (50, 200)  # rows, cols — wide to reduce wrapping
    encoding: str = "utf-8"


class ClaudeDriver(ABC):
    """Abstract interactive process driver."""

    @abstractmethod
    def start(self) -> None:
        ...

    @abstractmethod
    def send_line(self, text: str) -> None:
        """Send *text* followed by Enter."""

    @abstractmethod
    def send_text(self, text: str) -> None:
        """Send raw text with no trailing newline."""

    @abstractmethod
    def send_control(self, char: str) -> None:
        """Send a control character, e.g. 'c' for Ctrl-C."""

    @abstractmethod
    def read_available(self, timeout: float) -> str:
        """Return newly available output (up to ~64k). May return '' on idle.

        Blocks at most *timeout* seconds waiting for the first byte.
        """

    @abstractmethod
    def is_alive(self) -> bool:
        ...

    @property
    @abstractmethod
    def pid(self) -> Optional[int]:
        ...

    @abstractmethod
    def terminate(self, force: bool = False) -> None:
        ...

    @abstractmethod
    def close(self) -> None:
        ...


# ---------------------------------------------------------------------------
# Live driver (pexpect)
# ---------------------------------------------------------------------------


class PexpectClaudeDriver(ClaudeDriver):
    def __init__(self, config: DriverConfig):
        self.config = config
        self._child = None
        self._eof = False

    def start(self) -> None:
        import pexpect  # imported lazily so tests/fakes need not have a TTY

        self._child = pexpect.spawn(
            self.config.command,
            args=list(self.config.args),
            cwd=self.config.cwd,
            env=self.config.env,
            encoding=self.config.encoding,
            codec_errors="replace",
            dimensions=self.config.dimensions,
            timeout=None,  # we manage timeouts ourselves in the monitor loop
            echo=False,
        )
        self._eof = False

    def send_line(self, text: str) -> None:
        self._require().sendline(text)

    def send_text(self, text: str) -> None:
        self._require().send(text)

    def send_control(self, char: str) -> None:
        self._require().sendcontrol(char)

    def read_available(self, timeout: float) -> str:
        import pexpect

        child = self._require()
        try:
            data = child.read_nonblocking(size=65536, timeout=timeout)
            return data or ""
        except pexpect.TIMEOUT:
            return ""
        except pexpect.EOF:
            self._eof = True
            return ""
        except OSError:
            # PTY closed underneath us.
            self._eof = True
            return ""

    def is_alive(self) -> bool:
        if self._child is None or self._eof:
            return False
        try:
            return bool(self._child.isalive())
        except Exception:  # pragma: no cover - defensive
            return False

    @property
    def pid(self) -> Optional[int]:
        return getattr(self._child, "pid", None)

    def terminate(self, force: bool = False) -> None:
        """Terminate the Claude process and its child tree.

        Graceful first (Ctrl-C, then pexpect terminate); escalates to SIGKILL and
        a psutil tree-kill only when *force* is requested or graceful failed. A
        finished session must never linger, so we always reap the process tree.

        The descendant list is captured BEFORE we kill the parent — once the
        parent dies, its children reparent (to init) and can no longer be found
        via ``parent.children()``, which would let orphaned children survive.
        """
        if self._child is None:
            return
        pid = self.pid
        descendants = self._collect_descendants(pid)  # snapshot before killing
        try:
            if self._child.isalive():
                try:
                    self._child.sendcontrol("c")
                    time.sleep(0.3)
                    self._child.sendcontrol("c")
                    time.sleep(0.3)
                except Exception:
                    pass
                try:
                    self._child.terminate(force=False)
                except Exception:
                    pass
        except Exception:
            pass
        # escalate if still alive
        try:
            if self._child.isalive():
                self._child.terminate(force=True)
        except Exception:
            pass
        # reap the pre-captured tree (parent + all descendants)
        self._kill_pids([pid] + descendants)

    @staticmethod
    def _collect_descendants(pid: Optional[int]) -> list[int]:
        if not pid:
            return []
        try:
            import psutil
            return [p.pid for p in psutil.Process(pid).children(recursive=True)]
        except Exception:  # pragma: no cover - psutil optional / process gone
            return []

    @staticmethod
    def _kill_pids(pids: list[Optional[int]]) -> None:
        try:
            import psutil
        except Exception:  # pragma: no cover - psutil optional at runtime
            return
        procs = []
        for pid in pids:
            if not pid:
                continue
            try:
                procs.append(psutil.Process(pid))
            except psutil.NoSuchProcess:
                pass
        for p in procs:
            try:
                p.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(procs, timeout=3)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass

    def close(self) -> None:
        try:
            self.terminate(force=True)
        finally:
            if self._child is not None:
                try:
                    self._child.close(force=True)
                except Exception:  # pragma: no cover - defensive
                    pass

    def _require(self):
        if self._child is None:
            raise RuntimeError("driver not started; call start() first")
        return self._child


# ---------------------------------------------------------------------------
# Fake driver (tests)
# ---------------------------------------------------------------------------

Responder = tuple[str, Union[str, Callable[[str], Optional[str]]]]


class FakeClaudeDriver(ClaudeDriver):
    """Deterministic, scriptable driver for tests.

    *responders* is a list of ``(pattern, output)`` pairs. Whenever a sent line
    matches ``pattern`` (regex, searched), ``output`` is queued for the next
    reads (a callable receives the sent line and returns text or None). This lets
    a test say "when the packet prompt is sent, emit the result block" or "when
    /graphify is sent, emit graphify success".
    """

    def __init__(
        self,
        *,
        responders: Optional[list[Responder]] = None,
        initial_output: str = "",
        eof_after_empty_reads: Optional[int] = None,
        start_alive: bool = True,
    ):
        self.responders: list[Responder] = list(responders or [])
        self._out: deque[str] = deque()
        if initial_output:
            self._out.append(initial_output)
        self.sent_lines: list[str] = []
        self.sent_texts: list[str] = []
        self.sent_controls: list[str] = []
        self._alive = start_alive
        self.started = False
        self.terminated = False
        self.closed = False
        self._pid = 4242
        self._empty_reads = 0
        self._eof_after = eof_after_empty_reads

    # -- scripting helpers --------------------------------------------------
    def queue_output(self, text: str) -> None:
        self._out.append(text)

    def add_responder(self, pattern: str, output: Union[str, Callable[[str], Optional[str]]]) -> None:
        self.responders.append((pattern, output))

    def set_alive(self, alive: bool) -> None:
        self._alive = alive

    # -- driver interface ---------------------------------------------------
    def start(self) -> None:
        self.started = True
        self._alive = True

    def send_line(self, text: str) -> None:
        self.sent_lines.append(text)
        self._fire_responders(text)

    def send_text(self, text: str) -> None:
        self.sent_texts.append(text)
        self._fire_responders(text)

    def send_control(self, char: str) -> None:
        self.sent_controls.append(char)

    def _fire_responders(self, text: str) -> None:
        for pattern, output in self.responders:
            if re.search(pattern, text):
                value = output(text) if callable(output) else output
                if value:
                    self._out.append(value)

    def read_available(self, timeout: float) -> str:
        if self._out:
            self._empty_reads = 0
            return self._out.popleft()
        self._empty_reads += 1
        if self._eof_after is not None and self._empty_reads >= self._eof_after:
            self._alive = False
        return ""

    def is_alive(self) -> bool:
        return self._alive

    @property
    def pid(self) -> Optional[int]:
        return self._pid if self.started else None

    def terminate(self, force: bool = False) -> None:
        self.terminated = True
        self._alive = False

    def close(self) -> None:
        self.closed = True
        self._alive = False
