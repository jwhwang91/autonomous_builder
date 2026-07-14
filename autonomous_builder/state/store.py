"""Durable state store: run state, handoffs, logs, and failure/recovery reports.

Layout under the configured data directory (default ``runtime_data/``)::

    logs/
      builder.log
      sessions/<ts>_<packet>_attempt-N.log     raw Claude session log
    results/<ts>_<packet>_attempt-N.json        parsed result block
    handoffs/<packet>.json + <packet>.md        machine + human handoff
    state/<slug>.json                           resumable RunState
    dashboard/dashboard.md + dashboard.json     live dashboard
    failures/<ts>_<name>.md                     timeout / failure / recovery / ambiguity
    final_test_deck/                            finalization artifacts
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from autonomous_builder.config import ProjectProfile
from autonomous_builder.models import (
    Handoff,
    RunState,
    RunStatus,
    to_jsonable,
    utcnow_iso,
)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(name)) or "x"


class StateStore:
    def __init__(self, profile: ProjectProfile):
        self.profile = profile
        self.slug = profile.slug or _safe(profile.project.name)
        self.root = profile.data_dir
        self.logs_dir = self.root / "logs"
        self.sessions_dir = self.logs_dir / "sessions"
        self.results_dir = self.root / "results"
        self.handoffs_dir = self.root / "handoffs"
        self.state_dir = self.root / "state"
        self.dashboard_dir = self.root / "dashboard"
        self.failures_dir = self.root / "failures"
        self.final_deck_dir = self.root / "final_test_deck"
        for d in (self.logs_dir, self.sessions_dir, self.results_dir, self.handoffs_dir,
                  self.state_dir, self.dashboard_dir, self.failures_dir, self.final_deck_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._logger: Optional[logging.Logger] = None

    # -- logging ------------------------------------------------------------
    @property
    def state_path(self) -> Path:
        return self.state_dir / f"{self.slug}.json"

    @property
    def stop_file(self) -> Path:
        return self.state_dir / f"{self.slug}.stop"

    def request_stop(self, reason: str = "user stop") -> Path:
        self.stop_file.write_text(f"{utcnow_iso()} {reason}", encoding="utf-8")
        return self.stop_file

    def stop_requested(self) -> bool:
        return self.stop_file.exists()

    def clear_stop(self) -> None:
        try:
            self.stop_file.unlink()
        except FileNotFoundError:
            pass

    def logger(self) -> logging.Logger:
        if self._logger is not None:
            return self._logger
        # Key the logger name on the resolved data dir so distinct runs (and tests
        # with distinct temp dirs) each get their own FileHandler rather than
        # sharing the first-created one from the global logger registry.
        key = str(self.root.resolve())
        logger = logging.getLogger(f"autonomous_builder.{self.slug}.{abs(hash(key))}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        log_file = self.logs_dir / "builder.log"
        have = any(
            isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == str(log_file)
            for h in logger.handlers
        )
        if not have:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
            logger.addHandler(fh)
        self._logger = logger
        return logger

    def log(self, message: str, level: int = logging.INFO) -> None:
        self.logger().log(level, message)

    # -- run state ----------------------------------------------------------
    def new_run_id(self) -> str:
        return f"run-{_ts()}"

    def load_state(self) -> Optional[RunState]:
        if not self.state_path.exists():
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return RunState.from_dict(data)
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            self.log(f"failed to load state ({exc}); starting fresh", logging.WARNING)
            return None

    def save_state(self, state: RunState) -> None:
        state.touch()
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(to_jsonable(state), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    # -- per-attempt artefacts ---------------------------------------------
    def session_log_path(self, packet: str, attempt: int) -> Path:
        return self.sessions_dir / f"{_ts()}_{_safe(packet)}_attempt-{attempt}.log"

    def write_result_json(self, packet: str, attempt: int, result) -> Path:
        path = self.results_dir / f"{_ts()}_{_safe(packet)}_attempt-{attempt}.json"
        path.write_text(json.dumps(to_jsonable(result), indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    # -- handoffs -----------------------------------------------------------
    def write_handoff(self, handoff: Handoff) -> tuple[Path, Path]:
        base = _safe(handoff.completed_packet or "unknown")
        json_path = self.handoffs_dir / f"{base}.json"
        md_path = self.handoffs_dir / f"{base}.md"
        json_path.write_text(json.dumps(to_jsonable(handoff), indent=2, ensure_ascii=False), encoding="utf-8")
        md_path.write_text(self._handoff_md(handoff), encoding="utf-8")
        return json_path, md_path

    @staticmethod
    def _handoff_md(h: Handoff) -> str:
        def block(items):
            return "\n".join(f"- {i}" for i in items) if items else "- none"
        tests = "\n".join(f"- {k}: {v}" for k, v in h.tests.items()) or "- unknown"
        return (
            f"# Handoff — {h.completed_packet or 'UNKNOWN'}\n\n"
            f"- **Status:** {h.status}\n"
            f"- **Commit:** {h.commit or 'NONE'}\n"
            f"- **Next authoritative packet:** {h.next_authoritative_packet or 'NONE'}\n"
            f"- **Working tree:** {h.working_tree}\n"
            f"- **Graphify update:** {h.graphify_update}\n"
            f"- **Report:** {h.report or 'NONE'}\n"
            f"- **Attempt:** {h.attempt}\n"
            f"- **Timestamp:** {h.timestamp}\n\n"
            f"## Tests\n{tests}\n\n"
            f"## Blockers\n{block(h.blockers)}\n\n"
            f"## Plan drift\n{block(h.plan_drift)}\n\n"
            f"## Unresolved risks\n{block(h.unresolved_risks)}\n\n"
            f"## Changed files\n{block(h.changed_files)}\n"
        )

    def latest_handoff(self) -> Optional[Handoff]:
        jsons = sorted(self.handoffs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not jsons:
            return None
        try:
            data = json.loads(jsons[0].read_text(encoding="utf-8"))
            return Handoff(**{k: v for k, v in data.items() if k in Handoff.__dataclass_fields__})
        except Exception:  # pragma: no cover - defensive
            return None

    # -- failure / recovery reports ----------------------------------------
    def write_failure_report(self, name: str, content: str) -> Path:
        path = self.failures_dir / f"{_ts()}_{_safe(name)}.md"
        path.write_text(content, encoding="utf-8")
        return path

    # -- reconciliation -----------------------------------------------------
    def reconcile(self, state: RunState, *, completed_from_truth: list[str],
                  git_head: Optional[str], git_branch: Optional[str],
                  discovered_next: Optional[str]) -> list[str]:
        """Reconcile stored state against repository truth; return notes.

        Never blindly trusts stale local state: repository-derived completions and
        the freshly discovered next packet win. Only refreshes/annotates — the
        runner performs authoritative discovery each loop.
        """
        notes: list[str] = []
        # union completed packets (truth-derived first)
        merged = []
        seen = set()
        for pk in list(completed_from_truth) + list(state.completed_packets):
            if pk and pk not in seen:
                seen.add(pk)
                merged.append(pk)
        if merged != state.completed_packets:
            notes.append(f"completed packets refreshed from repository truth: {merged}")
            state.completed_packets = merged
        if discovered_next and state.next_packet and discovered_next != state.next_packet:
            notes.append(
                f"stored next packet {state.next_packet} differs from repository "
                f"truth {discovered_next}; trusting repository truth"
            )
        if discovered_next:
            state.next_packet = discovered_next
        if git_head:
            notes.append(f"target HEAD at resume: {git_head[:12]}")
        return notes
