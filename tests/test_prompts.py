from __future__ import annotations

from autonomous_builder.claude.prompts import PromptBuilder, render
from autonomous_builder.models import DiscoveryOutcome, DiscoveryResult, GitState, Handoff


def _git():
    return GitState(root="/t", exists=True, is_repo=True, branch="main", head="abc1234def")


def _disc():
    return DiscoveryResult(outcome=DiscoveryOutcome.NEXT_PACKET, next_packet="RT-D",
                           authority_source="ledger:explicit_colon@line1",
                           disagreements=["reports suggest RT-B but authority selects RT-D"])


def test_render_replaces_and_blanks():
    out = render("a {{X}} b {{MISSING}} c", {"X": "1"})
    assert out == "a 1 b  c"


def test_first_session_includes_starting_prompt_once(profile):
    pb = PromptBuilder()
    prompt = pb.build_packet_prompt(
        profile=profile, git_state=_git(), discovery=_disc(), handoff=None,
        starting_prompt="ORIENTATION-TEXT-XYZZY", recent_report_paths=[], is_first_session=True,
    )
    assert prompt.count("ORIENTATION-TEXT-XYZZY") == 1
    assert "STARTING PROMPT" in prompt
    assert "FIRST session" in prompt  # compact handoff note for first session


def test_non_first_session_omits_starting_prompt(profile):
    pb = PromptBuilder()
    h = Handoff(completed_packet="RT-C", status="COMPLETE", commit="abc1234",
                next_authoritative_packet="RT-D")
    prompt = pb.build_packet_prompt(
        profile=profile, git_state=_git(), discovery=_disc(), handoff=h,
        starting_prompt="ORIENTATION-TEXT-XYZZY", recent_report_paths=[], is_first_session=False,
    )
    assert "ORIENTATION-TEXT-XYZZY" not in prompt
    assert "PREVIOUS SESSION HANDOFF" in prompt
    assert "Completed packet: RT-C" in prompt
    assert "Do not trust this handoff alone" in prompt.replace("NOT", "not")


def test_paths_rendered_safely_with_spaces(profile):
    profile.assets.test_asset_dir = "/Users/x/Desktop/Decks/Artist Deck"
    pb = PromptBuilder()
    prompt = pb.build_packet_prompt(
        profile=profile, git_state=_git(), discovery=_disc(), handoff=None,
        starting_prompt=None, recent_report_paths=["/a b/r.md"], is_first_session=True,
    )
    # no leftover placeholders
    assert "{{" not in prompt and "}}" not in prompt
    assert "/a b/r.md" in prompt


def test_result_sentinel_present(profile):
    prompt = PromptBuilder().build_packet_prompt(
        profile=profile, git_state=_git(), discovery=_disc(), handoff=None,
        starting_prompt=None, recent_report_paths=[], is_first_session=True,
    )
    assert "AUTONOMOUS_BUILDER_RESULT" in prompt
    assert "END_AUTONOMOUS_BUILDER_RESULT" in prompt
    assert "PUSH POLICY: pushing is DISABLED" in prompt


def test_disagreements_included(profile):
    prompt = PromptBuilder().build_packet_prompt(
        profile=profile, git_state=_git(), discovery=_disc(), handoff=None,
        starting_prompt=None, recent_report_paths=[], is_first_session=True,
    )
    assert "reports suggest RT-B but authority selects RT-D" in prompt


def test_repair_prompt(profile):
    prompt = PromptBuilder().build_repair_prompt(
        profile=profile, git_state=_git(), handoff=None,
        repair_reason="prior attempt left tree dirty",
    )
    assert "REPAIR" in prompt
    assert "prior attempt left tree dirty" in prompt
    assert "AUTONOMOUS_BUILDER_RESULT" in prompt
