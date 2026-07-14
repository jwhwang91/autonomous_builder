"""Shared domain models, enums and packet-id primitives.

This module is the dependency foundation for the whole package. It deliberately
depends only on the standard library so every other layer (repository, claude,
execution, state) can import it freely without cycles.

Packet ids are treated as opaque, evolvable strings. The system must never
hardcode a sequence such as R0..R13; it supports ids like ``R0``, ``RT-A``,
``RT-D1``, ``GR1``, ``GR1-REPAIR-A``, ``IP6`` and ``IP9′`` (with a Unicode
prime). See :func:`normalize_packet_id` for the canonicalisation rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Time helpers (runtime code — not a workflow script — so datetime is fine).
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PacketStatus(str, Enum):
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    IN_PROGRESS = "IN_PROGRESS"
    PENDING = "PENDING"
    SUPERSEDED = "SUPERSEDED"
    UNKNOWN = "UNKNOWN"


class TestOutcome(str, Enum):
    __test__ = False  # tell pytest not to try to collect this as a test class
    PASS = "PASS"
    FAIL = "FAIL"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class WorkingTree(str, Enum):
    CLEAN = "CLEAN"
    DIRTY = "DIRTY"
    UNKNOWN = "UNKNOWN"


class PlanComplete(str, Enum):
    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"


class DiscoveryOutcome(str, Enum):
    NEXT_PACKET = "NEXT_PACKET"
    PLAN_COMPLETE = "PLAN_COMPLETE"
    AMBIGUOUS = "AMBIGUOUS"
    NO_DATA = "NO_DATA"


class RunStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class StopReason(str, Enum):
    NONE = "NONE"
    PLAN_COMPLETE = "PLAN_COMPLETE"
    DIRTY_TREE = "DIRTY_TREE_BEFORE_PACKET"
    BLOCKED = "PACKET_BLOCKED"
    FAILED_TESTS = "FAILED_TESTS"
    STOP_AT_GRAPHIFY_GATE = "STOP_AT_GRAPHIFY_GATE"
    AMBIGUOUS_DISCOVERY = "AMBIGUOUS_PACKET_DISCOVERY"
    MAX_RETRIES = "MAX_RETRIES_EXCEEDED"
    BRANCH_MISMATCH = "BRANCH_MISMATCH"
    ORIGIN_MISMATCH = "ORIGIN_MISMATCH"
    WRONG_REPO = "WRONG_REPO"
    NOT_A_REPO = "NOT_A_GIT_REPOSITORY"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    UNEXPECTED_PUSH = "UNEXPECTED_PUSH"
    TIMEOUT = "TIMEOUT"
    BOOTSTRAP_FAILED = "BOOTSTRAP_FAILED"
    NO_COMMIT = "NO_COMMIT_WHEN_REQUIRED"
    USER_STOP = "USER_STOP"
    NO_DATA = "NO_DISCOVERY_DATA"


# ---------------------------------------------------------------------------
# Packet id primitives
# ---------------------------------------------------------------------------

# A packet-id *token*: an uppercase family prefix plus dash-joined uppercase /
# digit segments and an optional trailing prime. Intentionally shape-based, not
# an enumerated list, so evolved ids (RT-D -> RT-D1 -> GR1-REPAIR-A) all match.
# Segments are uppercase+digits ONLY: this prevents lowercase continuations such
# as "R0-report" (a filename fragment) from being misread as packet ids.
_PACKET_TOKEN = r"[A-Z][A-Z0-9]*(?:[-‐‑][A-Z0-9]+)*[′ʹ'’]?"
PACKET_TOKEN_RE = re.compile(_PACKET_TOKEN)

# Characters various tools use for a "prime" (IP9', IP9′, IP9’ ...).
_PRIME_CHARS = "′ʹ’'"
_DASH_CHARS = "-‐‑"

# Uppercase tokens that look like packet ids by shape but never are. Used only to
# reduce false positives in loose scans; explicit-marker extraction does not rely
# on it.
STOPWORDS = {
    "COMPLETE", "BLOCKED", "FAILED", "PASS", "FAIL", "PARTIAL", "CLEAN", "DIRTY",
    "YES", "NO", "NONE", "TODO", "UPDATE", "PACKET", "STATUS", "COMMIT", "TESTS",
    "REPORT", "AUTHORITATIVE", "NEXT", "PLAN", "DRIFT", "BLOCKERS", "RISKS",
    "UNRESOLVED", "GRAPHIFY", "REQUIRED", "WORKING", "TREE", "SUPERSEDED", "DONE",
    "TP", "TBD", "N", "A", "I", "THE", "AND", "OR", "DO", "NOT", "RUN", "GATE",
    "END", "AUTONOMOUS", "BUILDER", "RESULT", "PENDING", "PROGRESS",
}


def _strip_markup(token: str) -> str:
    """Remove common Markdown emphasis / punctuation around a candidate token."""
    token = token.strip()
    # strip surrounding markdown emphasis and strike-through
    for pat in ("**", "__", "~~", "*", "_", "`"):
        token = token.strip(pat)
    # strip leading/trailing punctuation that is not part of an id
    token = token.strip(" \t\n\r.,;:()[]{}<>\"")
    return token


def normalize_packet_id(raw: str) -> str:
    """Canonicalise a packet id for storage and equality comparison.

    - strips Markdown emphasis / surrounding punctuation
    - upper-cases the alphabetic portions
    - canonicalises any dash variant to ``-``
    - canonicalises any prime/apostrophe variant to ``′``
    """
    if raw is None:
        return ""
    token = _strip_markup(str(raw))
    if not token:
        return ""
    # normalise dashes
    for d in _DASH_CHARS[1:]:
        token = token.replace(d, "-")
    # normalise a trailing prime-like character
    if token and token[-1] in _PRIME_CHARS:
        token = token[:-1] + "′"
    return token.upper()


def is_packet_id(raw: str) -> bool:
    """Return True if *raw* looks like a genuine packet id.

    Shape-based and family-agnostic: a token qualifies when it matches the packet
    token grammar and carries a digit, an internal dash, or a trailing prime
    (which real packet ids always do), and is not a known stopword.
    """
    token = _strip_markup(str(raw))
    if not token:
        return False
    m = PACKET_TOKEN_RE.fullmatch(token)
    if not m:
        return False
    upper = token.upper()
    if upper in STOPWORDS:
        return False
    # first alphabetic run must be a short family prefix (1..4 letters)
    lead = re.match(r"[A-Za-z]+", token).group(0)
    if not (1 <= len(lead) <= 4):
        return False
    has_digit = any(c.isdigit() for c in token)
    has_dash = any(c in _DASH_CHARS for c in token)
    has_prime = token[-1] in _PRIME_CHARS
    return has_digit or has_dash or has_prime


def find_packet_ids(text: str) -> list[str]:
    """Return every distinct, normalised packet id appearing in *text* (in order)."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for m in PACKET_TOKEN_RE.finditer(text or ""):
        tok = m.group(0)
        if is_packet_id(tok):
            norm = normalize_packet_id(tok)
            if norm not in seen_set:
                seen_set.add(norm)
                seen.append(norm)
    return seen


