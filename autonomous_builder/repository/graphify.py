"""Graphify update gate.

After every verified packet commit, the builder runs ``/graphify . --update`` in
the target project (as an interactive Claude command) and confirms it succeeded
BEFORE closing the session and moving on. If it fails, the run stops at the
graphify gate (STOP_AT_GRAPHIFY_GATE) with a recovery record.

This module holds only the config-driven *detection* logic and the result
record; the actual command send/monitor is performed by the Claude session.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from autonomous_builder.config import GraphifyConfig
from autonomous_builder.models import utcnow_iso


@dataclass
class GraphifyResult:
    success: bool
    packet: Optional[str] = None
    commit: Optional[str] = None
    matched_success: list[str] = field(default_factory=list)
    matched_failure: list[str] = field(default_factory=list)
    error: Optional[str] = None
    output_tail: str = ""
    timestamp: str = field(default_factory=utcnow_iso)

    def recovery_instructions(self) -> str:
        return (
            "Graphify update did not confirm success.\n"
            "1. Open the target project and run `/graphify . --update` manually in "
            "an interactive Claude session.\n"
            "2. Confirm graphify-out/graph.json was written and GRAPH_REPORT.md updated.\n"
            "3. If graphify reports 'refused to shrink', investigate deleted files or "
            "re-run a full build.\n"
            "4. Once graphify succeeds, resume the builder — it will re-verify repo "
            "truth and continue from the next authoritative packet."
        )


class GraphifyGate:
    def __init__(self, config: GraphifyConfig):
        self.config = config
        self._success = [re.compile(p, re.IGNORECASE) for p in config.success_patterns]
        self._failure = [re.compile(p, re.IGNORECASE) for p in config.failure_patterns]

    @property
    def command(self) -> str:
        return self.config.command

    def classify(
        self,
        output: str,
        *,
        packet: Optional[str] = None,
        commit: Optional[str] = None,
        error: Optional[str] = None,
    ) -> GraphifyResult:
        """Decide whether a graphify run succeeded from its captured output."""
        matched_success = [p.pattern for p in self._success if p.search(output or "")]
        matched_failure = [p.pattern for p in self._failure if p.search(output or "")]
        tail = "\n".join((output or "").splitlines()[-25:])

        # A failure pattern is decisive; otherwise require a success signal.
        if matched_failure:
            success = False
        elif error:
            success = False
        elif matched_success:
            success = True
        else:
            # No clear signal at all -> treat as failure (safe default: the gate
            # must positively confirm success before proceeding).
            success = False
            error = error or "no graphify success signal detected in output"

        return GraphifyResult(
            success=success,
            packet=packet,
            commit=commit,
            matched_success=matched_success,
            matched_failure=matched_failure,
            error=None if success else (error or "graphify failure pattern matched"),
            output_tail=tail,
        )
