"""Execution Ledger parser.

The ledger is the second-highest authority (after live repo/git state) for
determining the current authoritative packet. This parser recognises, in order
of strength:

1. Explicit ``NEXT AUTHORITATIVE PACKET: <id>`` colon markers (canonical pointer).
2. Dated ``> **UPDATE (YYYY-MM-DD, latest):** … the next authoritative packet is
   **<id>**`` blocks — the newest wins.
3. Bare inline ``next authoritative packet is <id>`` prose.

It also extracts packet statuses (from the status table and prose), the
superseded-packets section, repair/split packets, and plan-completion markers.

It is deliberately shape-based and family-agnostic: it never hardcodes a packet
sequence and copes with evolved ids (RT-D → RT-D1 → GR1-REPAIR-A).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from autonomous_builder.models import (
    PacketStatus,
    find_packet_ids,
    first_packet_id_after,
    is_packet_id,
    normalize_packet_id,
)

# --- regexes ---------------------------------------------------------------

_COLON_MARKER = re.compile(
    r"NEXT\s+AUTHORITATIVE\s+PACKET\s*[:\-–—]\s*(.+)", re.IGNORECASE
)
_INLINE_NEXT = re.compile(
    r"next\s+authoritative\s+packet\s+(?:is|was|will\s+be|:)\s*(.+)", re.IGNORECASE
)
_UPDATE_HEADER = re.compile(
    r"UPDATE\s*\((\d{4}-\d{2}-\d{2})?(?:\s*,\s*([A-Za-z]+))?\s*\)", re.IGNORECASE
)
_DATE_RE = re.compile(r"\((\d{4}-\d{2}-\d{2})(?:\s*,\s*([A-Za-z]+))?\s*\)")
_SUPERSEDED_INLINE = re.compile(
    r"([A-Z][A-Z0-9′ʹ'’\-]*)\s+(?:is\s+)?(?:SUPERSEDED|superseded)\s+by\s+"
    r"\*{0,2}([A-Z][A-Z0-9′ʹ'’\-]*)",
)
_SUPERSEDES = re.compile(
    r"supersedes?\s+\*{0,2}([A-Z][A-Z0-9′ʹ'’\-]*)",
)

# Recency words used in "(date, word)" tags on UPDATE blocks.
_RECENCY_RANK = {
    "latest": 100, "newest": 100, "current": 90, "later": 50, "recent": 60,
    "now": 40, "": 10, "prior": -10, "previous": -10, "earlier": -20,
    "old": -30, "earliest": -30, "original": -40, "first": -40,
}

# Phrases that indicate the whole plan / a phase is complete.
_PLAN_COMPLETE_PHRASES = [
    "plan complete", "plan is complete", "entire plan complete",
    "all packets complete", "all packets are complete",
    "runtime complete", "runtime is complete", "implementation complete",
    "no remaining authoritative", "no further authoritative",
    "completion audit ... pass", "runtime completion audit",
]

_COMPLETE_WORDS = ("complete", "done", "closed", "shipped", "landed")


@dataclass
class MarkerCandidate:
    packet: str
    kind: str            # explicit_colon | update_next | inline_next
    line_no: int
    date: Optional[str] = None
    recency: int = 10
    doc_order: int = 0
    context: str = ""

    def authority_key(self) -> tuple:
        """The MEANINGFUL authority dimensions (kind, date, recency).

        Excludes doc_order: two markers that tie here but name different packets
        are genuinely ambiguous — document position alone must not silently pick
        a winner.
        """
        kind_rank = {"explicit_colon": 3, "update_next": 2, "inline_next": 1}[self.kind]
        date_key = self.date or "0000-00-00"
        return (kind_rank, date_key, self.recency)

    def score_key(self) -> tuple:
        # full ordering: authority first, doc_order only as a last-resort tiebreak
        return self.authority_key() + (self.doc_order,)


@dataclass
class LedgerParse:
    source_path: Optional[str] = None
    next_authoritative_packet: Optional[str] = None
    next_source: Optional[str] = None
    next_candidates: list[MarkerCandidate] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    completed_packets: list[str] = field(default_factory=list)
    blocked_packets: list[str] = field(default_factory=list)
    failed_packets: list[str] = field(default_factory=list)
    superseded: dict[str, str] = field(default_factory=dict)  # id -> superseded_by
    repair_packets: list[str] = field(default_factory=list)
    split_packets: list[str] = field(default_factory=list)
    all_packets: list[str] = field(default_factory=list)
    plan_complete: bool = False
    plan_complete_evidence: list[str] = field(default_factory=list)
    ambiguous: bool = False
    ambiguity_reason: Optional[str] = None

    def status_of(self, packet: str) -> PacketStatus:
        s = self.statuses.get(normalize_packet_id(packet))
        return PacketStatus(s) if s in PacketStatus.__members__.values() else PacketStatus.UNKNOWN


def _extract_first_id(tail: str) -> Optional[str]:
    """Extract the first packet id from marker tail text like '**RT-D** — ...'."""
    pid = first_packet_id_after(tail, 0)
    if pid:
        # guard against markers that say NONE/COMPLETE explicitly
        head = tail.strip().split()[0] if tail.strip() else ""
        head_up = normalize_packet_id(head)
        if head_up in {"NONE", "COMPLETE", "DONE"}:
            return None
    return pid


def _marker_says_complete(tail: str) -> bool:
    head = tail.strip().lstrip("*`~ ").split()[0] if tail.strip() else ""
    return normalize_packet_id(head) in {"NONE", "COMPLETE", "DONE"}


class LedgerParser:
    """Parse an Execution Ledger into a :class:`LedgerParse`."""

    def parse_file(self, path: str | Path) -> LedgerParse:
        p = Path(path).expanduser()
        text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        result = self.parse(text)
        result.source_path = str(p)
        return result

    def parse(self, text: str) -> LedgerParse:
        result = LedgerParse()
        lines = text.splitlines()

        candidates: list[MarkerCandidate] = []
        complete_markers = 0

        for i, line in enumerate(lines):
            # date / recency context for this line (if it is/contains an UPDATE tag)
            date, recency_word = None, ""
            dm = _DATE_RE.search(line)
            if dm:
                date = dm.group(1)
                recency_word = (dm.group(2) or "").lower()
            recency = _RECENCY_RANK.get(recency_word, 10 if not recency_word else 5)

            # explicit colon marker
            for m in _COLON_MARKER.finditer(line):
                tail = m.group(1)
                if _marker_says_complete(tail):
                    complete_markers += 1
                    result.plan_complete_evidence.append(
                        f"line {i+1}: explicit NEXT AUTHORITATIVE PACKET marker is NONE/COMPLETE"
                    )
                    continue
                pid = _extract_first_id(tail)
                if pid:
                    candidates.append(MarkerCandidate(
                        packet=pid, kind="explicit_colon", line_no=i + 1,
                        date=date, recency=recency, doc_order=i, context=line.strip()[:200],
                    ))

            # inline "next authoritative packet is X"
            for m in _INLINE_NEXT.finditer(line):
                if _COLON_MARKER.search(line):
                    continue  # already handled as colon marker
                tail = m.group(1)
                if _marker_says_complete(tail):
                    complete_markers += 1
                    continue
                pid = _extract_first_id(tail)
                if not pid:
                    continue
                kind = "update_next" if _UPDATE_HEADER.search(line) else "inline_next"
                candidates.append(MarkerCandidate(
                    packet=pid, kind=kind, line_no=i + 1,
                    date=date, recency=recency, doc_order=i, context=line.strip()[:200],
                ))

            # supersession (inline)
            for m in _SUPERSEDED_INLINE.finditer(line):
                a, b = m.group(1), m.group(2)
                if is_packet_id(a) and is_packet_id(b):
                    result.superseded.setdefault(normalize_packet_id(a), normalize_packet_id(b))
            for m in _SUPERSEDES.finditer(line):
                sid = m.group(1)
                if is_packet_id(sid):
                    result.superseded.setdefault(normalize_packet_id(sid), "?")

            # plan-complete phrases
            low = line.lower()
            for phrase in _PLAN_COMPLETE_PHRASES:
                if phrase in low:
                    result.plan_complete_evidence.append(f"line {i+1}: '{phrase}'")

        # ---- statuses (table rows + prose) -------------------------------
        self._parse_statuses(lines, result)
        self._parse_superseded_section(lines, result)

        # ---- all packets seen --------------------------------------------
        result.all_packets = find_packet_ids(text)
        result.repair_packets = [p for p in result.all_packets if "REPAIR" in p]
        # split packets: ids with a family that also appears with sub-letters,
        # e.g. IP3 + IP3-A..F  -> IP3 is "split"
        result.split_packets = self._detect_splits(result.all_packets)

        # ---- choose the authoritative next packet ------------------------
        self._choose_next(candidates, result)

        # ---- plan completion ---------------------------------------------
        if complete_markers > 0 and not result.next_authoritative_packet:
            result.plan_complete = True
        if result.plan_complete_evidence and not result.next_authoritative_packet:
            result.plan_complete = True

        return result

    # -- helpers ------------------------------------------------------------
    def _choose_next(self, candidates: list[MarkerCandidate], result: LedgerParse) -> None:
        result.next_candidates = candidates
        if not candidates:
            return
        candidates_sorted = sorted(candidates, key=lambda c: c.score_key(), reverse=True)
        best = candidates_sorted[0]
        # ambiguity: is there another candidate of EQUAL authority (kind, date,
        # recency — NOT doc position) that names a different packet?
        top_auth = best.authority_key()
        rivals = [
            c for c in candidates_sorted
            if c.authority_key() == top_auth
            and normalize_packet_id(c.packet) != normalize_packet_id(best.packet)
        ]
        if rivals:
            result.ambiguous = True
            names = sorted({best.packet, *[c.packet for c in rivals]})
            result.ambiguity_reason = (
                "multiple next-packet markers of equal authority/recency disagree: "
                + ", ".join(names)
            )
            return
        result.next_authoritative_packet = normalize_packet_id(best.packet)
        result.next_source = f"ledger:{best.kind}@line{best.line_no}"

    def _parse_statuses(self, lines: list[str], result: LedgerParse) -> None:
        for i, line in enumerate(lines):
            # markdown table row?
            if line.count("|") >= 2:
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if not cells:
                    continue
                pid = None
                for tok in find_packet_ids(cells[0]):
                    pid = tok
                    break
                if not pid:
                    continue
                status_text = " ".join(cells[1:]).lower()
                # Table rows are the AUTHORITATIVE supersession source (cell 0 is
                # the subject); prose is not.
                self._apply_status(pid, status_text, result, allow_superseded=True)
            else:
                # prose "X is complete/closed/done", "X is BLOCKED".
                # Bound each id's window to stop at the NEXT packet id, so a verb
                # that belongs to a later-mentioned packet (e.g. "F0 -> mark IP0
                # superseded") is not misattributed to the earlier id.
                self._parse_prose_statuses(line, result)

    def _parse_prose_statuses(self, line: str, result: LedgerParse) -> None:
        # Locate every packet-id occurrence with its span, in order.
        spans: list[tuple[int, int, str]] = []
        from autonomous_builder.models import PACKET_TOKEN_RE
        for m in PACKET_TOKEN_RE.finditer(line):
            if is_packet_id(m.group(0)):
                spans.append((m.start(), m.end(), normalize_packet_id(m.group(0))))
        for k, (start, end, pid) in enumerate(spans):
            next_start = spans[k + 1][0] if k + 1 < len(spans) else len(line)
            window = line[end: min(next_start, end + 70)].lower()
            # In prose, DO NOT infer supersession from a forward window: phrases
            # like "RT-D ... do not run IP0" refer to the FOLLOWING id, not the
            # preceding one. Supersession comes from the dedicated section, table
            # rows, and the explicit "X superseded by Y" / "supersedes X" regexes.
            self._apply_status(pid, window, result, allow_superseded=False)

    # A status keyword negated by a nearby "not"/"never"/"no"/"n't" must NOT be
    # applied ("RT-D is not complete" is not COMPLETE).
    _NEGATION = re.compile(r"(?:\bnot\b|\bnever\b|\bno\b|n['’]t)\s+(?:\w+\s+){0,2}$")

    def _negated(self, text: str, keyword: str) -> bool:
        idx = text.find(keyword)
        if idx == -1:
            return False
        return bool(self._NEGATION.search(text[:idx]))

    def _apply_status(self, pid: str, text: str, result: LedgerParse,
                      *, allow_superseded: bool) -> None:
        norm = normalize_packet_id(pid)

        def kw(word: str) -> bool:
            return (word in text) and not self._negated(text, word)

        superseded = allow_superseded and (kw("superseded") or kw("do not run"))
        if superseded:
            result.statuses[norm] = PacketStatus.SUPERSEDED.value
            if norm not in result.superseded:
                result.superseded[norm] = "?"
        elif kw("blocked"):
            result.statuses.setdefault(norm, PacketStatus.BLOCKED.value)
            if norm not in result.blocked_packets:
                result.blocked_packets.append(norm)
        elif kw("failed") or kw("fail "):
            result.statuses.setdefault(norm, PacketStatus.FAILED.value)
            if norm not in result.failed_packets:
                result.failed_packets.append(norm)
        elif any(kw(w) for w in _COMPLETE_WORDS):
            # don't downgrade a superseded/blocked marking
            if result.statuses.get(norm) not in (
                PacketStatus.SUPERSEDED.value, PacketStatus.BLOCKED.value
            ):
                result.statuses[norm] = PacketStatus.COMPLETE.value
            if norm not in result.completed_packets:
                result.completed_packets.append(norm)
        elif kw("in progress") or kw("underway") or kw("in-progress"):
            result.statuses.setdefault(norm, PacketStatus.IN_PROGRESS.value)

    def _parse_superseded_section(self, lines: list[str], result: LedgerParse) -> None:
        in_section = False
        for line in lines:
            low = line.lower()
            if low.startswith("#") and "superseded packet" in low:
                in_section = True
                continue
            if in_section and low.startswith("#"):
                break
            if in_section and line.count("|") >= 2:
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) >= 2:
                    left = find_packet_ids(cells[0])
                    right = find_packet_ids(cells[1])
                    if left:
                        by = right[0] if right else "?"
                        result.superseded[normalize_packet_id(left[0])] = normalize_packet_id(by) if by != "?" else "?"
                        result.statuses[normalize_packet_id(left[0])] = PacketStatus.SUPERSEDED.value

    def _detect_splits(self, packets: list[str]) -> list[str]:
        families: dict[str, bool] = {}
        # a parent "IP3" is split if "IP3-A" style children exist
        parents = set()
        for p in packets:
            if "-" in p:
                parent = p.split("-")[0]
                parents.add(parent)
        return sorted(pk for pk in packets if pk in parents and "-" not in pk)
