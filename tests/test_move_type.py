"""Telling an internal elevation from a lateral hire.

`move_type` was decided by one test — whether the origin firm happened to
equal the destination — and a promotion article states no origin at all. So
every internal elevation was stored as a lateral: the corpus held 11
promotions against 428 laterals while whole articles announced promotion
rounds. Any lateral-versus-promotion split computed from that is wrong.

Every string below is from a real article in the corpus.
"""

from __future__ import annotations

import pytest

from tracker.extract.rules import classify_move_type


@pytest.mark.parametrize(
    "text",
    [
        "Clayton Utz has announced the promotion of seven to its partnership",
        "The latest round of promotions at DLA Piper has added a further 14 lawyers",
        "Clyde & Co has promoted five to partner in Australia in its latest promotions round",
        "Nicole Whitby has been promoted to partner as part of a massive promotions round",
        "Dentons elevates seven lawyers to partnership",
    ],
)
def test_an_internal_elevation_is_a_promotion(text):
    assert classify_move_type(text, origin_is_known=False) == "promotion"


def test_arriving_through_a_firm_combination_is_not_an_individual_move():
    """Nobody chose anything, so it is a different market signal."""
    text = ("Dentons has confirmed the names of its new partners in Australia "
            "as a result of absorbing Fisher Jeffries, its associate firm")
    assert classify_move_type(text, origin_is_known=False) == "merger_absorbed"


@pytest.mark.parametrize(
    "text",
    [
        # A firm welcomes people to its partnership whether it promoted them
        # or hired them. The article withheld which, so the rule must not
        # supply an answer.
        "Sparke Helmore has welcomed four additions to its national partnership",
        "MinterEllison announced an expansion, adding seven consulting "
        "specialists to its partnership",
        "Reed Smith has bolstered its partnership in Singapore with two key appointments",
        "Holding Redlich has announced the recruitment of two new partners",
    ],
)
def test_ambiguous_partnership_language_leaves_the_default_alone(text):
    assert classify_move_type(text, origin_is_known=False) is None


@pytest.mark.parametrize(
    "text",
    [
        "Dentons has appointed former Holding Redlich special counsel Michael "
        "Stretton to the partnership",
        "Herbert Smith Freehills has welcomed three tax partners from "
        "Greenwoods to its partnership",
        # Even outright promotion vocabulary loses to a named origin.
        "Clyde & Co has promoted five to partner in its promotions round",
    ],
)
def test_a_stated_origin_firm_outranks_promotion_language(text):
    """The origin is direct evidence the person came from somewhere else."""
    assert classify_move_type(text, origin_is_known=True) is None


def test_empty_text_decides_nothing():
    assert classify_move_type("", origin_is_known=False) is None
    assert classify_move_type(None, origin_is_known=False) is None
