"""Completion / audit / repair report scanning.

Reports are the third authority level (after live git and the ledger). This
scanner classifies every file in the reports directory, extracts the packet ids
each report concerns, any next-packet hints, and any PASS/FAIL verdicts — all
without hardcoding a project's filename convention (it matches on both filename
and report *content*, since e.g. ``runtime-phase-C-report.md`` concerns RT-C).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from autonomous_builder.models import (
    find_packet_ids,
    first_packet_id_after,
    normalize_packet_id,
)

_MAX_BYTES = 400_000

# A verdict is only read from a line that IS a verdict declaration — the keyword
# sits at (or near) the start of the line, after optional markdown. This avoids
# mid-paragraph false hits ("7 bugs fixed", "fail-open jury") in prose-heavy
# adversarial-review reports.
_VERDICT_LEAD = re.compile(
    r"^[>*_#`\s\-]*\**\s*"
    r"(?:verdict|test\s+status|status|acceptance|audit\s+verdict|gate|final\s+result|result|outcome|blockers)\b",
    re.IGNORECASE,
)
_LINE_PASS = re.compile(r"\b(pass(?:ed|es)?|green|approved|accepted)\b", re.IGNORECASE)
_LINE_FAIL = re.compile(r"\b(fail(?:ed|s)?|red|rejected|blocked)\b", re.IGNORECASE)
_NEXT_HINT = re.compile(
    r"next\s+authoritative\s+packet\s*(?:is|was|:|\-)?\s*(.+)", re.IGNORECASE
)


@dataclass
class ReportInfo:
    path: str
    name: str
    mtime: float
    kind: str  # completion | audit | acceptance | repair | timeout | generic
    title: str = ""
    header_packets: list[str] = field(default_factory=list)
    all_packets: list[str] = field(default_factory=list)
    verdict: Optional[str] = None  # PASS | FAIL | None
    next_hints: list[str] = field(default_factory=list)
    size: int = 0


def _classify_kind(name: str) -> str:
    low = name.lower()
    if "timeout" in low:
        return "timeout"
    if "repair" in low:
        return "repair"
    if "acceptance" in low or "final" in low:
        return "acceptance"
    if "audit" in low:
        return "audit"
    if "report" in low or "phase" in low:
        return "completion"
    return "generic"


def _filename_matches_packet(name: str, packet: str) -> bool:
    """Fuzzy filename<->packet match, dash/prime insensitive."""
    n = re.sub(r"[^a-z0-9]", "", name.lower())
    p = re.sub(r"[^a-z0-9]", "", packet.lower())
    return bool(p) and p in n


class ReportScanner:
    def __init__(self, reports_dir: str | Path):
        self.reports_dir = Path(reports_dir).expanduser()

    def scan(self) -> list[ReportInfo]:
        if not self.reports_dir.exists():
            return []
        infos: list[ReportInfo] = []
        for path in sorted(self.reports_dir.glob("*.md")):
            try:
                stat = path.stat()
                text = path.read_text(encoding="utf-8", errors="replace")[:_MAX_BYTES]
            except OSError:  # pragma: no cover - defensive
                continue
            lines = text.splitlines()
            title = next((l.lstrip("# ").strip() for l in lines if l.startswith("#")), path.stem)
            header = "\n".join(lines[:40])
            info = ReportInfo(
                path=str(path),
                name=path.name,
                mtime=stat.st_mtime,
                kind=_classify_kind(path.name),
                title=title,
                header_packets=find_packet_ids(title + "\n" + header),
                all_packets=find_packet_ids(text),
                size=stat.st_size,
            )
            info.verdict = self._verdict(text)
            for m in _NEXT_HINT.finditer(text):
                pid = first_packet_id_after(m.group(1), 0)
                if pid:
                    info.next_hints.append(pid)
            infos.append(info)
        return infos

    @staticmethod
    def _verdict(text: str) -> Optional[str]:
        """Read a verdict only from verdict-declaration lines.

        A ``Blockers: none`` line counts as PASS-leaning; a ``verdict: FAIL`` /
        ``status: RED`` line is decisive FAIL. Any FAIL verdict line wins over
        PASS ones. Prose that merely mentions pass/fail is ignored.
        """
        saw_pass = False
        for raw in text.splitlines():
            if not _VERDICT_LEAD.match(raw):
                continue
            low = raw.lower()
            fail = bool(_LINE_FAIL.search(low))
            passed = bool(_LINE_PASS.search(low))
            # "blockers: none" / "no blockers" is a pass-leaning declaration
            if re.search(r"blockers?\s*[:\-]?\s*\**\s*(none|0|nil)\b", low) or "no blocker" in low:
                saw_pass = True
                continue
            if fail and not passed:
                return "FAIL"
            if passed and not fail:
                saw_pass = True
        return "PASS" if saw_pass else None

    # -- convenience queries -----------------------------------------------
    def latest(self, n: int = 6) -> list[ReportInfo]:
        return sorted(self.scan(), key=lambda r: r.mtime, reverse=True)[:n]

    def latest_paths(self, n: int = 6) -> list[str]:
        return [r.path for r in self.latest(n)]

    def report_for_packet(self, packet: str) -> Optional[ReportInfo]:
        packet = normalize_packet_id(packet)
        if not packet:
            return None
        scored: list[tuple[int, float, ReportInfo]] = []
        for info in self.scan():
            score = 0
            if _filename_matches_packet(info.name, packet):
                score = 3
            elif info.header_packets and info.header_packets[0] == packet:
                score = 2  # the report's *title/subject* is this packet
            elif packet in info.header_packets:
                score = 1  # merely mentioned in the header (e.g. as a dependency)
            if score:
                scored.append((score, info.mtime, info))
        if not scored:
            return None
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return scored[0][2]

    def audits(self) -> list[ReportInfo]:
        return [r for r in self.scan() if r.kind in ("audit", "acceptance")]

    def repairs(self) -> list[ReportInfo]:
        return [r for r in self.scan() if r.kind == "repair"]

    def next_packet_hints(self, n: int = 4) -> list[tuple[str, str]]:
        """(packet, report_path) hints from the *n* most recent reports."""
        hints: list[tuple[str, str]] = []
        for info in self.latest(n):
            for pid in info.next_hints:
                hints.append((pid, info.path))
        return hints
