"""Body sentences the templates could not read.

`adds_named_role` listed only present-tense verbs — "adds", "hires",
"recruits" — which is how a *headline* is written. The body it introduces
writes "has added", "has recruited", "has taken onboard". So the shape that
carries the person's name most often was the one shape that never matched.
Measured across the rejected items, it was the largest single template gap.

Every sentence below is from a real article in the corpus.
"""

from __future__ import annotations

import pytest

from tracker.extract import body_rules
from tracker.firms import FirmGazetteer

GAZETTEER = FirmGazetteer.load()


def people(body: str, headline: str) -> list[str]:
    return [h.person for h in body_rules.find(body, headline, GAZETTEER)]


@pytest.mark.parametrize(
    "body, headline, expected",
    [
        ("HFW has recruited senior partner Brinton Scott and of counsel "
         "Danielle Peng, both in Shanghai.",
         "HFW recruits in Shanghai", "Brinton Scott"),
        ("Bird & Bird has taken onboard new corporate partner David Cheng "
         "from the shuttered Hong Kong office.",
         "Bird & Bird adds corporate partner", "David Cheng"),
        ("Dorsey has hired corporate of counsel Carlton Ng in its HK office.",
         "Dorsey hires in Hong Kong", "Carlton Ng"),
        ("Thomson Geer has appointed Clayton Utz lawyer Cameron Forbes as a "
         "partner in the firm's tax practice.",
         "Thomson Geer lures Clayton Utz lawyer as new tax partner",
         "Cameron Forbes"),
    ],
)
def test_a_past_tense_body_sentence_is_read(body, headline, expected):
    assert expected in people(body, headline)


def test_a_role_phrase_between_the_noun_and_the_name_no_longer_blocks_it():
    """`addition_of` required the name to follow "of" directly, so "the
    addition of aviation lawyer Ethan Tan" defeated it."""
    body = ("Mishcon de Reya has beefed up its transactional team in Singapore "
            "with the addition of aviation lawyer Ethan Tan as a partner.")
    assert "Ethan Tan" in people(body, "Mishcon de Reya expands in Singapore")


def test_the_present_tense_shape_still_works():
    """The headline form this template was written for must not regress."""
    body = "Dentons adds former Dechert's partner Stephen Chan to its corporate practice."
    assert "Stephen Chan" in people(body, "Dentons adds corporate specialist in Hong Kong")


@pytest.mark.parametrize(
    "body",
    [
        # No role word next to the name: the role word is what tells a lawyer
        # from a practice, an office or a client, so these must stay silent.
        "Dentons has added a new Brisbane office to its Australian network.",
        "The firm has recruited heavily across Greater China this year.",
    ],
)
def test_a_verb_without_a_role_word_beside_a_name_reads_nothing(body):
    assert people(body, "Dentons expands in Australia") == []
