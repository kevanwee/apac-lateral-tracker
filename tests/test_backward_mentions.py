"""Mentions that refer back to an earlier hire.

An article about one arrival routinely names others: it ends by noting who
else joined this year. Those sentences read like movement sentences because
they describe movements — just not the one being reported, and usually not in
the period the record would be dated to. Thirteen stored records were built
from a sentence of this shape.

Each sentence below is the real one, with the firm and person kept because
CLAUDE.md requires a regression test to run on the text that produced the bug.
"""

from __future__ import annotations

import pytest

from tracker.extract.rules import mention_is_backward_looking


def at(body: str, name: str) -> int:
    index = body.find(name)
    assert index > 0, "fixture must contain the name"
    return index


@pytest.mark.parametrize(
    "body, name",
    [
        ("Hogan Lovells has appointed a data privacy partner in Singapore. "
         "This move follows the recent hire of Ed Sheremeta, who joined as "
         "co-head of the firm's real estate practice.", "Ed Sheremeta"),
        ("Zhong Lun has hired a corporate partner in Hong Kong. This follows "
         "Zhong Lun's recruitment in October of former Sidley Austin partner "
         "Renee Xiong, marking the second such hire.", "Renee Xiong"),
        ("Kennedys has bolstered its insurance practice with three partners. "
         "This move follows the hiring of Lucinda Lyons in December last "
         "year.", "Lucinda Lyons"),
        ("The firm expanded its New York bench. The firm announced in June "
         "the arrival of Jamie Lynn Walter to the Washington office.",
         "Jamie Lynn Walter"),
        ("Jones Day has added a partner in Sydney. Jones Day also welcomed "
         "Paul Greening to its Melbourne office.", "Paul Greening"),
        ("Withers has hired a corporate partner. Earlier this year, Withers "
         "also welcomed a litigation and arbitration team led by Chenthil "
         "Kumarasingam.", "Chenthil Kumarasingam"),
        ("Mayer Brown continues to grow in Asia. He is the latest M&A partner "
         "to have joined Mayer Brown in Asia following the recent addition of "
         "Eiji Kobayashi.", "Eiji Kobayashi"),
        ("The firm promoted four partners. The new partners follow the "
         "addition last year of Paul Lingard.", "Paul Lingard"),
    ],
)
def test_a_mention_introduced_as_an_earlier_event_is_flagged(body, name):
    assert mention_is_backward_looking(body, at(body, name)) is not None


@pytest.mark.parametrize(
    "body, name",
    [
        # Neutral phrasing: this *is* the announcement, not a look back.
        ("DLA Piper continues to expand its Los Angeles office with the "
         "hiring of James Williams.", "James Williams"),
        ("Bird & Bird announced the recruitment of Sven-Michael Werner.",
         "Sven-Michael Werner"),
        ("The arrival of Robert Freedman strengthens the corporate team.",
         "Robert Freedman"),
        # A cue that sits *after* the name does not govern it. This is the
        # opening-line shape that a looser check flagged wrongly.
        ("A&O Shearman has appointed Peter McDonald as co-managing partner, "
         "following a review of its Asia leadership.", "Peter McDonald"),
    ],
)
def test_the_subject_of_the_article_is_not_flagged(body, name):
    assert mention_is_backward_looking(body, at(body, name)) is None


def test_a_cue_in_an_earlier_sentence_does_not_reach_across_the_full_stop():
    body = ("The move follows a difficult year for the firm. Sarah Chen joins "
            "as a partner in Singapore.")
    assert mention_is_backward_looking(body, at(body, "Sarah Chen")) is None


def test_a_distant_cue_in_the_same_sentence_does_not_govern():
    body = (
        "The firm said the appointment follows a strategic review of its "
        "Asia-Pacific footprint conducted over eighteen months by the "
        "management board and its external advisers, and confirmed that "
        "Sarah Chen will lead the practice."
    )
    assert mention_is_backward_looking(body, at(body, "Sarah Chen")) is None


def test_a_mention_at_the_start_of_the_body_is_never_backward_looking():
    assert mention_is_backward_looking("Sarah Chen joins the firm.", 0) is None
