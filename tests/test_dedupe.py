"""Phase 4 pair scoring.

The test that earns its place here is the team-move one: two partners with the
same surname joining the same firm in the same window must stay two records.
Everything else in this file exists to stop a later weights change from
quietly breaking that.
"""

from __future__ import annotations

from datetime import date

import pytest

from tracker import dedupe


def move(**kwargs) -> dict:
    """A blocked move. Defaults are the sparse shape trade press actually gives."""
    base = {
        "person_name": "Sarah Chen",
        "name_variants": [],
        "from_firm_id": None,
        "office_jurisdiction": None,
        "title_to": None,
        "announced_date": date(2026, 3, 1),
    }
    base.update(kwargs)
    return base


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------

def test_two_partners_sharing_a_surname_are_not_one_partner():
    """A lift-out is the normal way this pair arises. Merging it deletes a
    partner from the dataset and inflates the confidence of the survivor."""
    got = dedupe.score_pair(
        move(person_name="Sarah Chen"),
        move(person_name="Michael Chen"),
    )
    assert got.decision == "distinct"
    assert got.total == 0.0


def test_no_amount_of_other_agreement_rescues_a_given_name_disagreement():
    """Same origin, same office, same title, same day — still two people."""
    got = dedupe.score_pair(
        move(person_name="Sarah Chen", from_firm_id="f1",
             office_jurisdiction="SG", title_to="partner"),
        move(person_name="Michael Chen", from_firm_id="f1",
             office_jurisdiction="SG", title_to="partner"),
    )
    assert got.decision == "distinct"


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------

def test_the_same_move_reported_twice_merges():
    got = dedupe.score_pair(
        move(from_firm_id="f1", office_jurisdiction="SG", title_to="Partner",
             announced_date=date(2026, 3, 1)),
        move(from_firm_id="f1", office_jurisdiction="SG", title_to="partner",
             announced_date=date(2026, 3, 3)),
    )
    assert got.decision == "merge"


def test_an_exact_given_name_match_merges_without_a_stated_origin():
    """The common real shape: one outlet names the origin firm, the other does
    not. Blocking has already agreed surname, destination and window."""
    got = dedupe.score_pair(
        move(from_firm_id="f1"),
        move(from_firm_id=None, announced_date=date(2026, 3, 6)),
    )
    assert got.decision == "merge"


def test_a_field_only_one_side_states_is_not_a_disagreement():
    one, other = move(office_jurisdiction="SG"), move(office_jurisdiction=None)
    assert dedupe.score_pair(one, other).components["jurisdiction"] is None


def test_a_bracketed_alias_in_the_variants_merges_the_two_spellings():
    """gold-s001. `people.name_variants` already carries both spellings."""
    got = dedupe.score_pair(
        move(person_name="Wei Ming (Kevin) Tan SC",
             name_variants=["Wei Ming Tan", "Kevin Tan", "Tan Wei Ming"]),
        move(person_name="Kevin Tan", name_variants=["Kevin Tan"]),
    )
    assert got.given.score == 1.0
    assert got.decision == "merge"


def test_a_middle_name_in_only_one_report_does_not_block_a_merge():
    got = dedupe.score_pair(
        move(person_name="Sarah Jane Chen", from_firm_id="f1"),
        move(person_name="Sarah Chen", from_firm_id="f1"),
    )
    assert got.given.score == 0.9
    assert got.decision == "merge"


# --------------------------------------------------------------------------
# The review band
# --------------------------------------------------------------------------

def test_a_headline_that_never_gave_a_given_name_goes_to_review():
    """"Chen joins Allen & Gledhill" could be either Chen. A human decides."""
    got = dedupe.score_pair(
        move(person_name="Sarah Chen", from_firm_id="f1", office_jurisdiction="SG"),
        move(person_name="Chen", from_firm_id="f1", office_jurisdiction="SG"),
    )
    assert got.given.score == 0.5
    assert got.decision == "ambiguous"


def test_outlets_disagreeing_about_the_origin_firm_go_to_review():
    """Not a merge: one of the two reports is wrong and a merge would pick a
    winner silently. Not distinct either — the person still matches."""
    got = dedupe.score_pair(
        move(from_firm_id="f1"),
        move(from_firm_id="f2"),
    )
    assert got.decision == "ambiguous"
    assert got.components["from_firm"] == "conflict"


def test_outlets_disagreeing_about_the_office_go_to_review():
    got = dedupe.score_pair(
        move(office_jurisdiction="SG"),
        move(office_jurisdiction="HK"),
    )
    assert got.decision == "ambiguous"


def test_an_initial_needs_corroboration_before_it_merges():
    alone = dedupe.score_pair(
        move(person_name="J Chen"), move(person_name="John Chen"))
    assert alone.given.score == 0.75
    assert alone.decision == "ambiguous"

    corroborated = dedupe.score_pair(
        move(person_name="J Chen", from_firm_id="f1",
             office_jurisdiction="SG", title_to="partner"),
        move(person_name="John Chen", from_firm_id="f1",
             office_jurisdiction="SG", title_to="partner"),
    )
    assert corroborated.decision == "merge"


# --------------------------------------------------------------------------
# Mechanics
# --------------------------------------------------------------------------

@pytest.mark.parametrize("days, expected_order", [(0, "high"), (30, "mid"), (60, "zero")])
def test_proximity_decays_across_the_window(days, expected_order):
    got = dedupe._proximity(days)
    if expected_order == "high":
        assert got == dedupe.PROXIMITY_MAX
    elif expected_order == "zero":
        assert got == 0.0
    else:
        assert 0.0 < got < dedupe.PROXIMITY_MAX


def test_proximity_is_never_negative_outside_the_window():
    assert dedupe._proximity(365) == 0.0


def test_a_score_is_never_outside_zero_to_one():
    got = dedupe.score_pair(
        move(from_firm_id="f1", office_jurisdiction="SG", title_to="partner"),
        move(from_firm_id="f1", office_jurisdiction="SG", title_to="partner"),
    )
    assert 0.0 <= got.total <= 1.0


def test_the_score_explains_itself():
    """`review_queue.detail` gets this, so a reviewer sees why, not just what."""
    got = dedupe.score_pair(move(from_firm_id="f1"), move(from_firm_id="f2"))
    as_dict = got.as_dict()
    assert as_dict["given"]["label"]
    assert as_dict["components"]["from_firm"] == "conflict"
    assert as_dict["decision"] == "ambiguous"
