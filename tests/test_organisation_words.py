"""Organisations that reached the person slot.

The person slot is filled from a sentence, and a sentence carries outlets and
firms as well as people. Three records survived every other guard in
`_looks_like_a_person` — the gazetteer prefix check, NOT_A_PERSON, the digit
check, the firm resolution and the place check — because the words that make a
string an organisation were in none of those lists.

All three strings below are real `people.canonical_name` values.
"""

from __future__ import annotations

import pytest

from tracker.extract.rules import ORGANISATION_WORDS, _looks_like_a_person
from tracker.firms import FirmGazetteer

GAZETTEER = FirmGazetteer.load()


@pytest.mark.parametrize(
    "candidate",
    [
        "Asia Business Law Journal",   # the outlet's own masthead
        "Law Offices Wang Xikang",     # a firm suffix glued to a real name
        "lantegy legal founder",       # a role phrase; the article named nobody
    ],
)
def test_an_organisation_is_not_stored_as_a_person(candidate):
    assert _looks_like_a_person(candidate, GAZETTEER) is False


@pytest.mark.parametrize(
    "candidate",
    [
        # "Law" is the organisation word this list deliberately omits: it is a
        # common Hong Kong surname, and rejecting it would lose real partners
        # in a depth market.
        "Anthony Law",
        "Sarah Chen",
        "Wei Ming Tan",
        "Michael Robertson",
    ],
)
def test_a_real_name_still_passes(candidate):
    assert _looks_like_a_person(candidate, GAZETTEER) is True


@pytest.mark.parametrize("surname", ["law", "young", "king", "long", "white"])
def test_surnames_that_are_also_organisation_words_stay_out_of_the_list(surname):
    """These read as organisation words but are surnames in this corpus.

    Asserted so that extending ORGANISATION_WORDS cannot quietly start
    rejecting real partners.
    """
    assert surname not in ORGANISATION_WORDS
