from __future__ import annotations

from autonomous_builder.claude.parser import ResultParser

FULL = """\
some prose before the block
AUTONOMOUS_BUILDER_RESULT
STATUS: COMPLETE
PACKET: RT-D
COMMIT: a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2
NEXT_AUTHORITATIVE_PACKET: RT-E
TESTS: PASS
WORKING_TREE: CLEAN
PLAN_COMPLETE: NO
REPORT: docs/reports/runtime-phase-D-report.md
GRAPHIFY_UPDATE_REQUIRED: YES
BLOCKERS:
- none
PLAN_DRIFT:
- RT-D gained a continuity sub-step
UNRESOLVED_RISKS:
- watch memory on large decks
END_AUTONOMOUS_BUILDER_RESULT
trailing prose that should be ignored
"""


def test_valid_block():
    r = ResultParser().parse(FULL)
    assert r.is_valid
    assert r.status.value == "COMPLETE"
    assert r.packet == "RT-D"
    assert r.commit.startswith("a1b2c3d4")
    assert r.next_authoritative_packet == "RT-E"
    assert r.tests.value == "PASS"
    assert r.working_tree.value == "CLEAN"
    assert r.plan_complete.value == "NO"
    assert r.graphify_update_required is True
    assert r.plan_drift == ["RT-D gained a continuity sub-step"]
    assert r.unresolved_risks == ["watch memory on large decks"]
    assert r.blockers == []  # "- none" filtered out


def test_extra_prose_tolerated():
    text = "blah\n\n" + FULL + "\n\nmore blah\n"
    r = ResultParser().parse(text)
    assert r.is_valid and r.packet == "RT-D"


def test_last_block_wins():
    text = FULL.replace("PACKET: RT-D", "PACKET: RT-OLD") + FULL
    r = ResultParser().parse(text)
    assert r.packet == "RT-D"


def test_missing_field():
    text = "AUTONOMOUS_BUILDER_RESULT\nSTATUS: COMPLETE\nEND_AUTONOMOUS_BUILDER_RESULT"
    r = ResultParser().parse(text)
    assert not r.is_valid
    assert any("PACKET" in e for e in r.parse_errors)


def test_malformed_status():
    text = "AUTONOMOUS_BUILDER_RESULT\nSTATUS: FINISHED_MAYBE\nPACKET: RT-D\nEND_AUTONOMOUS_BUILDER_RESULT"
    r = ResultParser().parse(text)
    assert not r.is_valid
    assert any("malformed STATUS" in e for e in r.parse_errors)


def test_unicode_packet_id():
    text = "AUTONOMOUS_BUILDER_RESULT\nSTATUS: COMPLETE\nPACKET: IP9′\nEND_AUTONOMOUS_BUILDER_RESULT"
    r = ResultParser().parse(text)
    assert r.packet == "IP9′"
    assert r.is_valid


def test_none_commit_becomes_none():
    text = ("AUTONOMOUS_BUILDER_RESULT\nSTATUS: BLOCKED\nPACKET: RT-D\n"
            "COMMIT: NONE\nNEXT_AUTHORITATIVE_PACKET: NONE\nEND_AUTONOMOUS_BUILDER_RESULT")
    r = ResultParser().parse(text)
    assert r.commit is None
    assert r.next_authoritative_packet is None


def test_sha256_commit_accepted():
    # regression: 64-hex SHA-256 object names must be accepted, not flagged
    sha = "a" * 64
    text = (f"AUTONOMOUS_BUILDER_RESULT\nSTATUS: COMPLETE\nPACKET: RT-D\n"
            f"COMMIT: {sha}\nEND_AUTONOMOUS_BUILDER_RESULT")
    r = ResultParser().parse(text)
    assert r.commit == sha
    assert not any("git sha" in e for e in r.parse_errors)


def test_no_block_at_all():
    r = ResultParser().parse("just some text, no sentinel here")
    assert not r.found_block
    assert not r.is_valid


def test_ansi_stripped():
    text = ("\x1b[32mAUTONOMOUS_BUILDER_RESULT\x1b[0m\nSTATUS: COMPLETE\n"
            "PACKET: RT-D\nEND_AUTONOMOUS_BUILDER_RESULT")
    r = ResultParser().parse(text)
    assert r.is_valid and r.packet == "RT-D"