def first_packet_id_after(text: str, start: int) -> Optional[str]:
    """Return the first probable packet id found in ``text[start:]`` (normalised)."""
    for m in PACKET_TOKEN_RE.finditer(text, start):
        tok = m.group(0)
        if is_packet_id(tok):
            return normalize_packet_id(tok)
    return None


# ---------------------------------------------------------------------------
# JSON serialisation helper
# ---------------------------------------------------------------------------


def to_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses / enums / Paths into JSON-safe values."""
    from dataclasses import is_dataclass, fields as dc_fields

    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dc_fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    return str(obj)


# ---------------------------------------------------------------------------
# Repository / Git
# ---------------------------------------------------------------------------


@dataclass
class GitState:
    """A read-only snapshot of the target Git repository."""

    root: str
    exists: bool = False
    is_repo: bool = False
    branch: Optional[str] = None
    head: Optional[str] = None
    dirty: bool = False
    origin_url: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    changed_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def working_tree(self) -> WorkingTree:
        if not self.is_repo:
            return WorkingTree.UNKNOWN
        return WorkingTree.DIRTY if self.dirty else WorkingTree.CLEAN

    @property
    def short_head(self) -> Optional[str]:
        return self.head[:7] if self.head else None


# ---------------------------------------------------------------------------
# Claude session result (parsed sentinel block)
# ---------------------------------------------------------------------------


@dataclass
class ClaudeResult:
    """Structured form of the AUTONOMOUS_BUILDER_RESULT sentinel block."""

    found_block: bool = False
    status: PacketStatus = PacketStatus.UNKNOWN
    packet: Optional[str] = None
    commit: Optional[str] = None
    next_authoritative_packet: Optional[str] = None
    tests: TestOutcome = TestOutcome.UNKNOWN
    working_tree: WorkingTree = WorkingTree.UNKNOWN
    plan_complete: PlanComplete = PlanComplete.UNKNOWN
    report: Optional[str] = None
    graphify_update_required: Optional[bool] = None
    blockers: list[str] = field(default_factory=list)
    plan_drift: list[str] = field(default_factory=list)
    unresolved_risks: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)
    raw_block: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        """True when the block was present and carried the mandatory fields."""
        return (
            self.found_block
            and self.status != PacketStatus.UNKNOWN
            and bool(self.packet)
            and not self.parse_errors
        )


# ---------------------------------------------------------------------------
# Packet discovery
# ---------------------------------------------------------------------------


@dataclass
class DiscoveryResult:
    outcome: DiscoveryOutcome
    next_packet: Optional[str] = None
    authority_source: Optional[str] = None  # e.g. "ledger:explicit-marker"
    completed_packets: list[str] = field(default_factory=list)
    superseded_packets: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)
    ambiguity_reason: Optional[str] = None
    plan_complete_evidence: list[str] = field(default_factory=list)

    @property
    def is_ambiguous(self) -> bool:
        return self.outcome == DiscoveryOutcome.AMBIGUOUS

    @property
    def is_plan_complete(self) -> bool:
        return self.outcome == DiscoveryOutcome.PLAN_COMPLETE


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@dataclass
class VerificationCheck:
    name: str
    ok: bool
    detail: str = ""
    blocking: bool = True


@dataclass
class VerificationReport:
    checks: list[VerificationCheck] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", blocking: bool = True) -> None:
        self.checks.append(VerificationCheck(name, ok, detail, blocking))

    @property
    def failures(self) -> list[VerificationCheck]:
        return [c for c in self.checks if not c.ok and c.blocking]

    @property
    def ok(self) -> bool:
        return not self.failures


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


@dataclass
class RetryDecision:
    should_retry: bool
    safe: bool
    reason: str
    stop_reason: StopReason = StopReason.NONE


# ---------------------------------------------------------------------------
# Handoff
# ---------------------------------------------------------------------------


@dataclass
class Handoff:
    completed_packet: Optional[str] = None
    status: str = PacketStatus.UNKNOWN.value
    commit: Optional[str] = None
    next_authoritative_packet: Optional[str] = None
    tests: dict[str, str] = field(default_factory=dict)
    report: Optional[str] = None
    plan_drift: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    unresolved_risks: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    graphify_update: str = "UNKNOWN"
    working_tree: str = WorkingTree.UNKNOWN.value
    attempt: int = 1
    timestamp: str = field(default_factory=utcnow_iso)


# ---------------------------------------------------------------------------
# Run state / history
# ---------------------------------------------------------------------------


@dataclass
class PacketHistoryEntry:
    packet: str
    attempt: int
    status: str
    commit: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    session_log: Optional[str] = None
    result_json: Optional[str] = None
    verification_ok: Optional[bool] = None
    stop_reason: str = StopReason.NONE.value
    note: str = ""


@dataclass
class RunState:
    run_id: str
    project: str
    started_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)
    status: str = RunStatus.IDLE.value
    current_packet: Optional[str] = None
    attempt: int = 0
    completed_packets: list[str] = field(default_factory=list)
    packet_history: list[PacketHistoryEntry] = field(default_factory=list)
    commits: dict[str, str] = field(default_factory=dict)
    latest_handoff: Optional[str] = None
    latest_report: Optional[str] = None
    next_packet: Optional[str] = None
    plan_complete: bool = False
    stop_reason: str = StopReason.NONE.value
    stop_detail: str = ""
    last_graphify_update: Optional[dict] = None
    # set when a post-commit graphify gate fails; the update is re-attempted on
    # the next run/resume before any new packet, so the stop is durable.
    graphify_pending: Optional[dict] = None
    claude_pid: Optional[int] = None
    first_session_done: bool = False
    last_session_log: Optional[str] = None

    # -- serialisation ------------------------------------------------------
    def to_dict(self) -> dict:
        return to_jsonable(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RunState":
        history = [
            PacketHistoryEntry(**h) if isinstance(h, dict) else h
            for h in data.get("packet_history", [])
        ]
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["packet_history"] = history
        return cls(**kwargs)

    def touch(self) -> None:
        self.updated_at = utcnow_iso()
