"""Parser for the AUTONOMOUS_BUILDER_RESULT sentinel block.

The parser is deliberately tolerant: Claude will emit lots of prose around the
block, terminal ANSI, partial lines, and occasionally a malformed field. The
required structure is::

    AUTONOMOUS_BUILDER_RESULT
    STATUS: COMPLETE | BLOCKED | FAILED
    PACKET: <packet id>
    COMMIT: <hash or NONE>
    NEXT_AUTHORITATIVE_PACKET: <packet id or NONE>
    TESTS: PASS | FAIL | PARTIAL
    WORKING_TREE: CLEAN | DIRTY
    PLAN_COMPLETE: YES | NO
    REPORT: <path or NONE>
    GRAPHIFY_UPDATE_REQUIRED: YES | NO
    BLOCKERS:
    - ...
    PLAN_DRIFT:
    - ...
    UNRESOLVED_RISKS:
    - ...
    END_AUTONOMOUS_BUILDER_RESULT
"""
from __future__ import annotations

import re
from typing import Optional

from autonomous_builder.models import (
    ClaudeResult,
    PacketStatus,
    PlanComplete,
    TestOutcome,
    WorkingTree,
    normalize_packet_id,
)

START_SENTINEL = "AUTONOMOUS_BUILDER_RESULT"
END_SENTINEL = "END_AUTONOMOUS_BUILDER_RESULT"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]|[\x00-\x08\x0b\x0c\x0e-\x1f]")
_SCALAR_FIELDS = {
    "STATUS", "PACKET", "COMMIT", "NEXT_AUTHORITATIVE_PACKET", "TESTS",
    "WORKING_TREE", "PLAN_COMPLETE", "REPORT", "GRAPHIFY_UPDATE_REQUIRED",
}
_LIST_FIELDS = {"BLOCKERS", "PLAN_DRIFT", "UNRESOLVED_RISKS"}
_ALL_FIELDS = _SCALAR_FIELDS | _LIST_FIELDS

