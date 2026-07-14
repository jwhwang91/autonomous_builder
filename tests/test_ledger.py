from __future__ import annotations

from autonomous_builder.repository.ledger import LedgerParser


def parse(text: str):
    return LedgerParser().parse(text)


def test_standard_next_packet():
    p = parse("NEXT AUTHORITATIVE PACKET: RT-D — the plane camera.\n")
    assert p.next_authoritative_packet == "RT-D"
    assert not p.ambiguous


def test_bold_inline_marker():
    p = parse("Prose. The next authoritative packet is **RT-E** (compilation).\n")
    assert p.next_authoritative_packet == "RT-E"


def test_latest_update_wins_over_older():
    text = (
        "> **UPDATE (2026-07-13, latest):** done; the next authoritative packet is **RT-D**.\n"
        "> **UPDATE (2026-07-12, prior):** the next authoritative packet is **RT-B**.\n"
    )
    p = parse(text)
    assert p.next_authoritative_packet == "RT-D"


def test_explicit_colon_beats_inline():
    text = (
        "The next authoritative packet is RT-B someday.\n"
        "NEXT AUTHORITATIVE PACKET: RT-D — canonical pointer.\n"
    )
    p = parse(text)
    assert p.next_authoritative_packet == "RT-D"
    assert p.next_source and "explicit_colon" in p.next_source


def test_repair_packet_detected():
    text = "NEXT AUTHORITATIVE PACKET: GR1-REPAIR-A — fix the gate.\n"
    p = parse(text)
    assert p.next_authoritative_packet == "GR1-REPAIR-A"
    assert "GR1-REPAIR-A" in p.repair_packets


def test_split_packet_detected():
    text = (
        "| IP3 | Director layer | Done |\n"
        "IP3-A complete. IP3-B complete. IP3-C1 complete.\n"
        "NEXT AUTHORITATIVE PACKET: RT-A\n"
    )
    p = parse(text)
    assert "IP3" in p.split_packets


def test_superseded_packet():
    text = (
        "## 9. Superseded packets (do not run)\n"
        "| Superseded packet | Superseded by | Authority |\n"
        "|---|---|---|\n"
        "| **IP0** | **F0** | Foundation |\n"
        "NEXT AUTHORITATIVE PACKET: RT-D\n"
    )
    p = parse(text)
    assert "IP0" in p.superseded
    assert p.superseded["IP0"] == "F0"
    assert p.status_of("IP0").value == "SUPERSEDED"


def test_no_next_packet():
    p = parse("NEXT AUTHORITATIVE PACKET: NONE\nEverything else complete.\n")
    assert p.next_authoritative_packet is None


def test_ambiguous_two_equal_markers():
    text = (
        "NEXT AUTHORITATIVE PACKET: RT-D — one\n"
        "NEXT AUTHORITATIVE PACKET: RT-E — two\n"
    )
    p = parse(text)
    assert p.ambiguous
    assert p.ambiguity_reason and "RT-D" in p.ambiguity_reason and "RT-E" in p.ambiguity_reason


def test_completed_and_status():
    text = (
        "RT-C is complete. R0 is complete.\n"
        "NEXT AUTHORITATIVE PACKET: RT-D\n"
    )
    p = parse(text)
    assert "RT-C" in p.completed_packets
    assert p.status_of("RT-C").value == "COMPLETE"


def test_unicode_prime_packet():
    p = parse("NEXT AUTHORITATIVE PACKET: IP9′ — primed packet.\n")
    assert p.next_authoritative_packet == "IP9′"


def test_prose_do_not_run_does_not_supersede_preceding_id():
    # regression: "do not run IP0" must not mark the preceding RT-D superseded
    p = parse("RT-D is the active packet; do not run IP0 (its predecessor).\n"
              "NEXT AUTHORITATIVE PACKET: RT-D\n")
    assert "RT-D" not in p.superseded
    assert p.next_authoritative_packet == "RT-D"
    assert not p.ambiguous


def test_negated_status_not_applied():
    # regression: "RT-D is not complete" must not mark RT-D COMPLETE
    p = parse("RT-D is not complete yet.\nNEXT AUTHORITATIVE PACKET: RT-D\n")
    assert "RT-D" not in p.completed_packets
    assert p.status_of("RT-D").value != "COMPLETE"
    # and "not blocked" must not mark BLOCKED
    p2 = parse("RT-D is not blocked.\nNEXT AUTHORITATIVE PACKET: RT-D\n")
    assert "RT-D" not in p2.blocked_packets


def test_table_supersession_still_works():
    # supersession from a status-table row is still authoritative
    p = parse("| ~~IP0~~ | Story graph | SUPERSEDED by F0 — do not run |\n"
              "NEXT AUTHORITATIVE PACKET: RT-D\n")
    assert p.status_of("IP0").value == "SUPERSEDED"
