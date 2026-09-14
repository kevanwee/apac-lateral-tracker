"""Which appointments count as partner-level.

Every rejected string below is a real `moves.title_to` from the stored corpus,
and every one of them entered the dataset as a partner move because
PARTNER_LEVEL_TITLE accepts bare "counsel" and bare "director". Seventeen
records were affected. The brief is partner-level movement, so these are
records that should never have been made.
"""

from __future__ import annotations

import pytest

from tracker.extract.rules import is_partner_level


@pytest.mark.parametrize(
    "title",
    [
        # Real stored titles, asia-business-law-journal-archive.
        "patent counsel in Beijing",
        "of counsel at its Hong Kong office",
        "counsel in its Singapore office",
        "its newest counsel",
        "an of counsel to join its litigation",
        "international counsel to join its litigation team",
        "of counsel in the firm's Hong Kong office in a",
        "of counsel in Jones Day's global disputes practice",
        # Real stored titles, australasian-lawyer-archive.
        "special counsel",
        "special counsel on the environment and planning",
        "the role of special counsel",
        "its WHS practice as special counsel",
        "special counsel by the national firm",
        "its executive director of licencing to boost t",
        "the firm's first director of global workforce",
    ],
)
def test_a_non_partner_appointment_is_not_partner_level(title):
    assert is_partner_level(title) is False


@pytest.mark.parametrize(
    "title",
    [
        "partner",
        "a partner in Sydney to strengthen its global M",
        "the newest partner in its projects and government commercial",
        "a new partner and co-head of its employment and benefits team",
        "the sole country managing partner in Japan",
        "head of restructuring",
        "co-head of real estate",
        "managing partner",
        "consulting principal",
        # "Director" alone is partner-equivalent in an incorporated legal
        # practice, which is how Singapore and Australian firms style it.
        # Both stored records that rest on a bare "director" are Singapore
        # law corporations -- Kennedys Singapore and TSMP -- where Director is
        # the partner grade. A director *of a named function* is excluded
        # above; that is what separates these from "director of licencing".
        "a director",
        "the team's new director",
        "general counsel",
    ],
)
def test_a_partner_level_appointment_still_passes(title):
    assert is_partner_level(title) is True


def test_partner_wins_when_a_clause_says_both():
    """"Partner and head of counsel training" is a partner appointment."""
    assert is_partner_level("partner and head of counsel training") is True
    assert is_partner_level("principal and special counsel to the board") is True


def test_associate_partner_is_partner_level_but_senior_associate_is_not():
    assert is_partner_level("associate partner") is True
    assert is_partner_level("senior associate") is False
    assert is_partner_level("an associate in the corporate team") is False


def test_an_absent_title_is_not_asserted_to_be_partner_level():
    assert is_partner_level(None) is False
    assert is_partner_level("") is False
