"""Live dashboard: ``dashboard.md`` (morning-readable) and ``dashboard.json``.

Written after every important transition so an operator can inspect overnight
progress at a glance.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from autonomous_builder.config import ProjectProfile
from autonomous_builder.models import GitState, RunState, to_jsonable


def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _fmt_elapsed(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


@dataclass
class DashboardExtras:
    blockers: list[str] = field(default_factory=list)
    plan_drift: list[str] = field(default_factory=list)
    unresolved_risks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    next_packet_source: Optional[str] = None
    disagreements: list[str] = field(default_factory=list)


class DashboardWriter:
    def __init__(self, dashboard_dir: str | Path):
        self.dir = Path(dashboard_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        state: RunState,
        *,
        profile: ProjectProfile,
        git_state: Optional[GitState] = None,
        extras: Optional[DashboardExtras] = None,
        elapsed_seconds: Optional[float] = None,
    ) -> tuple[Path, Path]:
        extras = extras or DashboardExtras()
        if elapsed_seconds is None:
            start = _parse_iso(state.started_at)
            now = datetime.now(timezone.utc)
            elapsed_seconds = (now - start).total_seconds() if start else 0.0

        data = self._data(state, profile, git_state, extras, elapsed_seconds)
        md = self._markdown(state, profile, git_state, extras, elapsed_seconds)

        json_path = self.dir / "dashboard.json"
        md_path = self.dir / "dashboard.md"
        json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        md_path.write_text(md, encoding="utf-8")
        return md_path, json_path

    # -- json ---------------------------------------------------------------
    def _data(self, state, profile, git_state, extras, elapsed) -> dict:
        retries = sum(1 for h in state.packet_history if h.attempt > 1)
        failures = [h for h in state.packet_history if (h.stop_reason or "NONE") not in ("NONE",)]
        return {
            "run_id": state.run_id,
            "run_status": state.status,
            "target_project": profile.project.name,
            "project_root": str(profile.resolve(profile.project.root_dir) or profile.project.root_dir),
            "git_branch": git_state.branch if git_state else None,
            "current_head": git_state.head if git_state else None,
            "working_tree": git_state.working_tree.value if git_state else "UNKNOWN",
            "elapsed": _fmt_elapsed(elapsed),
            "elapsed_seconds": int(elapsed),
            "started_at": state.started_at,
            "updated_at": state.updated_at,
            "current_packet": state.current_packet,
            "attempt": state.attempt,
            "next_authoritative_packet": state.next_packet,
            "next_packet_source": extras.next_packet_source,
            "completed_packets": state.completed_packets,
            "commits": state.commits,
            "retries": retries,
            "failures": [f"{h.packet} attempt-{h.attempt}: {h.stop_reason}" for h in failures],
            "plan_drift": extras.plan_drift,
            "blockers": extras.blockers,
            "unresolved_risks": extras.unresolved_risks,
            "disagreements": extras.disagreements,
            "last_graphify_update": state.last_graphify_update,
            "last_session_log": state.last_session_log,
            "plan_complete": state.plan_complete,
            "stop_reason": state.stop_reason,
            "stop_detail": state.stop_detail,
            "packet_history": to_jsonable(state.packet_history),
        }

    # -- markdown -----------------------------------------------------------
    def _markdown(self, state, profile, git_state, extras, elapsed) -> str:
        def bullets(items, empty="_none_"):
            items = [i for i in items if i]
            return "\n".join(f"- {i}" for i in items) if items else empty

        retries = sum(1 for h in state.packet_history if h.attempt > 1)
        history_rows = "\n".join(
            f"| {h.packet} | {h.attempt} | {h.status} | {(h.commit or '')[:8]} | "
            f"{'ok' if h.verification_ok else ('—' if h.verification_ok is None else 'FAIL')} | "
            f"{h.stop_reason} |"
            for h in state.packet_history
        ) or "| _none_ | | | | | |"

        commits = "\n".join(f"- **{k}**: {v}" for k, v in state.commits.items()) or "_none_"
        graphify = state.last_graphify_update or {}
        graphify_line = (
            f"{graphify.get('packet', '?')} @ {(graphify.get('commit') or '')[:8]} — "
            f"{'OK' if graphify.get('success') else 'FAILED'} ({graphify.get('timestamp', '')})"
            if graphify else "_none yet_"
        )

        return f"""# Autonomous Builder — Dashboard

**Run:** `{state.run_id}`  ·  **Status:** `{state.status}`  ·  **Updated:** {state.updated_at}

## Target
- **Project:** {profile.project.name}
- **Root:** `{profile.resolve(profile.project.root_dir) or profile.project.root_dir}`
- **Branch:** `{git_state.branch if git_state else '?'}`
- **HEAD:** `{git_state.short_head if git_state else '?'}`
- **Working tree:** `{git_state.working_tree.value if git_state else 'UNKNOWN'}`

## Progress
- **Elapsed:** {_fmt_elapsed(elapsed)}
- **Current packet:** `{state.current_packet or '—'}` (attempt {state.attempt})
- **Next authoritative packet:** `{state.next_packet or '—'}`  ({extras.next_packet_source or 'n/a'})
- **Completed packets:** {', '.join(state.completed_packets) or '_none_'}
- **Retries:** {retries}
- **Plan complete:** {'YES' if state.plan_complete else 'NO'}
- **Stop reason:** `{state.stop_reason}` {('— ' + state.stop_detail) if state.stop_detail else ''}

## Commits
{commits}

## Last Graphify update
{graphify_line}

## Blockers
{bullets(extras.blockers)}

## Plan drift
{bullets(extras.plan_drift)}

## Unresolved risks
{bullets(extras.unresolved_risks)}

## Disagreements (repository truth vs prose)
{bullets(extras.disagreements)}

## Packet history
| Packet | Attempt | Status | Commit | Verified | Stop reason |
|---|---|---|---|---|---|
{history_rows}

## Logs
- **Last Claude session log:** `{state.last_session_log or '—'}`
- **Builder log:** `{(profile.data_dir / 'logs' / 'builder.log')}`
"""