# accept both SHA-1 (40 hex) and SHA-256 (64 hex) object names, plus abbreviations
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)
_NONE_TOKENS = {"", "NONE", "N/A", "NA", "-", "—", "NULL", "TBD"}


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences and stray control characters."""
    if not text:
        return text
    return _ANSI_RE.sub("", text).replace("\r", "")


def _clean_scalar(value: str) -> str:
    value = value.strip()
    # strip surrounding markdown/backticks/quotes
    value = value.strip("`*_ \t\"'")
    return value


def _is_none(value: str) -> bool:
    return _clean_scalar(value).upper() in _NONE_TOKENS


def _extract_block(text: str) -> Optional[str]:
    """Return the LAST complete sentinel block body, else None."""
    blocks: list[str] = []
    idx = 0
    while True:
        s = text.find(START_SENTINEL, idx)
        if s == -1:
            break
        e = text.find(END_SENTINEL, s + len(START_SENTINEL))
        if e == -1:
            break
        blocks.append(text[s + len(START_SENTINEL): e])
        idx = e + len(END_SENTINEL)
    if blocks:
        return blocks[-1]
    # Tolerate a start sentinel with no explicit end (truncated): take everything
    # after the last start sentinel.
    s = text.rfind(START_SENTINEL)
    if s != -1:
        return text[s + len(START_SENTINEL):]
    return None


class ResultParser:
    def parse(self, raw_text: str) -> ClaudeResult:
        text = strip_ansi(raw_text or "")
        result = ClaudeResult()

        body = _extract_block(text)
        if body is None:
            result.parse_errors.append("no AUTONOMOUS_BUILDER_RESULT block found")
            return result
        result.found_block = True
        result.raw_block = body.strip()

        scalars: dict[str, str] = {}
        lists: dict[str, list[str]] = {k: [] for k in _LIST_FIELDS}

        current_list: Optional[str] = None
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # a bullet under the current list section
            m_field = re.match(r"^\**([A-Z_]+)\**\s*:\s*(.*)$", stripped)
            key = m_field.group(1) if m_field else None
            if key in _ALL_FIELDS:
                if key in _LIST_FIELDS:
                    current_list = key
                    inline = _clean_scalar(m_field.group(2))
                    if inline and inline.upper() not in _NONE_TOKENS:
                        lists[key].append(inline)
                else:
                    current_list = None
                    scalars[key] = m_field.group(2)
                continue
            # bullet line for the active list section
            if current_list and re.match(r"^[-*•]\s+", stripped):
                item = re.sub(r"^[-*•]\s+", "", stripped).strip()
                if item and item.upper() not in _NONE_TOKENS:
                    lists[current_list].append(item)
                continue
            # any non-field, non-bullet line ends a list section
            if current_list and not re.match(r"^[-*•]", stripped):
                current_list = None

        self._apply(result, scalars, lists)
        return result

    # -- field application --------------------------------------------------
    def _apply(self, result: ClaudeResult, scalars: dict, lists: dict) -> None:
        # required scalar fields
        if "STATUS" not in scalars:
            result.parse_errors.append("missing required field STATUS")
        else:
            status = _clean_scalar(scalars["STATUS"]).upper()
            if status in PacketStatus.__members__:
                result.status = PacketStatus[status]
            else:
                result.parse_errors.append(f"malformed STATUS: {scalars['STATUS']!r}")
                result.status = PacketStatus.UNKNOWN

        if "PACKET" not in scalars or not _clean_scalar(scalars.get("PACKET", "")):
            result.parse_errors.append("missing required field PACKET")
        else:
            result.packet = normalize_packet_id(scalars["PACKET"]) or None

        # commit
        commit_raw = _clean_scalar(scalars.get("COMMIT", ""))
        if _is_none(commit_raw):
            result.commit = None
        elif _COMMIT_RE.match(commit_raw):
            result.commit = commit_raw.lower()
        else:
            result.commit = commit_raw  # keep, but note it doesn't look like a sha
            result.parse_errors.append(f"COMMIT does not look like a git sha: {commit_raw!r}")

        # next packet
        npk = scalars.get("NEXT_AUTHORITATIVE_PACKET", "")
        result.next_authoritative_packet = None if _is_none(npk) else (normalize_packet_id(npk) or None)

        # tests / tree / plan-complete enums
        result.tests = self._enum(scalars.get("TESTS"), TestOutcome, TestOutcome.UNKNOWN, result, "TESTS")
        result.working_tree = self._enum(scalars.get("WORKING_TREE"), WorkingTree, WorkingTree.UNKNOWN, result, "WORKING_TREE")
        result.plan_complete = self._enum(scalars.get("PLAN_COMPLETE"), PlanComplete, PlanComplete.UNKNOWN, result, "PLAN_COMPLETE")

        # report
        report_raw = _clean_scalar(scalars.get("REPORT", ""))
        result.report = None if _is_none(report_raw) else report_raw

        # graphify update required
        gur = _clean_scalar(scalars.get("GRAPHIFY_UPDATE_REQUIRED", "")).upper()
        if gur in {"YES", "TRUE", "Y"}:
            result.graphify_update_required = True
        elif gur in {"NO", "FALSE", "N"}:
            result.graphify_update_required = False
        else:
            result.graphify_update_required = None

        result.blockers = lists.get("BLOCKERS", [])
        result.plan_drift = lists.get("PLAN_DRIFT", [])
        result.unresolved_risks = lists.get("UNRESOLVED_RISKS", [])

    @staticmethod
    def _enum(value, enum_cls, default, result: ClaudeResult, field: str):
        if value is None:
            return default
        v = _clean_scalar(value).upper()
        if v in enum_cls.__members__:
            return enum_cls[v]
        if v and v not in _NONE_TOKENS:
            result.parse_errors.append(f"malformed {field}: {value!r}")
        return default
