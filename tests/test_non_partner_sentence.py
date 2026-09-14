"""Non-partner appointments where no title was captured.

`is_partner_level` only runs when a template captured a title. Ten stored
records captured none, so nothing checked them, and appointments the article
itself calls a special counsel or a senior associate were stored as partner
moves.

The check reads the person's own sentence rather than the headline, and that
distinction is the point of the file: the headline rule is the one that looks
obviously right and quietly deletes true records.
"""

from __future__ import annotations

import pytest

from tracker.extract.rules import sentence_states_non_partner_role


def at(body: str, name: str) -> int:
    index = body.find(name)
    assert index >= 0, "fixture must contain the name"
    return index


@pytest.mark.parametrize(
    "body, name",
    [
        ("McCabes has enhanced its insurance division with the appointment of "
         "Steven Donley as special counsel in the firm's Melbourne office.",
         "Steven Donley"),
        ("Special counsel Shehan Gunatunga joined the firm earlier this year, "
         "as did senior associate Zaid Mohammed.", "Shehan Gunatunga"),
        ("Colin Biggers & Paisley has named April Campbell as a special "
         "counsel in its Sydney office.", "April Campbell"),
        ("Jones Day has appointed Daigo Takahashi as of counsel in Tokyo.",
         "Daigo Takahashi"),
    ],
)
def test_a_sentence_that_states_a_non_partner_role_abstains(body, name):
    assert sentence_states_non_partner_role(body, at(body, name)) is not None


@pytest.mark.parametrize(
    "body, name",
    [
        # One sentence announcing a partner and a junior together. The partner
        # must survive: this is the shape a headline rule gets wrong.
        ("Recently, the firm also announced the appointment of Michelle "
         "MacMahon as a new health partner, along with that of Troy Gurnett "
         "as a partner and Liam Maguire as a senior associate.",
         "Michelle MacMahon"),
        # An elevation *from* special counsel *to* the partnership. Reading
        # only the word "partner" would miss "partnership" and reject it.
        ("Dentons has added a Holding Redlich special counsel to its "
         "partnership, welcoming Michael Stretton.", "Michael Stretton"),
        ("Gadens has announced the lateral hire of Andrew Rich as a new "
         "partner in the firm's workplace advisory practice.", "Andrew Rich"),
        ("Macpherson Kelley has welcomed Stuart Gibson to its commercial team "
         "as a new principal lawyer.", "Stuart Gibson"),
    ],
)
def test_a_partner_in_the_same_sentence_is_not_abstained_on(body, name):
    assert sentence_states_non_partner_role(body, at(body, name)) is None


def test_a_role_in_a_neighbouring_sentence_does_not_reach_across_the_full_stop():
    body = ("The firm appointed two new special counsel to its Canberra team. "
            "Sarah Chen joined the partnership in Sydney.")
    assert sentence_states_non_partner_role(body, at(body, "Sarah Chen")) is None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known cost. When an article states the role collectively in its lede "
        "-- 'the appointment of two new special counsel' -- and the person's "
        "own sentence only says they joined a named team, no sentence carries "
        "the role next to the name and the record is still made. Catching it "
        "needs the lede to govern every later mention, which would delete "
        "true records in the common shape where a lede announces one hire and "
        "the body names other, genuine, partners. Two or three records in a "
        "sample of 50 are affected; person and to_firm on them are correct, "
        "so the cost is scope, not accuracy."
    ),
)
def test_a_role_stated_only_collectively_in_the_lede_is_not_caught():
    body = ("Maddocks has strengthened its government practice in Canberra "
            "with the appointment of two new special counsel to its real "
            "estate and probity teams. Chloe Costas has joined the firm's "
            "real estate team, bringing experience from MinterEllison.")
    assert sentence_states_non_partner_role(body, at(body, "Chloe Costas")) is not None
